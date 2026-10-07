"""Bounded private economics storage inside the participant's locked SQLite journal."""

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import ensure
from modules.economics.records import atoms, boundedInt, recordDigest, sortedRecord

TABLES = (
    "economic_candidates",
    "economic_estimates",
    "forecast_attempts",
    "usage_reports",
    "usage_active",
    "usage_receipts",
    "economic_meta",
)


class EconomicsStore:
    def __init__(self, journal, agentRef, operator, *, maxRows=10000, maxBytes=16 * 1024 * 1024):
        self.journal, self.db = journal, journal.db
        self.agentRef, self.operator = agentRef, operator
        ensure(
            journal.settings.get("agentRef", agentRef) == agentRef,
            "Economics identity binding",
            "CONFLICT",
        )
        self.maxRows, self.maxBytes = boundedInt(maxRows, 1), boundedInt(maxBytes, 1024)
        with self.db:
            self.put(
                "economic_meta",
                "binding",
                {"agentRef": agentRef, "operator": operator},
                immutable=True,
            )
            for key, attempt in self.items("forecast_attempts"):
                if attempt["state"] == "STARTED":
                    self.put("forecast_attempts", key, attempt | {"state": "UNKNOWN"})

    def get(self, table, key):
        ensure(table in TABLES, "Economics table")
        row = self.db.execute(f"SELECT body FROM {table} WHERE key=?", (key,)).fetchone()
        return strictJson(row[0]) if row else None

    def items(self, table):
        ensure(table in TABLES, "Economics table")
        return [
            (key, strictJson(body))
            for key, body in self.db.execute(f"SELECT key,body FROM {table} ORDER BY key")
        ]

    def put(self, table, key, body, *, immutable=False):
        old = self.get(table, key)
        if old is not None and immutable:
            ensure(old == body, "Immutable economics record conflict", "CONFLICT")
            return
        raw = jsonBytes(sortedRecord(body))
        rows, size = 0, 0
        for selected in TABLES:
            count, length = self.db.execute(
                f"SELECT count(*),coalesce(sum(length(body)),0) FROM {selected}"
            ).fetchone()
            rows, size = rows + count, size + length
        ensure(
            rows + (old is None) <= self.maxRows
            and size - (len(jsonBytes(old)) if old else 0) + len(raw) <= self.maxBytes,
            "Economics storage capacity",
            "UNAVAILABLE",
        )
        self.db.execute(
            f"INSERT INTO {table} VALUES (?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
            (key, raw),
        )

    def observeClock(self, now):
        boundedInt(now, 0, 2**64 - 1)
        with self.db:
            last = self.get("economic_meta", "clock")
            ensure(
                last is None or now >= int(last["now"]),
                "Economics clock moved backwards",
                "UNAVAILABLE",
            )
            self.put("economic_meta", "clock", {"now": str(now)})

    def candidateKey(self, taskRef):
        return recordDigest([self.agentRef, taskRef])

    def enqueueCandidate(self, task, stamp, configDigest):
        key = self.candidateKey(task["taskRef"])
        with self.db:
            old = self.get("economic_candidates", key)
            if old:
                ensure(old["task"] == task, "Candidate terms changed", "CONFLICT")
                return key
            self.put(
                "economic_candidates",
                key,
                {
                    "task": task,
                    "stamp": stamp,
                    "configDigest": configDigest,
                    "phase": "QUEUED",
                    "decision": None,
                    "disposition": None,
                    "operationId": None,
                    "nextAttemptAt": "0",
                },
            )
        return key

    def updateCandidate(self, key, **changes):
        with self.db:
            row = self.get("economic_candidates", key)
            ensure(row is not None, "Candidate missing")
            if row["decision"] is not None:
                ensure(
                    "decision" not in changes or changes["decision"] == row["decision"],
                    "Decision immutable",
                    "CONFLICT",
                )
            self.put("economic_candidates", key, row | changes)

    def reserveForecast(self, key, config, contextDigest, now):
        self.observeClock(now)
        with self.db:
            old = self.get("forecast_attempts", key)
            if old:
                return False
            if (
                not config["enabled"]
                or config["maxChargeAtoms"] is None
                or not config["chargeBoundSource"]
                or now >= int(config["validUntil"])
            ):
                return False
            self.put("economic_meta", "feeUnit", config["feeUnit"], immutable=True)
            attempts = [row for _, row in self.items("forecast_attempts")]
            day = now // 86400
            daily = [row for row in attempts if row["day"] == day]
            maximum = atoms(config["maxChargeAtoms"])
            if (
                any(row["state"] == "STARTED" for row in attempts)
                or len(daily) >= config["dailyCalls"]
                or maximum > atoms(config["taskBudgetAtoms"])
                or sum(atoms(row["reservedAtoms"]) for row in daily) + maximum
                > atoms(config["dailyBudgetAtoms"])
            ):
                return False
            self.put(
                "forecast_attempts",
                key,
                {
                    "requestId": recordDigest(["forecast-v1", key]),
                    "contextDigest": contextDigest,
                    "state": "STARTED",
                    "day": day,
                    "createdAt": str(now),
                    "reservedAtoms": str(maximum),
                    "unit": config["feeUnit"],
                    "actualCostAtoms": None,
                    "response": None,
                    "responseDigest": None,
                    "reason": None,
                    "expenseId": recordDigest(["forecast-expense-v1", key]),
                },
            )
            return True

    def finishForecast(self, key, response=None, reason=None):
        with self.db:
            row = self.get("forecast_attempts", key)
            ensure(
                row is not None and row["state"] == "STARTED",
                "Forecast already completed",
                "CONFLICT",
            )
            self.put(
                "forecast_attempts",
                key,
                row
                | {
                    "state": "COMPLETE" if response is not None else "UNKNOWN",
                    "response": response,
                    "responseDigest": recordDigest(response) if response is not None else None,
                    "reason": reason,
                },
            )

    def saveEstimate(self, bundle):
        with self.db:
            self.put("economic_estimates", bundle["estimate"]["estimateId"], bundle, immutable=True)

    def saveDecision(self, key, decision):
        candidate = self.get("economic_candidates", key)
        ensure(candidate is not None, "Candidate missing")
        context = candidate.get("evaluation", {}).get("context") or {
            "agentRef": self.agentRef,
            "operator": self.operator,
            "taskDigest": recordDigest(candidate["task"]),
            "configDigest": candidate["configDigest"],
            "stamp": candidate["stamp"],
        }
        body = decision | {"agentRef": self.agentRef, "contextDigest": recordDigest(context)}
        body["decisionId"] = recordDigest(["layer5-decision-v1", body])
        self.updateCandidate(key, phase="DECIDED", decision=body)
        return body

    def saveReport(self, report, context, supersedesReportId):
        key = recordDigest([context["operator"], report["reportId"]])
        scope = recordDigest([context["operator"], report["executionRef"]])
        row = {"report": report, "context": context, "supersedesReportId": supersedesReportId}
        with self.db:
            old = self.get("usage_reports", key)
            if old:
                ensure(old == row, "Conflicting report replay", "CONFLICT")
                return "UNCHANGED"
            active = self.get("usage_active", scope)
            ensure(
                (active is None and supersedesReportId is None)
                or (active is not None and active["reportId"] == supersedesReportId),
                "Correction must name active report",
                "CONFLICT",
            )
            for _, stored in self.items("usage_reports"):
                if stored["context"]["operator"] != context["operator"]:
                    continue
                expenses = {item["expenseId"]: item for item in stored["report"]["items"]}
                ensure(
                    stored["report"]["executionRef"] == report["executionRef"]
                    or not any(item["expenseId"] in expenses for item in report["items"]),
                    "Expense already attributed to another execution",
                    "CONFLICT",
                )
                ensure(
                    all(
                        item["expenseId"] not in expenses or expenses[item["expenseId"]] == item
                        for item in report["items"]
                    ),
                    "Conflicting expense",
                    "CONFLICT",
                )
            self.put("usage_reports", key, row, immutable=True)
            self.put("usage_active", scope, {"key": key, "reportId": report["reportId"]})
        return "STORED"

    def activeReports(self):
        return [self.get("usage_reports", row["key"]) for _, row in self.items("usage_active")]
