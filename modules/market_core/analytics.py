"""Reported resource costs and realized cash flows, explicitly separate from settlement."""

from copy import deepcopy
from dataclasses import dataclass
from fractions import Fraction

from modules.domain.records import taskKey
from modules.market_core.state import TaskState

REASONS = (
    "INVALID_SCOPE",
    "CONFLICTING_REPORT",
    "CONFLICTING_EXPENSE",
    "MISSING_REPORT",
    "INCOMPLETE_REPORT",
    "UNKNOWN_COST",
    "UNIT_MISMATCH",
    "TASK_UNSETTLED",
    "CHILDREN_UNSETTLED",
    "UNKNOWN_CORRECTNESS",
)


def executionKey(ref: dict) -> tuple:
    return (*taskKey(ref["taskRef"]), ref["awardId"])


def orderedReasons(reasons) -> tuple:
    return tuple(reason for reason in REASONS if reason in reasons)


@dataclass(frozen=True)
class CostSummary:
    unit: dict
    expectedReports: frozenset
    executionAtoms: int | None
    overheadAtoms: int | None
    operatingAtoms: int | None
    byOperator: dict
    unavailableReasons: tuple


def summarizeCosts(attributedReports: list, expectedReports: set, unit: dict) -> CostSummary:
    expected = frozenset(expectedReports)
    selected, reportIds, expenses = {}, {}, {}
    operators = {operator for operator, _ in expected}
    reasons = {operator: set() for operator in operators}
    invalidScope = False
    for attributed in attributedReports:
        operator, report = attributed["operator"], attributed["report"]
        key = operator, executionKey(report["executionRef"])
        if key not in expected:
            invalidScope = True
            continue
        reportKey = operator, report["reportId"]
        if reportKey in reportIds:
            if reportIds[reportKey] != report:
                reasons[operator].add("CONFLICTING_REPORT")
            continue
        reportIds[reportKey] = report
        if key in selected:
            reasons[operator].add("CONFLICTING_REPORT")
        selected[key] = report
        if report["completeness"] != "COMPLETE":
            reasons[operator].add("INCOMPLETE_REPORT")
        for item in report["items"]:
            expenseKey = operator, item["expenseId"]
            if expenseKey in expenses and expenses[expenseKey] != item:
                reasons[operator].add("CONFLICTING_EXPENSE")
            expenses[expenseKey] = item
            if item["costAtoms"] is None or item["quantity"] is None:
                reasons[operator].add("UNKNOWN_COST")
            if item["unit"] != unit:
                reasons[operator].add("UNIT_MISMATCH")
    for key in expected - selected.keys():
        reasons[key[0]].add("MISSING_REPORT")
    totals = {}
    for operator in sorted(operators):
        if invalidScope:
            reasons[operator].add("INVALID_SCOPE")
        execution, overhead = 0, 0
        if reasons[operator]:
            execution, overhead, operating = None, None, None
        else:
            for (bearer, _), item in expenses.items():
                if bearer != operator or item["category"] == "CHILD_PAYMENT":
                    continue
                if item["category"] == "EXECUTION":
                    execution += int(item["costAtoms"])
                else:
                    overhead += int(item["costAtoms"])
            operating = execution + overhead
        totals[operator] = {
            "executionAtoms": execution,
            "overheadAtoms": overhead,
            "operatingAtoms": operating,
            "unavailableReasons": orderedReasons(reasons[operator]),
        }
    allReasons = set().union(*reasons.values())
    if invalidScope:
        allReasons.add("INVALID_SCOPE")
    aggregate = (
        [None] * 3
        if allReasons
        else [
            sum(row[field] for row in totals.values())
            for field in ("executionAtoms", "overheadAtoms", "operatingAtoms")
        ]
    )
    return CostSummary(deepcopy(unit), expected, *aggregate, totals, orderedReasons(allReasons))


@dataclass(frozen=True)
class TaskMetrics:
    actualPayoutAtoms: int | None
    childPaymentsAtoms: int | None
    cashProfitAtoms: int | None
    userSpendingAtoms: int | None
    executionUtility: Fraction | None
    unavailableReasons: tuple


def calculateTaskMetrics(
    task: TaskState, descendants: list[TaskState], operator: str, costSummary: CostSummary
) -> TaskMetrics:
    reasons = set(costSummary.unavailableReasons)
    nodes = [task, *descendants]
    keys = {taskKey(node.spec["taskRef"]): node for node in nodes}
    validScope = len(keys) == len(nodes)
    direct = []
    for node in nodes:
        children = [
            child for child in descendants if child.spec["parentRef"] == node.spec["taskRef"]
        ]
        validScope &= len(children) == node.childrenCreated
        if node is task:
            direct = children
        else:
            parent = keys.get(taskKey(node.spec["parentRef"])) if node.spec["parentRef"] else None
            validScope &= (
                parent is not None
                and node.spec["depth"] == parent.spec["depth"] + 1
                and node.spec["rootRef"] == task.spec["rootRef"]
            )
    # Strictly increasing depth and complete parent links exclude cycles/disconnected subtrees.
    awarded = {
        (*key, 1)
        for key, node in keys.items()
        if node.allocation is not None and node.allocation["winner"] is not None
    }
    reported = {execution for _, execution in costSummary.expectedReports}
    validScope &= reported == awarded and operator in costSummary.byOperator
    if not validScope:
        reasons.add("INVALID_SCOPE")
    paid = int(task.receipt["paidAtoms"]) if task.receipt else None
    if paid is None:
        reasons.add("TASK_UNSETTLED")
    if any(node.receipt is None for node in descendants):
        reasons.add("CHILDREN_UNSETTLED")
    childPaid = (
        sum(int(child.receipt["paidAtoms"]) for child in direct)
        if validScope and all(child.receipt for child in direct)
        else None
    )
    if costSummary.unit != {"currency": "MON", "decimals": 18}:
        reasons.add("UNIT_MISMATCH")
    profit, utility = None, None
    if not reasons:
        own = costSummary.byOperator[operator]
        profit = paid - childPaid - own["operatingAtoms"]
    validation = task.receipt["validation"] if task.receipt else None
    if validation is None:
        reasons.add("UNKNOWN_CORRECTNESS")
    if not reasons:
        terms = task.spec["terms"]
        utility = (
            int(validation["verdict"] == "PASS")
            - Fraction(int(terms["alphaNum"]), int(terms["alphaDen"])) * costSummary.executionAtoms
        )
    return TaskMetrics(paid, childPaid, profit, paid, utility, orderedReasons(reasons))
