import pytest

from conftest import ACTORS, SCHEMA, address, cases, contextFor, example
from modules.domain.records import ProtocolViolation, agentKey, taskKey, validateRecord
from modules.market_core.accounting import validateChildEnvelope
from modules.market_core.state import Applied, CoreState, TaskState
from modules.market_core.test_authority import invoke

CHILD_PAYOUT = address(301)
CHILD_REFUND = ACTORS["SIGNER"]


def childTerms(budget=30, validationBy=2800):
    terms = example("TaskSpec")["terms"]
    terms.update(
        budgetAtoms=str(budget),
        alphaDen="60",
        refundAddress=CHILD_REFUND,
        biddingClose="1500",
        allocationBy="1600",
        acceptBy="1720",
        resultBy="2000",
        validationBy=str(validationBy),
    )
    terms["delegation"] = {"maxDepth": 2, "maxChildren": 0}
    return terms


def projectedChild(parent, taskId, budget, paid=None, reason="SUCCESS", status="SUBMITTED"):
    spec = example("TaskSpec")
    spec.update(
        taskRef={**spec["taskRef"], "taskId": str(taskId)},
        parentRef=parent.spec["taskRef"],
        rootRef=parent.spec["rootRef"],
        requester=ACTORS["SIGNER"],
        depth=1,
        terms=childTerms(budget),
    )
    child = TaskState(spec, status=status, escrowAtoms=budget)
    if status != "OPEN":
        bid = example("Bid")
        bid["offer"].update(
            taskRef=spec["taskRef"],
            payout=CHILD_PAYOUT,
            agentRef={**bid["offer"]["agentRef"], "agentId": "9"},
        )
        child.bids = {agentKey(bid["offer"]["agentRef"]): bid}
        child.topTwo = (bid,)
        child.allocation = {
            **example("Allocation"),
            "taskRef": spec["taskRef"],
            "winner": bid["offer"]["agentRef"],
            "reservedAtoms": str(paid or 0),
        }
        child.result = {
            **example("ResultCommitment"),
            "executionRef": {"taskRef": spec["taskRef"], "awardId": 1},
        }
    if status == "SETTLED":
        record = example("ValidationRecord")
        record.update(
            executionRef=child.result["executionRef"], agentRef=child.allocation["winner"]
        )
        child.receipt = {
            **example("SettlementReceipt"),
            "taskRef": spec["taskRef"],
            "reason": reason,
            "winner": child.allocation["winner"],
            "payout": CHILD_PAYOUT,
            "refundAddress": CHILD_REFUND,
            "budgetAtoms": str(budget),
            "paidAtoms": str(paid),
            "refundAtoms": str(budget - paid),
            "validation": record,
        }
        child.escrowAtoms = 0
    return child


def delegationState(initial):
    spec = example("TaskSpec")
    spec["terms"].update(resultBy=initial["resultBy"], validationBy="4000", alphaDen="200")
    spec["terms"]["delegation"] = {
        "maxDepth": initial["maxDepth"],
        "maxChildren": initial["maxChildren"],
    }
    spec["depth"] = initial["depth"]
    parent = TaskState(spec, status=initial["state"], escrowAtoms=int(initial["parentEscrow"]))
    bid = example("Bid")
    bid["score"] = "80000000"
    parent.bids = {agentKey(bid["offer"]["agentRef"]): bid}
    parent.topTwo = (bid,)
    parent.allocation = {
        **example("Allocation"),
        "reservedAtoms": initial["price"],
        "criticalAtoms": initial["price"],
    }
    parent.ownWorkReserveAtoms = int(initial["ownReserve"])
    parent.reservedChildBudgets = int(initial["reservedChildren"])
    parent.committedChildPayouts = int(initial["paidChildren"])
    parent.childrenCreated, parent.activeChildren = (
        initial["childrenCreated"],
        initial["activeChildren"],
    )
    state = CoreState(taskCount=1, tasks={taskKey(spec["taskRef"]): parent})
    if parent.status == "SETTLED":
        parent.receipt = {
            **example("SettlementReceipt"),
            "reason": "EXECUTION_TIMEOUT",
            "counterEffect": "FAILURE",
            "validation": None,
            "resultDigest": None,
            "paidAtoms": "0",
            "refundAtoms": "100",
        }
    if parent.activeChildren:
        child = projectedChild(parent, 2, int(initial["childEscrow"]))
        state.tasks[taskKey(child.spec["taskRef"])] = child
        state.taskCount = 2
    elif int(initial["childPayout"]) + int(initial["childRefund"]):
        paid = int(initial["childPayout"])
        budget = paid + int(initial["childRefund"])
        child = projectedChild(
            parent, 2, budget, paid, "SUCCESS" if paid else "VALIDATION_FAILED", "SETTLED"
        )
        if not paid:
            child.receipt["validation"]["verdict"] = "FAIL"
            child.receipt["counterEffect"] = "FAILURE"
        state.tasks[taskKey(child.spec["taskRef"])] = child
        state.taskCount = 2
    state.credits = {
        ACTORS["REQUESTER"]: int(initial["parentRefundCredit"]),
        ACTORS["PAYOUT"]: int(initial["parentPayoutCredit"]),
        CHILD_REFUND: int(initial["childRefund"]),
        CHILD_PAYOUT: int(initial["childPayout"]),
    }
    state.depositedAtoms = sum(task.escrowAtoms for task in state.tasks.values()) + sum(
        state.credits.values()
    )
    return state, parent


