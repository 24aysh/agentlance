from copy import deepcopy
from dataclasses import replace

import pytest

from conftest import ACTORS, POLICY, Harness, address, cases, example
from modules.domain.records import agentKey, taskKey
from modules.market_core.accounting import validateLedger
from modules.market_core.analytics import calculateTaskMetrics, summarizeCosts
from modules.market_core.state import IdentityObservation
from modules.market_core.test_analytics import UNIT, costReport, fractionWire, scope


def treeStart(data):
    h = Harness()
    rootTerms = example("TaskSpec")["terms"]
    rootTerms.update(budgetAtoms=data["rootBudget"], alphaDen=data["rootAlphaDen"])
    h.call("createTask", 1000, {"terms": rootTerms})
    for agent, amount in zip((7, 8), data["rootBids"], strict=True):
        offer = example("Bid")["offer"]
        offer["agentRef"]["agentId"] = str(agent)
        offer["bidAtoms"] = amount
        observation = IdentityObservation(offer["agentRef"], True, ACTORS["OWNER"], offer["payout"])
        h.call(
            "submitBid",
            1100,
            {"offer": offer, "permit": None, "signature": None},
            identityObservation=observation,
        )
    h.call("allocateTask", 1200)
    assert h.task().allocation["reservedAtoms"] == "30"
    # Ownership changes externally after admission. Frozen actor and payout are still authoritative.
    transfer = replace(
        observation, agentRef=example("AgentRef"), owner=address(999), verifiedWallet=None
    )
    h.call(
        "acceptAward",
        1400,
        {"executionRef": example("ExecutionRef"), "ownWorkReserveAtoms": data["ownReserve"]},
        identityObservation=transfer,
    )
    terms = example("TaskSpec")["terms"]
    terms.update(
        budgetAtoms=data["childBudget"],
        alphaDen=data["childAlphaDen"],
        refundAddress=ACTORS["SIGNER"],
        biddingClose="1500",
        allocationBy="1600",
        acceptBy="1720",
        resultBy="2000",
        validationBy="2200",
        delegation={"maxDepth": 2, "maxChildren": 0},
    )
    h.call("createChildTask", 1410, {"parentRef": example("TaskRef"), "terms": terms})
    childRef = h.task(2).spec["taskRef"]
    execution = {"taskRef": childRef, "awardId": 1}
    offer = example("Bid")["offer"]
    offer.update(
        taskRef=childRef,
        bidAtoms=data["childBid"],
        executionSigner=address(300),
        payout=address(301),
    )
    offer["agentRef"]["agentId"] = "9"
    h.call(
        "submitBid",
        1420,
        {"offer": offer, "permit": None, "signature": None},
        caller=address(302),
        identityObservation=IdentityObservation(
            offer["agentRef"], True, address(302), address(301)
        ),
    )
    h.call("allocateTask", 1500, {"taskRef": childRef})
    h.call(
        "acceptAward",
        1600,
        {"executionRef": execution, "ownWorkReserveAtoms": "0"},
        caller=address(300),
    )
    assert h.task(2).allocation["reservedAtoms"] == "10"
    return h, execution


@pytest.mark.parametrize(
    "case",
    [c for c in cases("layer-1") if c["action"]["operation"] == "tree"],
    ids=lambda case: case["id"],
)
def testWholeTree(case):
    data = case["initial"]
    h, execution = treeStart(data)
    h.call(
        "submitResult",
        1900,
        {"executionRef": execution, "artifact": example("ResultCommitment")["artifact"]},
        caller=address(300),
    )
    record = example("ValidationRecord")
    record.update(executionRef=execution, agentRef=h.task(2).allocation["winner"], nonce="5")
    h.call("settleVerdict", 2000, {"record": record})
    childReceipt = deepcopy(h.task(2).receipt)
    assert h.task().reservedChildBudgets == 0
    assert h.task().committedChildPayouts == 10
    assert h.task().activeChildren == 0 and h.task().childrenCreated == 1
    if case["action"]["ending"] == "SUCCESS":
        h.call("submitResult", 2400)
        h.call("settleVerdict", 2600)
    else:
        h.call("expireTask", 2500)
    receipts = [deepcopy(h.task(i).receipt) for i in (1, 2)]
    assert h.task(2).receipt == childReceipt
    for owner, amount in sorted(h.state.credits.items()):
        h.call(
            "withdrawCredit", 2700, {"receiver": owner, "amountAtoms": str(amount)}, caller=owner
        )
    assert [h.task(i).receipt for i in (1, 2)] == receipts
    assert sum(h.state.credits.values()) == 0
    validateLedger(h.state)
    rootKey = (agentKey(example("AgentRef")), "structured-output-v1")
    counters = h.state.reputation.checkpoints[rootKey][-1]
    assert (counters.successes, counters.failures) == (
        (1, 0) if case["action"]["ending"] == "SUCCESS" else (0, 1)
    )
    assert (
        agentKey({**example("AgentRef"), "agentId": "8"}),
        "structured-output-v1",
    ) not in h.state.reputation.checkpoints
    reports = [
        costReport(
            ACTORS["SIGNER"],
            1,
            [("EXECUTION", data["managerExecution"]), ("GAS", data["managerGas"])],
            1,
        ),
        costReport(
            address(300), 2, [("EXECUTION", data["childExecution"]), ("GAS", data["childGas"])], 2
        ),
    ]
    summary = summarizeCosts(reports, scope(reports), UNIT)
    metrics = calculateTaskMetrics(h.task(), [h.task(2)], ACTORS["SIGNER"], summary)
    result = {
        "rootPaid": receipts[0]["paidAtoms"],
        "rootRefund": receipts[0]["refundAtoms"],
        "childPaid": receipts[1]["paidAtoms"],
        "childRefund": receipts[1]["refundAtoms"],
        "withdrawnAtoms": str(h.state.withdrawnAtoms),
        "executionAtoms": str(summary.executionAtoms),
        "overheadAtoms": str(summary.overheadAtoms),
        "operatingAtoms": str(summary.operatingAtoms),
        "cashProfitAtoms": str(metrics.cashProfitAtoms),
        "executionUtility": fractionWire(metrics.executionUtility),
    }
    assert result == case["expected"]
    assert [row["name"] for row in h.events] == case["events"]
    assert len(h.state.usedNonces) == (2 if case["action"]["ending"] == "SUCCESS" else 1)
    assert taskKey(h.task().spec["taskRef"])[0] == POLICY.chainId
