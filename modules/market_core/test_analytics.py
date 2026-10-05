from copy import deepcopy
from dataclasses import asdict

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import ACTORS, SCHEMA, Harness, address, cases, digest, example
from modules.domain.records import validateRecord
from modules.market_core.analytics import calculateTaskMetrics, executionKey, summarizeCosts
from modules.market_core.test_delegation import projectedChild

UNIT = {"currency": "MON", "decimals": 18}


def costReport(operator, taskId=1, expenses=None, reportId=1):
    report = example("CostReport")
    report["executionRef"]["taskRef"]["taskId"] = str(taskId)
    report["reportId"] = digest(reportId)
    report["items"] = []
    for index, (category, cost) in enumerate(expenses or []):
        item = deepcopy(example("CostReport")["items"][0])
        item.update(
            expenseId=digest(reportId * 100 + index),
            category=category,
            costAtoms=str(cost),
            unit=UNIT,
        )
        report["items"].append(item)
    validateRecord(report, "CostReport", SCHEMA)
    return {"operator": operator, "report": report}


def scope(reports):
    return {(row["operator"], executionKey(row["report"]["executionRef"])) for row in reports}


def fractionWire(value):
    return (
        {"numerator": str(value.numerator), "denominator": str(value.denominator)}
        if value is not None
        else None
    )


@pytest.mark.parametrize(
    "case",
    [c for c in cases("layer-1") if c["action"]["operation"] == "metrics"],
    ids=lambda case: case["id"],
)
def testAnalyticsGoldens(case):
    data = case["initial"]
    h = Harness().stage("SETTLED")
    task = h.task()
    task.receipt.update(
        reason=data["reason"], paidAtoms=data["paid"], refundAtoms=str(100 - int(data["paid"]))
    )
    if data["reason"] == "VALIDATOR_TIMEOUT":
        task.receipt["validation"] = None
    else:
        task.receipt["validation"]["verdict"] = "PASS" if data["reason"] == "SUCCESS" else "FAIL"
    reports = [
        costReport(
            ACTORS["SIGNER"],
            expenses=[
                ("EXECUTION", data["ownExecution"]),
                ("GAS", data["ownGas"]),
                ("FORECAST", data["ownForecast"]),
                ("VALIDATION", data["ownValidation"]),
                ("CHILD_PAYMENT", data["childPaid"]),
            ],
        )
    ]
    descendants = []
    if int(data["childPaid"]):
        task.childrenCreated = 1
        descendants = [projectedChild(task, 2, 40, int(data["childPaid"]), status="SETTLED")]
        reports.append(
            costReport(
                address(301),
                2,
                [("EXECUTION", data["childExecution"]), ("GAS", data["childGas"])],
                2,
            )
        )
    summary = summarizeCosts(reports, scope(reports), UNIT)
    metrics = calculateTaskMetrics(task, descendants, ACTORS["SIGNER"], summary)
    result = {
        field: str(getattr(summary, field))
        for field in ("executionAtoms", "overheadAtoms", "operatingAtoms")
    }
    result |= {
        "cashProfitAtoms": str(metrics.cashProfitAtoms),
        "userSpendingAtoms": str(metrics.userSpendingAtoms),
        "executionUtility": fractionWire(metrics.executionUtility),
    }
    assert result == case["expected"]


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in cases("layer-1")
        if c["action"]["operation"] in ("deduplicate", "costAvailability")
    ],
    ids=lambda case: case["id"],
)
def testCostGoldens(case):
    first = costReport(address(1), expenses=[("EXECUTION", 7)])
    reports = [first]
    if case["action"]["operation"] == "deduplicate":
        second = deepcopy(first)
        second["report"]["reportId"] = digest(2)
        second["report"]["executionRef"]["taskRef"]["taskId"] = "2"
        if not case["initial"]["sameOperator"]:
            second["operator"] = address(2)
        reports.append(second)
        summary = summarizeCosts(reports, scope(reports), UNIT)
        result = {"executionAtoms": str(summary.executionAtoms)}
    else:
        expected = scope(reports)
        if case["initial"].get("reportPresent") is False:
            reports = []
        else:
            first["report"]["items"][0]["unit"] = case["initial"]
        summary = summarizeCosts(reports, expected, UNIT)
        result = {
            field: getattr(summary, field)
            for field in ("executionAtoms", "overheadAtoms", "operatingAtoms")
        }
        result["unavailableReasons"] = list(summary.unavailableReasons)
    assert result == case["expected"]


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("duplicate-report", None),
        ("same-id-conflict", "CONFLICTING_REPORT"),
        ("correction", "CONFLICTING_REPORT"),
        ("expense-conflict", "CONFLICTING_EXPENSE"),
        ("outside", "INVALID_SCOPE"),
        ("incomplete", "INCOMPLETE_REPORT"),
        ("censored", "INCOMPLETE_REPORT"),
        ("cost-null", "UNKNOWN_COST"),
        ("quantity-null", "UNKNOWN_COST"),
        ("precision", "UNIT_MISMATCH"),
    ],
)
def testCostAvailability(mutation, reason):
    first = costReport(address(1), expenses=[("EXECUTION", 7)])
    second = deepcopy(first)
    reports, expected = [first], scope([first])
    if mutation in (
        "duplicate-report",
        "same-id-conflict",
        "correction",
        "expense-conflict",
        "outside",
    ):
        reports.append(second)
        if mutation != "duplicate-report":
            second["report"]["items"][0]["costAtoms"] = "8"
        if mutation in ("correction", "expense-conflict"):
            second["report"]["reportId"] = digest(2)
        if mutation == "correction":
            second["report"]["items"][0]["expenseId"] = digest(999)
        if mutation == "expense-conflict":
            second["report"]["executionRef"]["taskRef"]["taskId"] = "2"
            expected = scope(reports)
        if mutation == "outside":
            second["operator"] = address(2)
    elif mutation in ("incomplete", "censored"):
        first["report"]["completeness"] = mutation.upper()
    elif mutation == "precision":
        first["report"]["items"][0]["unit"] = {"currency": "MON", "decimals": 6}
    else:
        field = "costAtoms" if mutation == "cost-null" else "quantity"
        first["report"]["items"][0][field] = None
    summary = summarizeCosts(reports, expected, UNIT)
    if reason:
        assert summary.unavailableReasons == (reason,)
        assert summary.executionAtoms is None and summary.operatingAtoms is None
    else:
        assert summary.executionAtoms == 7
    # An undeclared empty scope with extra reports must not yield a known zero.
    assert summarizeCosts(reports, set(), UNIT).executionAtoms is None


