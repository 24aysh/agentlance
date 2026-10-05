from copy import deepcopy
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import ACTORS, POLICY, Harness, address, cases, contextFor, example
from modules.domain.records import ProtocolViolation, agentKey
from modules.market_core.accounting import (
    availableChildBudget,
    settlementAmounts,
    validateLedger,
    withdrawAmount,
)
from modules.market_core.reputation import reduceReceipt
from modules.market_core.state import Applied, Checkpoint, CoreState, Rejected, ReputationState
from modules.market_core.test_authority import invoke
from modules.market_core.test_delegation import childTerms
from modules.market_core.transitions import nonceKey

EXTRA = {
    "alias-credit",
    "checkpoint-at-ceiling",
    "expired-permit-rollback",
    "nonce-type-separation",
    "wrong-observation-binding",
    "terminal-parent-late-child",
}


@pytest.mark.parametrize(
    "case", [c for c in cases("layer-1") if c["id"] in EXTRA], ids=lambda case: case["id"]
)
def testExtraGoldens(case):
    initial, action = case["initial"], case["action"]["operation"]
    events = []
    if action == "settle":
        h = Harness()
        terms = example("TaskSpec")["terms"]
        terms["refundAddress"] = ACTORS["PAYOUT"]
        h.call("createTask", 1000, {"terms": terms})
        for name, now in [
            ("submitBid", 1100),
            ("allocateTask", 1200),
            ("acceptAward", 1400),
            ("submitResult", 2400),
        ]:
            h.call(name, now)
        applied = h.call("settleVerdict", 2600)
        events = [row["name"] for row in applied.events]
        result = {
            "creditAtoms": str(h.state.credits[ACTORS["PAYOUT"]]),
            "escrowAtoms": str(h.task().escrowAtoms),
        }
    elif action == "observe":
        key = agentKey(example("AgentRef")), "structured-output-v1"
        rep = ReputationState(
            {key: (Checkpoint(9, int(initial["successes"]), int(initial["failures"])),)}
        )
        updated, _ = reduceReceipt(
            rep, example("TaskSpec"), example("SettlementReceipt"), 26, int(initial["taskCount"])
        )
        point = updated.checkpoints[key][-1]
        result = {"successes": str(point.successes), "failures": str(point.failures)}
    elif action == "nonceKey":
        keys = [
            nonceKey(POLICY.market, ACTORS["OWNER"], primary, initial["nonce"])
            for primary in initial["primaryTypes"]
        ]
        result = {"equal": keys[0] == keys[1]}
    elif action == "submitBid":
        h = Harness().stage("OPEN")
        data = example("submitBid")["input"]
        if "expiry" in initial:
            data["permit"]["expiry"] = initial["expiry"]
        context = contextFor("submitBid", data, 1100)
        if "observationChainId" in initial:
            context = replace(
                context,
                signatureObservation=replace(
                    context.signatureObservation, chainId=int(initial["observationChainId"])
                ),
            )
        assert invoke(h.state, "submitBid", data, context) == Rejected(case["error"])
        result = {"bidCount": len(h.task().bids), "consumedNonces": len(h.state.usedNonces)}
    else:
        h = Harness().stage("RUNNING")
        terms = childTerms(30, 2200)
        h.call("createChildTask", 1410, {"parentRef": example("TaskRef"), "terms": terms})
        childRef = h.task(2).spec["taskRef"]
        execution = {"taskRef": childRef, "awardId": 1}
        data = example("submitBid")["input"]
        data.update(permit=None, signature=None)
        data["offer"]["taskRef"] = childRef
        h.call("submitBid", 1420, data)
        h.call("allocateTask", 1500, {"taskRef": childRef})
        h.call("acceptAward", 1600, {"executionRef": execution, "ownWorkReserveAtoms": "0"})
        h.call("expireTask", 2500)
        parentReceipt = deepcopy(h.task().receipt)
        applied = h.call("expireTask", 2500, {"taskRef": childRef})
        events = [row["name"] for row in applied.events]
        result = {
            "paidAtoms": h.task(2).receipt["paidAtoms"],
            "refundAtoms": h.task(2).receipt["refundAtoms"],
            "reservedChildBudgets": str(h.task().reservedChildBudgets),
            "activeChildren": h.task().activeChildren,
            "parentReceiptChanged": parentReceipt != h.task().receipt,
        }
    assert result == case["expected"]
    assert events == case["events"]


