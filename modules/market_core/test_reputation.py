from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import POLICY, cases, contextFor, example
from modules.domain.records import agentKey, taskKey
from modules.market_core.reputation import calculateProbability, reduceReceipt, snapshotCounters
from modules.market_core.state import Checkpoint, CoreState, ReputationState
from modules.market_core.transitions import applyCommand


@pytest.mark.parametrize("case", cases("reputation"), ids=lambda case: case["id"])
def testReputationGoldens(case):
    initial, action = case["initial"], case["action"]
    operation = action["operation"]
    spec, ref = example("TaskSpec"), example("AgentRef")
    key = agentKey(ref), spec["terms"]["taskFamily"]
    if operation in ("probability", "transfer"):
        p = calculateProbability(int(initial["successes"]), int(initial["failures"]))
        result = {"p": p}
        if operation == "transfer":
            result |= {**initial, "owner": action["owner"]}
    elif operation == "createTask":
        state = CoreState(taskCount=int(initial["taskCount"]))
        command = example("createTask")
        rejected = applyCommand(
            state, command, contextFor("createTask", command["input"], 1000), POLICY
        )
        assert rejected.code == case["error"]
        result = {"taskCount": str(state.taskCount)}
    elif operation == "observe":
        receipt = example("SettlementReceipt")
        receipt["reason"] = action["reason"]
        if action["reason"] == "VALIDATION_FAILED":
            receipt["counterEffect"] = "FAILURE"
            receipt["validation"]["verdict"] = "FAIL"
            receipt.update(paidAtoms="0", refundAtoms="100")
        elif action["reason"] == "VALIDATOR_TIMEOUT":
            receipt.update(counterEffect="NONE", validation=None, paidAtoms="0", refundAtoms="100")
        original = ReputationState(
            {key: (Checkpoint(9, int(initial["successes"]), int(initial["failures"])),)}
        )
        if initial["receiptSeen"]:
            original.seenReceipts[taskKey(receipt["taskRef"])] = deepcopy(receipt)
        before = deepcopy(original)
        rep, evidence = reduceReceipt(original, spec, receipt, 26, 5)
        assert original == before
        s, f = snapshotCounters(rep, ref, key[1], 26)
        result = {"successes": str(s), "failures": str(f), "observationAdded": evidence is not None}
    else:
        rep = ReputationState()
        if "families" in initial:
            for family in initial["families"]:
                rep.checkpoints[(agentKey(ref), family["family"])] = (
                    Checkpoint(9, int(family["successes"]), int(family["failures"])),
                )
            snapshot = 9
        else:
            writes = {
                int(row["block"]): Checkpoint(
                    int(row["block"]), int(row["successes"]), int(row["failures"])
                )
                for row in initial["checkpoints"]
            }
            rep.checkpoints[key] = tuple(writes.values())
            snapshot = int(action["creationBlock"]) - 1
        s, f = snapshotCounters(rep, ref, key[1], snapshot)
        result = {"successes": str(s), "failures": str(f), "p": calculateProbability(s, f)}
        if "creationBlock" in action:
            result["snapshotBlock"] = str(snapshot)
    assert result == case["expected"]


@given(st.integers(0, 2**64 - 2), st.integers(0, 2**64 - 2))
def testPriorMonotonicity(s, f):
    p = calculateProbability(s, f)
    assert 0 <= p <= 1000000
    assert calculateProbability(s + 1, f) >= p >= calculateProbability(s, f + 1)