@given(st.lists(st.integers(0, 10**12), max_size=15))
def testExpenseProperties(amounts):
    report = costReport(address(1), expenses=[("EXECUTION", n) for n in amounts])
    before = deepcopy(report)
    expected = scope([report])
    summary = summarizeCosts([report], expected, UNIT)
    assert summary.executionAtoms == sum(amounts)
    assert summarizeCosts([report, report], expected, UNIT) == summary
    reordered = deepcopy(report)
    reordered["report"]["items"].reverse()
    assert summarizeCosts([reordered], expected, UNIT) == summary
    withTransfer = deepcopy(report)
    transfer = deepcopy(example("CostReport")["items"][0])
    transfer.update(expenseId=digest(999), category="CHILD_PAYMENT", costAtoms="9999", unit=UNIT)
    withTransfer["report"]["items"].append(transfer)
    assert summarizeCosts([withTransfer], expected, UNIT) == summary
    assert report == before


def testMetricScopeAndUnknownData(harness):
    harness.stage("SETTLED")
    task = harness.task()
    operator = ACTORS["SIGNER"]
    report = costReport(operator, expenses=[])
    summary = summarizeCosts([report], scope([report]), UNIT)
    assert calculateTaskMetrics(task, [], operator, summary).cashProfitAtoms == 50
    child = projectedChild(task, 2, 30, 10, status="SETTLED")
    task.childrenCreated = 1
    assert "INVALID_SCOPE" in calculateTaskMetrics(task, [], operator, summary).unavailableReasons
    childReport = costReport(address(301), 2, [], 2)
    summary = summarizeCosts([report, childReport], scope([report, childReport]), UNIT)
    assert calculateTaskMetrics(task, [child], operator, summary).cashProfitAtoms == 40
    child.receipt = None
    pending = calculateTaskMetrics(task, [child], operator, summary)
    assert pending.cashProfitAtoms is None and pending.childPaymentsAtoms is None
    assert "CHILDREN_UNSETTLED" in pending.unavailableReasons
    child.spec["parentRef"] = None
    assert (
        "INVALID_SCOPE" in calculateTaskMetrics(task, [child], operator, summary).unavailableReasons
    )
    task.receipt = None
    assert "TASK_UNSETTLED" in calculateTaskMetrics(task, [], operator, summary).unavailableReasons
    assert asdict(summary)["unit"] == UNIT
    task = Harness().stage("SETTLED").task()
    usd = summarizeCosts(
        [costReport(operator)], scope([report]), {"currency": "USD", "decimals": 6}
    )
    assert calculateTaskMetrics(task, [], operator, usd).cashProfitAtoms is None
