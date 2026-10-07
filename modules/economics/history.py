"""Attributed usage, explicit corrections and pre-run forecast evaluation."""

from fractions import Fraction

from modules.agent_client.ports import checkStamp, checkTaskView, closed, ensure, recordCheck
from modules.economics.pricing import PricingCatalog
from modules.economics.records import (
    checkRuntime,
    checkUsage,
    fractionRecord,
    rational,
    recordDigest,
)


def cohort(task, runtime):
    return [
        task["taskRef"]["chainId"],
        task["taskRef"]["market"],
        task["terms"]["input"]["digest"],
        task["terms"]["taskFamily"],
        task["terms"]["outputSchema"]["digest"],
        task["terms"]["validationPolicy"]["digest"],
        recordDigest(runtime),
    ]


def executionUsage(report, runtime):
    quantities = {}
    for item in report["items"]:
        if item["category"] != "EXECUTION":
            continue
        ensure(item["quantity"] is not None, "Missing execution quantity", "UNAVAILABLE")
        key = item["resource"], item["quantityUnit"]
        quantities[key] = quantities.get(key, Fraction(0)) + rational(item["quantity"])
    if not report["items"]:
        quantities = {(r["resource"], r["quantityUnit"]): Fraction(0) for r in runtime["resources"]}
    usage = [
        {"resource": resource, "quantityUnit": unit, "quantity": fractionRecord(value)}
        for (resource, unit), value in sorted(quantities.items())
    ]
    checkUsage(usage, runtime)
    return usage


