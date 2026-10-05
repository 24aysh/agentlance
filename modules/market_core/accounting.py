"""Pure ledger/envelope calculations; no recipient calls or parent cash advances."""

from modules.domain.records import addressBytes, require, unsigned
from modules.market_core.reputation import COUNTER_EFFECTS
from modules.market_core.state import CoreState, TaskState


def settlementAmounts(budget: int, reserve: int, reason: str) -> tuple[int, int]:
    unsigned(budget, 96)
    unsigned(reserve, 96)
    if reserve > budget or reason not in COUNTER_EFFECTS:
        raise ValueError("Invalid settlement input")
    paid = reserve if reason == "SUCCESS" else 0
    return paid, budget - paid


def availableChildBudget(parent: TaskState) -> int:
    available = (
        int(parent.allocation["reservedAtoms"])
        - parent.ownWorkReserveAtoms
        - parent.reservedChildBudgets
        - parent.committedChildPayouts
    )
    if available < 0:
        raise ValueError("Negative child envelope")
    return available


def validateChildEnvelope(parent: TaskState, terms: dict, synthesisSlack: int = 120) -> None:
    limits, child = parent.spec["terms"]["delegation"], terms["delegation"]
    depth = parent.spec["depth"] + 1
    require(depth <= child["maxDepth"] <= limits["maxDepth"], "DEPTH_LIMIT")
    require(
        child["maxChildren"] <= limits["maxChildren"]
        and parent.childrenCreated < limits["maxChildren"],
        "CHILD_LIMIT",
    )
    require(int(terms["budgetAtoms"]) <= availableChildBudget(parent), "ENVELOPE_EXCEEDED")
    require(
        int(terms["validationBy"]) + synthesisSlack <= int(parent.spec["terms"]["resultBy"]),
        "CHILD_DEADLINE",
    )


def withdrawAmount(credit: int, amount: int, receiver: str, succeeded: bool) -> int:
    unsigned(credit, 256)
    unsigned(amount, 256)
    require(amount > 0, "INVALID_RANGE")
    addressBytes(receiver)
    require(amount <= credit, "INSUFFICIENT_CREDIT")
    require(succeeded, "TRANSFER_FAILED")
    return credit - amount


def validateLedger(state: CoreState) -> None:
    escrow = sum(task.escrowAtoms for task in state.tasks.values())
    credits = sum(state.credits.values())
    settled = sum(
        int(task.receipt["paidAtoms"]) + int(task.receipt["refundAtoms"])
        for task in state.tasks.values()
        if task.receipt is not None
    )
    if (
        any(value < 0 for value in state.credits.values())
        or state.withdrawnAtoms < 0
        or state.depositedAtoms != escrow + credits + state.withdrawnAtoms
        or state.depositedAtoms != escrow + settled
    ):
        raise ValueError("Ledger conservation violated")
    for task in state.tasks.values():
        expected = 0 if task.receipt is not None else int(task.spec["terms"]["budgetAtoms"])
        if task.escrowAtoms != expected or (task.status == "SETTLED") != (task.receipt is not None):
            raise ValueError("Task escrow/status mismatch")