def testReceiptImmutabilityBoundsAndSnapshots():
    spec, receipt = example("TaskSpec"), example("SettlementReceipt")
    key = agentKey(receipt["winner"]), spec["terms"]["taskFamily"]
    rep, evidence = reduceReceipt(ReputationState(), spec, receipt, 26, 3)
    assert evidence == example("ReputationEvidence")
    assert snapshotCounters(rep, receipt["winner"], key[1], 25) == (0, 0)
    duplicate, added = reduceReceipt(rep, spec, receipt, 26, 3)
    assert duplicate == rep and added is None
    conflicting = deepcopy(receipt)
    conflicting["settledAt"] = "2601"
    with pytest.raises(ValueError, match="Conflicting"):
        reduceReceipt(rep, spec, conflicting, 26, 3)
    nextSpec, nextReceipt = deepcopy(spec), deepcopy(receipt)
    nextSpec["taskRef"]["taskId"] = nextReceipt["taskRef"]["taskId"] = "2"
    nextReceipt["validation"]["executionRef"]["taskRef"]["taskId"] = "2"
    combined, _ = reduceReceipt(rep, nextSpec, nextReceipt, 26, 3)
    assert combined.checkpoints[key] == (Checkpoint(26, 2, 0),)
    assert rep.checkpoints[key] == (Checkpoint(26, 1, 0),)
    with pytest.raises(ValueError, match="Out-of-order"):
        reduceReceipt(rep, nextSpec, nextReceipt, 25, 3)
    ceiling = ReputationState({key: (Checkpoint(9, 2**64 - 2, 0),)})
    full, _ = reduceReceipt(ceiling, spec, receipt, 26, 2**64 - 1)
    assert full.checkpoints[key][-1].successes == 2**64 - 1
    with pytest.raises(ValueError, match="namespace"):
        reduceReceipt(full, nextSpec, nextReceipt, 27, 2**64 - 1)
    for field, value in [
        ("counterEffect", "NONE"),
        ("taskRef", nextSpec["taskRef"]),
        ("budgetAtoms", "99"),
        ("validation", None),
        ("winner", None),
    ]:
        invalid = {**receipt, field: value}
        with pytest.raises(ValueError):
            reduceReceipt(ReputationState(), spec, invalid, 26, 3)
    noShow = {
        **receipt,
        "reason": "NO_SHOW",
        "counterEffect": "FAILURE",
        "validation": None,
        "paidAtoms": "0",
        "refundAtoms": "100",
    }
    with pytest.raises(ValueError, match="Unsubmitted"):
        reduceReceipt(ReputationState(), spec, noShow, 26, 3)
    noShow["resultDigest"] = None
    with pytest.raises(ValueError, match="identity"):
        reduceReceipt(ReputationState(), spec, {**noShow, "winner": None}, 26, 3)


def testReceiptFinancialAndNamespaceIntegrity():
    spec, receipt = example("TaskSpec"), example("SettlementReceipt")
    for updates, message in [
        ({"paidAtoms": "0"}, "money"),
        ({"reservedAtoms": "101"}, "money"),
        ({"winner": {**receipt["winner"], "chainId": "1"}}, "chain"),
    ]:
        with pytest.raises(ValueError, match=message):
            reduceReceipt(ReputationState(), spec, {**receipt, **updates}, 26, 3)
    prior, _ = reduceReceipt(ReputationState(), spec, receipt, 26, 3)
    foreignSpec, foreignReceipt = deepcopy(spec), deepcopy(receipt)
    foreignSpec["taskRef"]["market"] = foreignReceipt["taskRef"]["market"] = "0x" + "12" * 20
    with pytest.raises(ValueError, match="market"):
        reduceReceipt(prior, foreignSpec, foreignReceipt, 27, 3)
    cancelled = {
        **receipt,
        "reason": "CANCELLED",
        "counterEffect": "NONE",
        "validation": None,
        "paidAtoms": "0",
        "refundAtoms": "100",
    }
    with pytest.raises(ValueError, match="execution fields"):
        reduceReceipt(ReputationState(), spec, cancelled, 26, 3)
    cancelled.update(winner=None, payout=None, resultDigest=None)
    with pytest.raises(ValueError, match="reserves"):
        reduceReceipt(ReputationState(), spec, cancelled, 26, 3)
