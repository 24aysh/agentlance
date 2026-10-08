"""Bounded execution records sharing the existing journal's exclusive writer lock."""

from copy import deepcopy
from fractions import Fraction

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.storage.journal import executionKey
from modules.agent_client.ports import ensure
from modules.agent_client.signing import contentDigest
from modules.economics.records import checkUsage, recordDigest


class ExecutionStore:
    def __init__(self, journal, profile):
        self.journal, self.db, self.profile = journal, journal.db, deepcopy(profile)

    def get(self, kind, key):
        row = self.db.execute(
            "SELECT body FROM execution_records WHERE kind=? AND key=?", (kind, key)
        ).fetchone()
        return strictJson(row[0]) if row else None

    def rows(self, kind):
        return [
            (key, strictJson(body))
            for key, body in self.db.execute(
                "SELECT key,body FROM execution_records WHERE kind=? ORDER BY key", (kind,)
            )
        ]

    def put(self, kind, key, value, *, immutable=False):
        old = self.get(kind, key)
        ensure(not immutable or old is None or old == value, "Execution record changed", "CONFLICT")
        raw = jsonBytes(value)
        count, size = self.db.execute(
            "SELECT count(*),COALESCE(sum(length(body)),0) FROM execution_records"
        ).fetchone()
        limits = self.profile["limits"]
        ensure(
            count + (old is None) <= limits["maxRows"]
            and size - (len(jsonBytes(old)) if old else 0) + len(raw) <= limits["maxBytes"],
            "Execution storage full",
            "UNAVAILABLE",
        )
        self.db.execute(
            "INSERT INTO execution_records VALUES (?,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET body=excluded.body",
            (kind, key, raw),
        )
        return value

    def save(self, kind, key, value, **options):
        with self.db:
            return self.put(kind, key, value, **options)

    def available(self):
        return max(
            0,
            self.profile["capacity"]
            - sum(row["state"] != "RELEASED" for _, row in self.rows("reservation")),
        )

    def reserve(self, taskRef, *, imported=False, decision=None):
        key = executionKey({"taskRef": taskRef, "awardId": 1})
        with self.db:
            old = self.get("reservation", key)
            if old:
                ensure(old["state"] != "RELEASED", "Reservation already released", "CONFLICT")
                return old
            ensure(
                imported or self.available() > 0, "No execution capacity", "CAPACITY_UNAVAILABLE"
            )
            # Reserve worst-case space up front, including immutable content. Nothing is evicted.
            limits = self.profile["limits"]
            active = sum(r["state"] != "RELEASED" for _, r in self.rows("reservation")) + 1
            count, size = self.db.execute(
                "SELECT count(*),COALESCE(sum(length(body)),0) FROM execution_records"
            ).fetchone()
            used = self.db.execute("SELECT COALESCE(sum(length(raw)),0) FROM content").fetchone()[0]
            ensure(
                imported
                or (
                    count + active * (limits["maxSteps"] + 16) <= limits["maxRows"]
                    and size + active * (limits["maxSteps"] + 16) * (limits["requestBytes"] + 8192)
                    <= limits["maxBytes"]
                    and used + active * (1048576 + (limits["maxSteps"] + 4) * limits["outputBytes"])
                    <= self.journal.maxContentBytes
                ),
                "Execution retention capacity",
                "UNAVAILABLE",
            )
            return self.put(
                "reservation",
                key,
                {
                    "version": 1,
                    "taskRef": taskRef,
                    "profile": self.profile,
                    "profileDigest": recordDigest(self.profile),
                    "decision": decision,
                    "state": "HELD",
                    "evidence": None,
                    "reason": None,
                },
            )

    def reservationState(self, taskRef, state, evidence, reason=None):
        key = executionKey({"taskRef": taskRef, "awardId": 1})
        row = self.get("reservation", key)
        ensure(
            row is not None and (row["state"] != "RELEASED" or state == "RELEASED"),
            "Reservation transition",
        )
        return self.save(
            "reservation", key, row | {"state": state, "evidence": evidence, "reason": reason}
        )

    def claim(self, ref, raw, now, cutoff):
        key = executionKey(ref)
        with self.db:
            old = self.get("run", key)
            if old:
                return old
            row = self.journal.get(ref)
            ensure(
                row is not None and row["phase"] == "READY" and not row["startClaimed"],
                "Managed run requires original claim",
            )
            reservation = self.get("reservation", key)
            ensure(reservation and reservation["state"] == "COMMITTED", "Unreserved run")
            run = {
                "version": 1,
                "executionRef": ref,
                "task": row["task"],
                "bid": row["bid"],
                "profile": reservation["profile"],
                "inputDigest": contentDigest(raw),
                "startedAt": now,
                "cutoff": cutoff,
                "lastClock": now,
                "stage": "WORKING",
                "plan": None,
                "children": {},
                "depositReserved": "0",
                "gasReserved": "0",
                "nextRead": now,
                "diagnostic": None,
                "usageStatus": None,
            }
            self.journal.insertContent(run["inputDigest"], raw)
            self.put("run", key, run, immutable=True)
            row.update(phase="STARTED", startClaimed=True, detail="Managed execution")
            changed = self.db.execute(
                "UPDATE executions SET claimed=1,body=? WHERE key=? AND claimed=0",
                (jsonBytes(row), key),
            ).rowcount
            ensure(changed == 1, "Execution already claimed", "CONFLICT")
        return run

    def updateRun(self, ref, **changes):
        key = executionKey(ref)
        run = self.get("run", key)
        ensure(run is not None, "Missing managed run")
        ensure("profile" not in changes and "executionRef" not in changes, "Frozen run binding")
        if run["plan"] is not None and "plan" in changes:
            ensure(run["plan"] == changes["plan"], "Frozen plan", "CONFLICT")
        return self.save("run", key, run | deepcopy(changes))

    def steps(self, ref):
        return [s for _, s in self.rows("step") if s["executionRef"] == ref]

    def prepareStep(self, ref, stepId, kind, request, usage, now, chargeAtoms):
        key = recordDigest([ref, stepId])
        with self.db:
            old = self.get("step", key)
            digest = recordDigest(request)
            if old:
                ensure(
                    old["requestDigest"] == digest and old["kind"] == kind,
                    "Step input changed",
                    "CONFLICT",
                )
                return old
            run = self.get("run", executionKey(ref))
            profile, limits = run["profile"], run["profile"]["limits"]
            ensure(run["lastClock"] <= now < run["cutoff"], "Execution wall deadline", "TIMEOUT")
            ensure(now < int(limits["validUntil"]), "Expired charge bound", "UNAVAILABLE")
            steps = self.steps(ref)
            ensure(len(steps) < limits["maxSteps"], "Step limit", "LIMIT")
            if kind in {"MODEL", "TOOL"}:
                field = "modelCalls" if kind == "MODEL" else "toolCalls"
                ensure(sum(s["kind"] == kind for s in steps) < limits[field], "Call limit", "LIMIT")
            quantities = checkUsage(usage, profile["runtime"])
            bounds = checkUsage(profile["runtime"]["envelope"]["usage"], profile["runtime"])
            for prior in steps:
                for resource, amount in checkUsage(prior["reserved"], profile["runtime"]).items():
                    quantities[resource] += amount
            ensure(
                all(amount <= bounds[k] for k, amount in quantities.items()),
                "Resource limit",
                "LIMIT",
            )
            ensure(
                int(chargeAtoms) >= 0
                and int(chargeAtoms) + sum(int(s["chargeAtoms"]) for s in steps)
                <= int(limits["maxChargeAtoms"]),
                "Charge limit",
                "LIMIT",
            )
            row = {
                "version": 1,
                "executionRef": ref,
                "stepId": stepId,
                "kind": kind,
                "requestDigest": digest,
                "profileDigest": recordDigest(profile),
                "request": request,
                "reserved": usage,
                "chargeAtoms": str(chargeAtoms),
                "state": "PREPARED",
                "outputDigest": None,
                "usage": None,
                "diagnostic": None,
            }
            self.put("run", executionKey(ref), run | {"lastClock": now})
            return self.put("step", key, row)

    def startStep(self, step):
        key = recordDigest([step["executionRef"], step["stepId"]])
        current = self.get("step", key)
        ensure(current["state"] == "PREPARED", "Step cannot dispatch again", "UNKNOWN")
        return self.save("step", key, current | {"state": "STARTED"})

    def finishStep(self, step, *, raw=None, usage=None, state="COMPLETE", diagnostic=None):
        key = recordDigest([step["executionRef"], step["stepId"]])
        with self.db:
            current = self.get("step", key)
            ensure(current["state"] == "STARTED", "Step completion transition", "CONFLICT")
            profile = self.get("run", executionKey(step["executionRef"]))["profile"]
            if usage is not None:
                measured = checkUsage(usage, profile["runtime"])
                reserved = checkUsage(current["reserved"], profile["runtime"])
                ensure(
                    all(Fraction(0) <= q <= reserved[k] for k, q in measured.items()),
                    "Provider exceeded reserved usage",
                    "LIMIT",
                )
            digest = None
            if raw is not None:
                ensure(len(raw) <= profile["limits"]["outputBytes"], "Step output limit", "LIMIT")
                digest = contentDigest(raw)
                self.journal.insertContent(digest, raw)
            return self.put(
                "step",
                key,
                current
                | {
                    "state": state,
                    "outputDigest": digest,
                    "usage": usage,
                    "diagnostic": diagnostic,
                },
            )
