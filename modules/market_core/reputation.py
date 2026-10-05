"""Market-local outcome counters and historical snapshots, never portable feedback."""

from copy import deepcopy

from modules.domain.records import agentKey, taskKey, unsigned
from modules.market_core.market import Q
from modules.market_core.state import Checkpoint, ReputationState

COUNTER_EFFECTS = {
    "SUCCESS": "SUCCESS",
    "VALIDATION_FAILED": "FAILURE",
    "NO_SHOW": "FAILURE",
    "EXECUTION_TIMEOUT": "FAILURE",
    "VALIDATOR_TIMEOUT": "NONE",
    "UNALLOCATED": "NONE",
    "ALLOCATION_EXPIRED": "NONE",
    "CANCELLED": "NONE",
}


def calculateProbability(successes: int, failures: int) -> int:
    unsigned(successes, 64)
    unsigned(failures, 64)
    return Q * (1 + successes) // (2 + successes + failures)


def snapshotCounters(
    reputation: ReputationState, agentRef: dict, taskFamily: str, snapshotBlock: int
) -> tuple[int, int]:
    unsigned(snapshotBlock, 64)
    for checkpoint in reversed(reputation.checkpoints.get((agentKey(agentRef), taskFamily), ())):
        if checkpoint.blockNumber <= snapshotBlock:
            return checkpoint.successes, checkpoint.failures
    return 0, 0


def reduceReceipt(
    reputation: ReputationState, taskSpec: dict, receipt: dict, blockNumber: int, taskCount: int
) -> tuple[ReputationState, dict | None]:
    unsigned(blockNumber, 64)
    unsigned(taskCount, 64)
    key = taskKey(receipt["taskRef"])
    terms = taskSpec["terms"]
    if (
        receipt["taskRef"] != taskSpec["taskRef"]
        or key[2] > taskCount
        or blockNumber < int(taskSpec["createdBlock"])
        or receipt["reason"] not in COUNTER_EFFECTS
        or receipt["counterEffect"] != COUNTER_EFFECTS[receipt["reason"]]
        or receipt["budgetAtoms"] != terms["budgetAtoms"]
        or receipt["refundAddress"] != terms["refundAddress"]
    ):
        raise ValueError("Inconsistent receipt/task binding")
    if any(seen[:2] != key[:2] for seen in reputation.seenReceipts):
        raise ValueError("Foreign market receipt")
    budget, reserve = int(receipt["budgetAtoms"]), int(receipt["reservedAtoms"])
    paid, refund = int(receipt["paidAtoms"]), int(receipt["refundAtoms"])
    expectedPaid = reserve if receipt["reason"] == "SUCCESS" else 0
    if not (0 <= reserve <= budget) or paid != expectedPaid or refund != budget - paid:
        raise ValueError("Inconsistent receipt money")
    unawarded = receipt["reason"] in ("UNALLOCATED", "ALLOCATION_EXPIRED", "CANCELLED")
    if unawarded and any(
        value is not None
        for value in (receipt["winner"], receipt["payout"], receipt["resultDigest"])
    ):
        raise ValueError("Unawarded receipt carries execution fields")
    if unawarded and reserve != 0:
        raise ValueError("Unawarded receipt reserves money")
    if (
        receipt["winner"] is not None
        and receipt["winner"]["chainId"] != taskSpec["taskRef"]["chainId"]
    ):
        raise ValueError("Foreign chain identity")
    previous = reputation.seenReceipts.get(key)
    if previous is not None:
        if previous != receipt:
            raise ValueError("Conflicting receipt replay")
        return deepcopy(reputation), None
    validation = receipt["validation"]
    validated = receipt["reason"] in ("SUCCESS", "VALIDATION_FAILED")
    if validated != (validation is not None):
        raise ValueError("Receipt validation missing or unexpected")
    if validation is not None and (
        validation["executionRef"] != {"taskRef": receipt["taskRef"], "awardId": 1}
        or validation["agentRef"] != receipt["winner"]
        or validation["resultDigest"] != receipt["resultDigest"]
        or validation["validationPolicyDigest"] != terms["validationPolicy"]["digest"]
        or validation["validator"] != terms["validator"]
        or (validation["verdict"] == "PASS") != (receipt["reason"] == "SUCCESS")
    ):
        raise ValueError("Inconsistent receipt validation")
    result = deepcopy(reputation)
    result.seenReceipts[key] = deepcopy(receipt)
    effect = receipt["counterEffect"]
    if effect == "NONE":
        return result, None
    if receipt["winner"] is None or receipt["payout"] is None:
        raise ValueError("An observation requires an awarded identity")
    if (
        receipt["reason"] in ("NO_SHOW", "EXECUTION_TIMEOUT")
        and receipt["resultDigest"] is not None
    ):
        raise ValueError("Unsubmitted task cannot have a result")
    historyKey = agentKey(receipt["winner"]), terms["taskFamily"]
    history = result.checkpoints.get(historyKey, ())
    latest = history[-1] if history else Checkpoint(0, 0, 0)
    if latest.blockNumber > blockNumber:
        raise ValueError("Out-of-order observation")
    successes = latest.successes + (effect == "SUCCESS")
    failures = latest.failures + (effect == "FAILURE")
    if min(latest.successes, latest.failures) < 0 or successes + failures > taskCount:
        raise ValueError("Observation count exceeds the created task namespace")
    point = Checkpoint(blockNumber, successes, failures)
    result.checkpoints[historyKey] = (
        (*history[:-1], point)
        if history and latest.blockNumber == blockNumber
        else (*history, point)
    )
    evidence = {
        "schemaVersion": 1,
        "receiptRef": deepcopy(receipt["taskRef"]),
        "agentRef": deepcopy(receipt["winner"]),
        "taskFamily": terms["taskFamily"],
        "outcome": effect,
        "reason": receipt["reason"],
        "validationPolicyDigest": terms["validationPolicy"]["digest"],
        "resultDigest": receipt["resultDigest"],
        "evidenceDigest": validation["evidence"]["digest"] if validation else None,
    }
    return result, evidence