@pytest.mark.parametrize("case", cases("delegation"), ids=lambda case: case["id"])
def testDelegationGoldens(case):
    initial, action = case["initial"], case["action"]
    state, parent = delegationState(initial)
    operation = action["operation"]
    funds = int(initial["managerFunds"])
    if operation in ("createChildTask", "retryChild"):
        terms = childTerms(int(action["budget"]), int(action["childValidationBy"]))
        terms["delegation"]["maxDepth"] = action.get("childMaxDepth", 2)
        if operation == "retryChild" and state.taskCount == 2:
            terms["retryOf"] = {**example("TaskRef"), "taskId": "2"}
        if "retryOutcome" in action or "retryParent" in action:
            prior = projectedChild(parent, 2, 30, 25, status="SETTLED")
            if "retryParent" in action:
                prior.spec["parentRef"] = {**example("TaskRef"), "taskId": "99"}
                prior.receipt.update(reason="VALIDATION_FAILED", counterEffect="FAILURE")
            state.tasks[taskKey(prior.spec["taskRef"])] = prior
            state.taskCount = 2
            state.depositedAtoms += 30
            state.credits[address(900)] = 30
            terms["retryOf"] = prior.spec["taskRef"]
        data = {"parentRef": parent.spec["taskRef"], "terms": terms}
        name, now = "createChildTask", 1400
        context = contextFor(
            name,
            data,
            now,
            ACTORS[action["actor"]],
            valueAtoms=int(action.get("valueAtoms", action["budget"])),
        )
        if case["id"] == "child-permissions-cannot-expand":
            with pytest.raises(ProtocolViolation, match=case["error"]):
                validateChildEnvelope(parent, terms)
            return
    elif operation == "expireParent":
        name, now = "expireTask", int(initial["resultBy"])
        data = {"taskRef": parent.spec["taskRef"]}
        context = contextFor(name, data, now)
    else:
        child = state.tasks[taskKey({**example("TaskRef"), "taskId": "2"})]
        reason = action["reason"]
        if reason == "SUCCESS":
            child.allocation["reservedAtoms"] = action["price"]
            record = example("ValidationRecord")
            record.update(
                executionRef=child.result["executionRef"],
                agentRef=child.allocation["winner"],
                expiry="5000",
            )
            name, now, data = "settleVerdict", 2100, {"record": record}
        elif reason == "UNALLOCATED":
            child.status, child.allocation, child.result, child.bids, child.topTwo = (
                "OPEN",
                None,
                None,
                {},
                (),
            )
            name, now, data = "allocateTask", 1500, {"taskRef": child.spec["taskRef"]}
        else:
            name, now, data = "expireTask", 3100, {"taskRef": child.spec["taskRef"]}
        context = contextFor(name, data, now)
    result = invoke(state, name, data, context)
    if case["error"]:
        assert result.code == case["error"]
        after, events = state, ()
    else:
        assert isinstance(result, Applied)
        after, events = result.state, result.events
        for emitted in events:
            validateRecord(emitted, "Event", SCHEMA)
        if name == "createChildTask":
            funds -= int(action["budget"])
    assert [emitted["name"] for emitted in events] == case["events"]
    updated = after.tasks[taskKey(parent.spec["taskRef"])]
    projected = {
        **initial,
        "state": updated.status,
        "reservedChildren": str(updated.reservedChildBudgets),
        "paidChildren": str(updated.committedChildPayouts),
        "childrenCreated": updated.childrenCreated,
        "activeChildren": updated.activeChildren,
        "managerFunds": str(funds),
        "parentEscrow": str(updated.escrowAtoms),
        "parentPayoutCredit": str(after.credits.get(ACTORS["PAYOUT"], 0)),
        "parentRefundCredit": str(after.credits.get(ACTORS["REQUESTER"], 0)),
        "childPayout": str(after.credits.get(CHILD_PAYOUT, 0)),
        "childRefund": str(after.credits.get(CHILD_REFUND, 0)),
        "childEscrow": str(
            sum(task.escrowAtoms for task in after.tasks.values() if task is not updated)
        ),
    }
    assert projected == case["expected"]