def testAccountingRejectsImpossibleState():
    for reason, reserve in [("OTHER", 1), ("SUCCESS", 101)]:
        with pytest.raises(ValueError):
            settlementAmounts(100, reserve, reason)
    h = Harness().stage("RUNNING")
    h.task().ownWorkReserveAtoms = 51
    with pytest.raises(ValueError, match="envelope"):
        availableChildBudget(h.task())
    with pytest.raises(ProtocolViolation, match="TRANSFER_FAILED"):
        withdrawAmount(10, 1, address(1), False)
    h = Harness().stage("OPEN")
    h.state.depositedAtoms += 1
    with pytest.raises(ValueError, match="conservation"):
        validateLedger(h.state)
    h.state.depositedAtoms = h.task().escrowAtoms = 99
    with pytest.raises(ValueError, match="escrow/status"):
        validateLedger(h.state)


def testRootRetriesAndFreshFunding():
    h = Harness().stage("OPEN")
    h.call("cancelTask", 1100)
    terms = example("TaskSpec")["terms"]
    terms["retryOf"] = example("TaskRef")
    h.call("createTask", 1150, {"terms": terms})
    assert h.task(2).spec["rootRef"] == h.task(2).spec["taskRef"]
    assert h.task(2).spec["parentRef"] is None
    assert h.state.depositedAtoms == 200
    assert h.state.credits[ACTORS["REQUESTER"]] == 100
    for ref in [{**example("TaskRef"), "taskId": "99"}, h.task(2).spec["taskRef"]]:
        terms["retryOf"] = ref
        assert invoke(
            h.state,
            "createTask",
            {"terms": terms},
            contextFor("createTask", {"terms": terms}, 1150),
        ) == Rejected("INVALID_RETRY")


def testZeroPriceAndFinalTaskId():
    h = Harness()
    terms = example("TaskSpec")["terms"]
    terms.update(budgetAtoms="1", alphaDen="1")
    h.call("createTask", 1000, {"terms": terms})
    offer = example("Bid")["offer"]
    offer["bidAtoms"] = "0"
    h.call("submitBid", 1100, {"offer": offer, "permit": None, "signature": None})
    h.call("allocateTask", 1200)
    assert h.task().allocation["reservedAtoms"] == "0"
    h.call(
        "acceptAward", 1400, {"executionRef": example("ExecutionRef"), "ownWorkReserveAtoms": "0"}
    )
    child = {"parentRef": example("TaskRef"), "terms": childTerms(1, 2200)}
    assert invoke(
        h.state, "createChildTask", child, contextFor("createChildTask", child, 1410)
    ) == Rejected("ENVELOPE_EXCEEDED")
    h.call("submitResult", 2400)
    h.call("settleVerdict", 2600)
    assert h.task().receipt["paidAtoms"] == "0"
    assert h.task().receipt["counterEffect"] == "SUCCESS"
    assert h.state.credits[ACTORS["REQUESTER"]] == 1
    data = example("createTask")["input"]
    nearEnd = CoreState(taskCount=2**64 - 2)
    result = invoke(nearEnd, "createTask", data, contextFor("createTask", data, 1000))
    assert result.state.taskCount == 2**64 - 1
    assert result.events[0]["payload"]["task"]["taskRef"]["taskId"] == str(2**64 - 1)
    assert invoke(
        result.state, "createTask", data, contextFor("createTask", data, 1000)
    ) == Rejected("TASK_LIMIT")


