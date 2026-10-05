from copy import deepcopy

import pytest

from conftest import ACTORS, POLICY, SCHEMA, cases, contextFor, digest, example
from modules.domain.records import agentKey, taskKey, validateRecord
from modules.market_core.reputation import snapshotCounters
from modules.market_core.state import Applied, Checkpoint, CoreState, TaskState
from modules.market_core.transitions import applyCommand


def lifecycleState(initial):
    state = CoreState()
    if initial["state"] == "ABSENT":
        return state
    spec = example("TaskSpec")
    for field in (
        "biddingClose",
        "allocationBy",
        "acceptBy",
        "resultBy",
        "validationBy",
        "budgetAtoms",
    ):
        spec["terms"][field] = initial[field]
    task = TaskState(spec, status=initial["state"], escrowAtoms=int(initial["escrowAtoms"]))
    for field in (
        "ownWorkReserveAtoms",
        "childrenCreated",
        "activeChildren",
        "reservedChildBudgets",
        "committedChildPayouts",
    ):
        setattr(task, field, int(initial[field]))
    if initial["bidCount"]:
        bid = example("Bid")
        task.bids[agentKey(bid["offer"]["agentRef"])] = bid
        task.topTwo = (deepcopy(bid),)
    if initial["hasWinner"]:
        task.allocation = example("Allocation")
        task.allocation["reservedAtoms"] = initial["reservedAtoms"]
    if initial["hasResult"]:
        task.result = example("ResultCommitment")
    if initial["terminalReason"]:
        task.receipt = example("SettlementReceipt")
    state.tasks[taskKey(spec["taskRef"])] = task
    state.taskCount = 1
    state.depositedAtoms = int(initial["budgetAtoms"])
    state.credits = {
        ACTORS["PAYOUT"]: int(initial["payoutCredit"]),
        ACTORS["REQUESTER"]: int(initial["refundCredit"]),
    }
    key = agentKey(example("AgentRef")), spec["terms"]["taskFamily"]
    state.reputation.checkpoints[key] = (
        Checkpoint(9, int(initial["successes"]), int(initial["failures"])),
    )
    if task.receipt:
        state.reputation.seenReceipts[taskKey(spec["taskRef"])] = deepcopy(task.receipt)
    if task.activeChildren:
        child = deepcopy(spec)
        child["taskRef"] = {**spec["taskRef"], "taskId": "2"}
        child.update(parentRef=spec["taskRef"], depth=1)
        child["terms"]["budgetAtoms"] = initial["reservedChildBudgets"]
        state.tasks[taskKey(child["taskRef"])] = TaskState(
            child, escrowAtoms=task.reservedChildBudgets
        )
        state.taskCount = 2
        state.depositedAtoms += task.reservedChildBudgets
    return state


def projectLifecycle(state, initial):
    projected = deepcopy(initial)
    if not state.tasks:
        return projected
    task = state.tasks[taskKey(example("TaskRef"))]
    projected.update(
        state=task.status,
        createdAt=task.spec["createdAt"],
        reservedAtoms=task.allocation["reservedAtoms"] if task.allocation else "0",
        escrowAtoms=str(task.escrowAtoms),
        payoutCredit=str(state.credits.get(ACTORS["PAYOUT"], 0)),
        refundCredit=str(state.credits.get(ACTORS["REQUESTER"], 0)),
        bidCount=len(task.bids),
        hasWinner=bool(task.allocation and task.allocation["winner"]),
        hasResult=task.result is not None,
        terminalReason=task.receipt["reason"] if task.receipt else None,
    )
    for field in ("ownWorkReserveAtoms", "reservedChildBudgets", "committedChildPayouts"):
        projected[field] = str(getattr(task, field))
    for field in ("childrenCreated", "activeChildren"):
        projected[field] = getattr(task, field)
    s, f = snapshotCounters(state.reputation, example("AgentRef"), "structured-output-v1", 100)
    projected.update(successes=str(s), failures=str(f))
    return projected


@pytest.mark.parametrize("case", cases("lifecycle"), ids=lambda case: case["id"])
def testLifecycleGoldens(case):
    initial, action = case["initial"], case["action"]
    state = lifecycleState(initial)
    before = deepcopy(state)
    name, args = action["command"], action["arguments"]
    command = example(name)
    data = command["input"]
    if name == "acceptAward":
        data["ownWorkReserveAtoms"] = args.get("ownWorkReserveAtoms", "0")
    if name == "settleVerdict":
        data["record"]["expiry"] = "5000"
        data["record"]["verdict"] = args.get("verdict", "PASS")
        if not args.get("resultMatches", True):
            data["record"]["resultDigest"] = digest(900)
        if not args.get("policyMatches", True):
            data["record"]["validationPolicyDigest"] = digest(900)
    if name == "withdrawCredit":
        data["amountAtoms"] = args["amountAtoms"]
    context = contextFor(
        name,
        data,
        int(initial["now"]),
        ACTORS[action["actor"]],
        valueAtoms=int(args.get("valueAtoms", "0")),
        blockNumber=26,
        transferSucceeded=args.get("receiverAccepts", True),
    )
    validateRecord(command, "Command", SCHEMA)
    result = applyCommand(state, command, context, POLICY)
    assert state == before
    if case["error"]:
        assert result.code == case["error"]
        after, events = state, ()
    else:
        assert isinstance(result, Applied)
        after, events = result.state, result.events
        for emitted in events:
            validateRecord(emitted, "Event", SCHEMA)
    assert [emitted["name"] for emitted in events] == case["events"]
    assert projectLifecycle(after, initial) == case["expected"]


def testCanonicalSuccessPayloads(harness):
    harness.stage("SETTLED")
    receipt = harness.task().receipt
    expected = example("SettlementReceipt")
    expected["validation"]["expiry"] = "3600"
    assert receipt == expected
    assert [row["name"] for row in harness.events] == [
        "TaskCreated",
        "BidAccepted",
        "TaskAwarded",
        "AwardAccepted",
        "ResultSubmitted",
        "TaskSettled",
    ]
    first = harness.events[0]["payload"]["task"]
    assert first == example("TaskSpec")
    assert harness.events[1]["payload"] == {"bid": example("Bid"), "permitNonce": None}
    assert harness.events[2]["payload"] == {"allocation": example("Allocation")}
