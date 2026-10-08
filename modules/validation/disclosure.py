"""Opt-in, one-execution numeric disclosure from a read-only WAL snapshot."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import ensure, recordCheck
from modules.economics.records import recordDigest


def exportCost(path, executionRef, schema, *, now=None):
    recordCheck(executionRef, "ExecutionRef", schema)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")

        def read(table, key, kind=None):
            ensure(
                table
                in {
                    "economic_meta",
                    "usage_active",
                    "usage_reports",
                    "economic_estimates",
                    "validation_records",
                },
                "Disclosure table",
            )
            where, params = ("kind=? AND key=?", (kind, key)) if kind else ("key=?", (key,))
            size = db.execute(f"SELECT length(body) FROM {table} WHERE {where}", params).fetchone()
            if size is None:
                return None
            ensure(size[0] <= 2 * 1048576, "Private record too large", "UNAVAILABLE")
            return strictJson(
                db.execute(f"SELECT body FROM {table} WHERE {where}", params).fetchone()[0]
            )

        binding = read("economic_meta", "binding")
        ensure(binding is not None, "Economics binding unavailable", "NOT_FOUND")
        agent, operator = binding["agentRef"], binding["operator"]
        recordCheck(agent, "AgentRef", schema)
        recordCheck(operator, "Address", schema)
        ensure(agent["chainId"] == executionRef["taskRef"]["chainId"], "Execution chain binding")
        settings = strictJson(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
        ensure(settings.get("agentRef", agent) == agent, "Journal identity binding", "CONFLICT")
        key = recordDigest([operator, executionRef])
        active = read("usage_active", key)
        selected = read("usage_reports", active["key"]) if active else None
        report = selected["report"] if selected else None
        if report:
            recordCheck(report, "CostReport", schema)
            ensure(
                report["executionRef"] == executionRef
                and selected["context"]["agentRef"] == agent
                and selected["context"]["operator"] == operator,
                "Report binding",
                "CONFLICT",
            )
        bundle = (
            read("economic_estimates", report["estimateId"])
            if report and report["estimateId"]
            else None
        )
        estimate = bundle["estimate"] if bundle else None
        if estimate:
            recordCheck(estimate, "CostEstimate", schema)
            ensure(
                estimate["taskRef"] == executionRef["taskRef"]
                and estimate["estimateId"] == report["estimateId"],
                "Estimate binding",
                "CONFLICT",
            )
        pointer = read("validation_records", key, "selection")
        reconciliation = (
            read("validation_records", pointer["key"], "reconciliation") if pointer else None
        )
        missing = [
            "INCOMPLETE_WHOLE_COST_SCOPE",
            "UNREPORTED_VALIDATOR_COST",
            "UNREPORTED_GAS_AND_PARTICIPATION_COST",
        ]
        if reconciliation:
            ensure(
                reconciliation["executionRef"] == executionRef
                and reconciliation["inputs"]["operator"] == operator,
                "Reconciliation binding",
            )
            inputs = reconciliation["inputs"]
            if inputs["reportDigest"] != (recordDigest(selected) if selected else None) or inputs[
                "estimateDigest"
            ] != (recordDigest(bundle) if bundle else None):
                missing.append("STALE_RECONCILIATION")
                reconciliation = None
        if not reconciliation:
            missing.append("MISSING_RECONCILIATION")
        if not report:
            missing.append("MISSING_USAGE")
        elif report["completeness"] != "COMPLETE":
            missing.append("INCOMPLETE_USAGE")
        if not estimate:
            missing.append("MISSING_ESTIMATE")
        observations = reconciliation["observations"] if reconciliation else {}
        forecast = (
            {
                k: estimate[k]
                for k in (
                    "meanAtoms",
                    "p50Atoms",
                    "p80Atoms",
                    "p95Atoms",
                    "sampleCount",
                    "fallback",
                    "tail",
                    "unit",
                )
            }
            if estimate
            else None
        )
        comparison = observations.get("comparison", {})
        # Select only numeric saved-rate observations for this exact execution.
        actual = next(
            (
                x["comparisonAtoms"]
                for x in comparison.get("observations", [])
                if x["executionRef"] == executionRef
                and report
                and x["reportId"] == report["reportId"]
            ),
            None,
        )
        result = {
            "version": 1,
            "executionRef": executionRef,
            "agentRef": agent,
            "operator": operator,
            "exportedAt": str(int(time.time() if now is None else now)),
            "trust": "OPERATOR_REPORTED_UNAUTHENTICATED",
            "scope": "OWN_EXECUTION",
            "coverage": "OWN_EXECUTION_ONLY",
            "uncalibrated": True,
            "reconciliationKey": pointer["key"] if reconciliation else None,
            "reportId": report["reportId"] if report else None,
            "reportDigest": recordDigest(selected) if selected else None,
            "estimateId": estimate["estimateId"] if estimate else None,
            "estimateDigest": recordDigest(bundle) if bundle else None,
            "forecast": forecast,
            "completeness": report["completeness"] if report else None,
            "provenance": sorted({x["provenance"] for x in report["items"]}) if report else [],
            "ownReportedExecutionAtoms": observations.get("ownReportedExecutionAtoms"),
            "directChildPaidAtoms": observations.get("directChildPaidAtoms"),
            "savedRateActualAtoms": actual,
            "wholeAgentProfitAtoms": None,
            "systemUtility": None,
            "unit": {"currency": "MON", "decimals": 18},
            "missing": sorted(set(missing)),
        }
        if result["ownReportedExecutionAtoms"] is None:
            result["missing"].append("UNKNOWN_EXECUTION_COST_OR_UNIT")
        if result["directChildPaidAtoms"] is None:
            result["missing"].append("UNKNOWN_CHILD_PAYMENTS")
        if actual is None:
            result["missing"].append("MISSING_SAVED_RATE_COMPARISON")
        from modules.agent_client.application import validateOutput

        validateOutput(result, "CostDisclosure")
        ensure(len(jsonBytes(result)) <= 1048576, "Disclosure byte limit")
        return result