class ExecutionHistory:
    def __init__(self, store, schema, market):
        self.store, self.schema, self.market = store, schema, market

    async def recordUsage(self, report, context, supersedesReportId=None):
        recordCheck(report, "CostReport", self.schema)
        closed(context, "operator agentRef task runtime status zeroUsage evidence")
        for field, kind in (
            ("operator", "Address"),
            ("agentRef", "AgentRef"),
            ("task", "TaskSpec"),
        ):
            recordCheck(context[field], kind, self.schema)
        checkRuntime(context["runtime"], self.schema)
        runtime, terms = context["runtime"], context["task"]["terms"]
        ensure(
            runtime["taskFamily"] == terms["taskFamily"]
            and runtime["outputSchemaDigest"] == terms["outputSchema"]["digest"]
            and runtime["validationPolicyDigest"] == terms["validationPolicy"]["digest"],
            "Usage runtime template binding",
        )
        ensure(
            context["agentRef"] == self.store.agentRef
            and context["operator"] == self.store.operator,
            "History attribution",
            "CONFLICT",
        )
        ensure(
            context["task"]["taskRef"] == report["executionRef"]["taskRef"]
            and report["executionRef"]["awardId"] == 1,
            "Usage execution binding",
        )
        ensure(context["status"] in ("COMPLETED", "ABORTED", "TIMED_OUT"), "Run status")
        ensure(
            context["evidence"] in ("FIXTURE", "LOCAL") and type(context["zeroUsage"]) is bool,
            "Usage provenance",
        )
        ids = set()
        for item in report["items"]:
            ensure(item["expenseId"] not in ids, "Duplicate expense")
            ids.add(item["expenseId"])
            ensure(
                item["provenance"] != "PROVIDER_ATTESTED",
                "Unverified provider attestation",
                "UNSUPPORTED",
            )
            if item["quantity"] is not None:
                rational(item["quantity"])
            if report["completeness"] == "COMPLETE":
                ensure(
                    item["quantity"] is not None
                    and item["costAtoms"] is not None
                    and item["provenance"] != "INCOMPLETE",
                    "Incomplete complete report",
                )
        if report["completeness"] == "COMPLETE":
            ensure(report["items"] or context["zeroUsage"], "Zero usage must be declared")
            executionUsage(report, context["runtime"])
        if report["estimateId"] is not None:
            bundle = self.store.get("economic_estimates", report["estimateId"])
            ensure(
                bundle is not None
                and bundle["estimate"]["taskRef"] == context["task"]["taskRef"]
                and bundle["estimate"]["runtimeDigest"] == recordDigest(context["runtime"]),
                "Report estimate binding",
                "CONFLICT",
            )
        view = await self.market.observeAward(report["executionRef"])
        checkTaskView(view, self.schema)
        ensure(
            view["stamp"]["finality"] == "FINALIZED"
            and view["task"] == context["task"]
            and view["winningBid"] is not None
            and view["winningBid"]["offer"]["agentRef"] == context["agentRef"],
            "Usage requires canonical own award",
            "CONFLICT",
        )
        self.store.journal.observe(view["stamp"])
        expectedEvidence = (
            "LOCAL" if self.store.journal.settings.get("mode") == "monad" else "FIXTURE"
        )
        ensure(context["evidence"] == expectedEvidence, "Fixture/live usage isolation")
        attempts = {
            attempt["expenseId"]: key for key, attempt in self.store.items("forecast_attempts")
        }
        for item in report["items"]:
            if item["expenseId"] in attempts:
                ensure(
                    item["category"] == "FORECAST"
                    and attempts[item["expenseId"]]
                    == self.store.candidateKey(report["executionRef"]["taskRef"]),
                    "Forecast expense attribution",
                    "CONFLICT",
                )
        return self.store.saveReport(report, context, supersedesReportId)

    def selectHistory(self, task, runtime, now, evidence):
        rows, excluded = (
            [],
            dict.fromkeys(
                ("INCOMPATIBLE", "EVIDENCE", "OWN_TASK", "AGE", "LIMIT", "INCOMPLETE"), 0
            ),
        )
        for row in self.store.activeReports():
            context, report = row["context"], row["report"]
            reason = None
            if cohort(context["task"], context["runtime"]) != cohort(task, runtime):
                reason = "INCOMPATIBLE"
            elif context["evidence"] != evidence:
                reason = "EVIDENCE"
            elif report["executionRef"]["taskRef"] == task["taskRef"]:
                reason = "OWN_TASK"
            elif not 0 <= now - int(report["observedAt"]) < runtime["maxHistoryAgeSeconds"]:
                reason = "AGE"
            if reason:
                excluded[reason] += 1
            else:
                rows.append(row)
        rows.sort(key=lambda row: (-int(row["report"]["observedAt"]), row["report"]["reportId"]))
        selected = rows[:32]
        incomplete = sum(row["report"]["completeness"] != "COMPLETE" for row in selected)
        excluded.update(LIMIT=max(0, len(rows) - 32), INCOMPLETE=incomplete)
        samples = [
            {"reportId": row["report"]["reportId"], "usage": executionUsage(row["report"], runtime)}
            for row in selected
            if row["report"]["completeness"] == "COMPLETE"
        ]
        return {
            "samples": samples,
            "adequate": incomplete == 0,
            "excludedCount": sum(excluded.values()),
            "excludedReasons": excluded,
            "reportIds": [row["report"]["reportId"] for row in selected],
            "expiresAt": min(
                (
                    int(row["report"]["observedAt"]) + runtime["maxHistoryAgeSeconds"]
                    for row in selected
                ),
                default=2**64 - 1,
            ),
        }

    async def attachSettlement(self, executionRef, receipt, stamp):
        recordCheck(executionRef, "ExecutionRef", self.schema)
        recordCheck(receipt, "SettlementReceipt", self.schema)
        checkStamp(stamp, self.schema)
        ensure(
            stamp["finality"] == "FINALIZED"
            and stamp["chainId"] == executionRef["taskRef"]["chainId"]
            and executionRef["awardId"] == 1
            and receipt["taskRef"] == executionRef["taskRef"]
            and receipt["winner"] == self.store.agentRef,
            "Receipt binding",
        )
        current = await self.market.readSettlement(executionRef["taskRef"])
        checkStamp(current["stamp"], self.schema)
        ensure(
            current["receipt"] == receipt
            and current["stamp"]["finality"] == "FINALIZED"
            and current["stamp"]["chainId"] == executionRef["taskRef"]["chainId"],
            "Receipt is not canonical",
            "CONFLICT",
        )
        stamp = current["stamp"]
        self.store.journal.observe(stamp)
        key = recordDigest(executionRef)
        with self.store.db:
            old = self.store.get("usage_receipts", key)
            if old and old["receipt"] != receipt:
                self.store.journal.halt("Conflicting economics receipt")
            if not old:
                self.store.put(
                    "usage_receipts", key, {"receipt": receipt, "stamp": stamp}, immutable=True
                )

    def evaluateForecasts(self, scope):
        ensure(isinstance(scope, list) and len(scope) <= self.store.maxRows, "Evaluation scope")
        for ref in scope:
            recordCheck(ref, "ExecutionRef", self.schema)
        selected = {recordDigest(ref) for ref in scope}
        result = {
            "uncalibrated": True,
            "scope": scope,
            "missing": len(selected),
            "excluded": 0,
            "excludedReasons": {"INCOMPLETE": 0, "MISSING_ESTIMATE": 0},
            "forecast": [],
            "baseline": [],
            "observations": [],
        }
        for row in self.store.activeReports():
            report, context = row["report"], row["context"]
            if recordDigest(report["executionRef"]) not in selected:
                continue
            result["missing"] -= 1
            bundle = (
                self.store.get("economic_estimates", report["estimateId"])
                if report["estimateId"]
                else None
            )
            if report["completeness"] != "COMPLETE" or bundle is None:
                result["excluded"] += 1
                reason = (
                    "INCOMPLETE" if report["completeness"] != "COMPLETE" else "MISSING_ESTIMATE"
                )
                result["excludedReasons"][reason] += 1
                continue
            ensure(
                int(report["observedAt"]) >= int(bundle["estimate"]["createdAt"]),
                "Report predates forecast",
            )
            catalog = PricingCatalog(
                bundle["pricing"], context["runtime"], self.schema, 0, historical=True
            )
            actual = catalog.priceUsage(executionUsage(report, context["runtime"]))
            result["observations"].append(
                {
                    "executionRef": report["executionRef"],
                    "reportId": report["reportId"],
                    "comparisonAtoms": str(actual),
                    "reportedItems": report["items"],
                    "evidence": context["evidence"],
                    "status": context["status"],
                }
            )
            for name in ("forecast", "baseline"):
                estimate = bundle["estimate" if name == "forecast" else "baseline"]
                result[name].append((actual, estimate))
        for name in ("forecast", "baseline"):
            pairs, measures = result[name], {}
            for q in (80, 95):
                values = [
                    (actual, est[f"p{q}Atoms"])
                    for actual, est in pairs
                    if est is not None and est[f"p{q}Atoms"] is not None
                ]
                measures[f"p{q}"] = {
                    "covered": sum(actual <= int(bound) for actual, bound in values),
                    "eligible": len(values),
                    "excluded": len(pairs) - len(values),
                    "unavailableReason": None if values else "NO_ELIGIBLE_QUANTILES",
                }
            errors = [
                abs(actual - int(est["meanAtoms"]))
                for actual, est in pairs
                if est is not None and est["meanAtoms"] is not None
            ]
            measures["meanAbsoluteErrorAtoms"] = (
                fractionRecord(Fraction(sum(errors), len(errors))) if errors else None
            )
            measures["meanEligible"] = len(errors)
            measures["meanExcluded"] = len(pairs) - len(errors)
            measures["meanUnavailableReason"] = None if errors else "NO_ELIGIBLE_MEANS"
            result[name] = measures
        return result