@given(
    st.sampled_from(
        [
            "SUCCESS",
            "VALIDATION_FAILED",
            "NO_SHOW",
            "EXECUTION_TIMEOUT",
            "VALIDATOR_TIMEOUT",
            "UNALLOCATED",
            "ALLOCATION_EXPIRED",
            "CANCELLED",
        ]
    ),
    st.lists(st.integers(0, 100), max_size=8),
)
def testLedgerSequences(reason, withdrawalRequests):
    stage, command, now = {
        "SUCCESS": ("SUBMITTED", "settleVerdict", 2600),
        "VALIDATION_FAILED": ("SUBMITTED", "settleVerdict", 2600),
        "NO_SHOW": ("AWARDED", "expireTask", 1500),
        "EXECUTION_TIMEOUT": ("RUNNING", "expireTask", 2500),
        "VALIDATOR_TIMEOUT": ("SUBMITTED", "expireTask", 3000),
        "UNALLOCATED": ("OPEN", "allocateTask", 1200),
        "ALLOCATION_EXPIRED": ("OPEN", "expireTask", 1300),
        "CANCELLED": ("OPEN", "cancelTask", 1100),
    }[reason]
    h = Harness().stage(stage)
    data = example(command)["input"]
    if reason == "VALIDATION_FAILED":
        data["record"]["verdict"] = "FAIL"
    applied = h.call(command, now, data)
    receipt = deepcopy(h.task().receipt)
    assert receipt["reason"] == reason
    assert int(receipt["paidAtoms"]) == (50 if reason == "SUCCESS" else 0)
    for amount in withdrawalRequests:
        request = {"amountAtoms": str(amount), "receiver": ACTORS["REQUESTER"]}
        result = invoke(
            h.state,
            "withdrawCredit",
            request,
            contextFor("withdrawCredit", request, 4000, ACTORS["REQUESTER"]),
        )
        if isinstance(result, Applied):
            h.state = result.state
        validateLedger(h.state)
        assert h.task().receipt == receipt
        assert invoke(
            h.state,
            "expireTask",
            {"taskRef": receipt["taskRef"]},
            contextFor("expireTask", {}, 5000),
        ) == Rejected("ALREADY_SETTLED")
    # Event records are detached from the caller's prior state, even after later input edits.
    applied.events[0]["payload"]["receipt"]["reason"] = "edited-for-alias-test"
    assert receipt["reason"] == reason


@given(st.lists(st.tuples(st.integers(1, 35), st.booleans()), min_size=1, max_size=4))
def testDelegationSequences(attempts):
    h = Harness().stage("RUNNING")
    # A longer parent window permits sequential attempts without moving chain time backwards.
    h.task().spec["terms"].update(resultBy="10000", validationBy="11000")
    for index, (budget, succeeds) in enumerate(attempts):
        now = 1410 + index * 1000
        terms = childTerms(budget, now + 700)
        terms.update(
            biddingClose=str(now + 100),
            allocationBy=str(now + 200),
            acceptBy=str(now + 320),
            resultBy=str(now + 500),
        )
        data = {"parentRef": example("TaskRef"), "terms": terms}
        result = invoke(h.state, "createChildTask", data, contextFor("createChildTask", data, now))
        if isinstance(result, Rejected):
            assert result.code == "ENVELOPE_EXCEEDED"
            continue
        h.state = result.state
        child = h.task(h.state.taskCount)
        ref = child.spec["taskRef"]
        if succeeds:
            offer = example("Bid")["offer"]
            offer.update(taskRef=ref, bidAtoms="0")
            h.call("submitBid", now + 10, {"offer": offer, "permit": None, "signature": None})
            h.call("allocateTask", now + 100, {"taskRef": ref})
            execution = {"taskRef": ref, "awardId": 1}
            h.call(
                "acceptAward", now + 200, {"executionRef": execution, "ownWorkReserveAtoms": "0"}
            )
            h.call(
                "submitResult",
                now + 400,
                {"executionRef": execution, "artifact": example("ResultCommitment")["artifact"]},
            )
            record = example("ValidationRecord")
            record.update(executionRef=execution, nonce=ref["taskId"])
            h.call("settleVerdict", now + 600, {"record": record})
        else:
            h.call("allocateTask", now + 100, {"taskRef": ref})
        children = [t for t in h.state.tasks.values() if t.spec["parentRef"] == example("TaskRef")]
        parent = h.task()
        assert parent.childrenCreated == len(children)
        assert parent.activeChildren == sum(t.receipt is None for t in children)
        assert parent.reservedChildBudgets == sum(t.escrowAtoms for t in children)
        assert parent.committedChildPayouts == sum(
            int(t.receipt["paidAtoms"]) for t in children if t.receipt
        )
        assert availableChildBudget(parent) >= 0
        validateLedger(h.state)
