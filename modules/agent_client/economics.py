"""Durable, asynchronous candidate economics composed with the existing participant."""

import asyncio
from copy import deepcopy
from secrets import randbits

from modules.adapters.storage.economics import EconomicsStore
from modules.adapters.storage.journal import executionKey
from modules.agent_client.discovery import filterTask
from modules.agent_client.ports import (
    AdapterError,
    checkReputation,
    checkTaskView,
    closed,
    ensure,
    recordCheck,
)
from modules.economics.estimator import estimateCost
from modules.economics.history import ExecutionHistory
from modules.economics.policy import decideBid
from modules.economics.pricing import PricingCatalog
from modules.economics.records import (
    atoms,
    boundedInt,
    ceilAtoms,
    checkForecast,
    checkPolicy,
    checkRuntime,
    rational,
    recordDigest,
    textField,
)


class EconomicRuntime:
    def __init__(
        self, participant, filterPolicy, capacity, signPermit, config, clock, predictor=None
    ):
        self.participant, self.filterPolicy = participant, filterPolicy
        self.capacity, self.signPermit, self.clock = capacity, signPermit, clock
        self.config, self.predictor = deepcopy(config), predictor
        self.schema = participant.schema
        closed(config, "operator runtime pricing policy overhead forecast maxRows maxBytes")
        recordCheck(config["operator"], "Address", self.schema)
        checkRuntime(config["runtime"], self.schema)
        checkPolicy(config["policy"])
        checkForecast(config["forecast"], self.schema)
        closed(config["overhead"], "gasAtoms validationAtoms forecastAtoms expiresAt source")
        for field in ("gasAtoms", "validationAtoms", "forecastAtoms"):
            if config["overhead"][field] is not None:
                atoms(config["overhead"][field])
        atoms(config["overhead"]["expiresAt"], 64)
        textField(config["overhead"]["source"], 1024)
        PricingCatalog(config["pricing"], config["runtime"], self.schema, 0, historical=True)
        ensure(
            config["policy"]["leadTimeSeconds"] >= filterPolicy["leadTimeSeconds"],
            "Economic lead time",
        )
        boundedInt(config["maxRows"], 1)
        boundedInt(config["maxBytes"], 1024)
        self.store = EconomicsStore(
            participant.journal,
            participant.agentRef,
            config["operator"],
            maxRows=config["maxRows"],
            maxBytes=config["maxBytes"],
        )
        self.history = ExecutionHistory(self.store, self.schema, participant.market)
        self.active = None

    async def enqueueCandidate(self, task, stamp):
        recordCheck(task, "TaskSpec", self.schema)
        self.store.enqueueCandidate(task, stamp, recordDigest(self.config))

    def availableCapacity(self):
        active = sum(
            row["phase"] not in {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}
            for row in self.participant.journal.rows()
        )
        return max(0, self.capacity - active)

    async def tick(self):
        ensure(not self.participant.journal.halted(), "Market halted", "FINALITY_CONFLICT")
        if self.active is not None:
            if not self.active.done():
                return
            task, self.active = self.active, None
            task.result()
        now = int(self.clock())
        self.store.observeClock(now)
        for key, row in self.store.items("economic_candidates"):
            if row["disposition"] in {"APPLIED", "REJECTED", "EXPIRED", "ABSTAIN"}:
                continue
            if now >= int(row["nextAttemptAt"]):
                self.active = asyncio.create_task(self.processCandidate(key))
                return

    async def close(self):
        if self.active is not None:
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)
            self.active = None

    def abstain(self, key, reason):
        row = self.store.get("economic_candidates", key)
        if row["decision"] is None:
            now = int(self.clock())
            self.store.saveDecision(
                key,
                {
                    "taskRef": row["task"]["taskRef"],
                    "estimateId": None,
                    "policyDigest": recordDigest(self.config["policy"]),
                    "action": "ABSTAIN",
                    "bidAtoms": None,
                    "reasons": [reason],
                    "inputs": {},
                    "reputation": None,
                    "createdAt": str(now),
                    "expiresAt": str(now),
                },
            )
        self.store.updateCandidate(key, disposition="EXPIRED" if row["decision"] else "ABSTAIN")

    async def recoverBid(self, key, row):
        participant, ref = self.participant, row["task"]["taskRef"]
        existing = await participant.market.readBid(ref, participant.agentRef)
        if existing["bid"] is not None:
            if row["decision"] is None:
                self.store.saveDecision(
                    key,
                    {
                        "taskRef": ref,
                        "estimateId": None,
                        "policyDigest": recordDigest(self.config["policy"]),
                        "action": "BID",
                        "bidAtoms": existing["bid"]["offer"]["bidAtoms"],
                        "reasons": ["EXISTING_BID"],
                        "inputs": {},
                        "reputation": None,
                        "createdAt": str(int(self.clock())),
                        "expiresAt": row["task"]["terms"]["biddingClose"],
                    },
                )
            self.store.updateCandidate(
                key, phase="DECIDED", disposition="APPLIED", admittedBid=existing["bid"]
            )
            return True
        scope = "bid:" + executionKey({"taskRef": ref, "awardId": 1})
        intent = participant.journal.findIntent(scope)
        if intent:
            result = await participant.submitIntent(scope, intent["command"])
            self.store.updateCandidate(
                key, phase="DECIDED", disposition=result["state"], operationId=intent["operationId"]
            )
            return True
        return False

    async def processCandidate(self, key):
        now = int(self.clock())
        self.store.updateCandidate(key, nextAttemptAt=str(now + 2))
        try:
            row = self.store.get("economic_candidates", key)
            if await self.recoverBid(key, row):
                return
            if row["configDigest"] != recordDigest(self.config):
                self.abstain(key, "CONFIG_CHANGED")
                return
            task = row["task"]
            cutoff = int(task["terms"]["biddingClose"]) - self.config["policy"]["leadTimeSeconds"]
            if now >= cutoff:
                self.abstain(key, "EXPIRED")
                return
            view = await self.participant.market.readTask(task["taskRef"])
            checkTaskView(view, self.schema)
            self.participant.journal.observe(view["stamp"])
            ensure(view["task"] == task, "Candidate terms changed", "FINALITY_CONFLICT")
            eligibility = filterTask(view, self.filterPolicy, self.availableCapacity(), self.schema)
            if eligibility["state"] == "WAIT":
                return
            if eligibility["state"] != "ELIGIBLE" or int(view["stamp"]["blockTimestamp"]) >= cutoff:
                self.abstain(key, "EXPIRED")
                return
            if row["decision"] is None:
                await self.evaluateCandidate(key, row, view)
                row = self.store.get("economic_candidates", key)
            decision = row["decision"]
            if decision["action"] == "ABSTAIN":
                self.store.updateCandidate(key, disposition="ABSTAIN")
                return
            if int(self.clock()) >= int(decision["expiresAt"]) or row[
                "configDigest"
            ] != recordDigest(self.config):
                self.store.updateCandidate(key, disposition="EXPIRED")
                return
            fresh = await self.participant.market.readTask(task["taskRef"])
            if (
                filterTask(fresh, self.filterPolicy, self.availableCapacity(), self.schema)["state"]
                != "ELIGIBLE"
            ):
                return
            ensure(not self.participant.journal.halted(), "Market halted", "FINALITY_CONFLICT")

            def signFreshPermit(permit):
                # Profile fetching may outlive an estimate after the initial preflight.
                ensure(not self.participant.journal.halted(), "Market halted", "FINALITY_CONFLICT")
                self.store.observeClock(int(self.clock()))
                ensure(
                    int(self.clock()) < int(decision["expiresAt"])
                    and row["configDigest"] == recordDigest(self.config),
                    "Economic decision expired",
                    "UNAVAILABLE",
                )
                return self.signPermit(permit)

            result = await self.participant.prepareBid(
                task["taskRef"],
                decision["bidAtoms"],
                signFreshPermit,
                str(randbits(256)),
                task["terms"]["biddingClose"],
            )
            self.store.updateCandidate(
                key,
                disposition=result.get("state", "APPLIED"),
                operationId=result.get("operationId"),
            )
        except AdapterError as error:
            if error.kind in {"FINALITY_CONFLICT", "INVALID_DATA"}:
                raise
            self.store.updateCandidate(key, diagnostic=error.kind)

    async def evaluateCandidate(self, key, row, view):
        config, task = self.config, row["task"]
        runtime, pricing, policy = config["runtime"], config["pricing"], config["policy"]
        ensure(
            (
                runtime["taskFamily"],
                runtime["outputSchemaDigest"],
                runtime["validationPolicyDigest"],
            )
            == (
                task["terms"]["taskFamily"],
                task["terms"]["outputSchema"]["digest"],
                task["terms"]["validationPolicy"]["digest"],
            ),
            "Runtime task support",
        )
        raw = await self.participant.supportTask(task)
        reputation = await self.participant.market.readReputation(
            task["taskRef"], self.participant.agentRef
        )
        checkReputation(reputation, self.schema)
        ensure(
            reputation["agentRef"] == self.participant.agentRef
            and reputation["taskRef"] == task["taskRef"]
            and reputation["taskFamily"] == task["terms"]["taskFamily"]
            and reputation["snapshotBlock"] == task["reputationSnapshotBlock"],
            "Reputation candidate binding",
        )
        self.participant.journal.observe(reputation["stamp"])
        self.store.observeClock(int(self.clock()))
        evaluation = row.get("evaluation")
        if evaluation is None:
            now = int(self.clock())
            history = self.history.selectHistory(
                task,
                runtime,
                now,
                "FIXTURE"
                if view["stamp"]["source"] == "FIXTURE"
                or self.participant.journal.settings.get("mode") != "monad"
                else "LOCAL",
            )
            context = {
                "agentRef": self.participant.agentRef,
                "operator": config["operator"],
                "taskDigest": recordDigest(task),
                "configDigest": recordDigest(config),
                "history": history,
                "cutoff": str(now),
                "stamp": view["stamp"],
            }
            baseline = estimateCost(
                task,
                runtime,
                pricing,
                history,
                now,
                self.schema,
                context=context,
                leadTimeSeconds=policy["leadTimeSeconds"],
            )
            evaluation = {"now": now, "history": history, "context": context, "baseline": baseline}
            self.store.updateCandidate(key, phase="EVALUATING", evaluation=evaluation)
        now = evaluation["now"]
        baseline = evaluation["baseline"]
        if baseline["estimate"] is None:
            self.abstain(key, baseline["reason"])
            return
        if int(self.clock()) >= int(baseline["estimate"]["expiresAt"]):
            self.abstain(key, "EXPIRED")
            return
        # Other L4 work may detect a conflict while our unpaid reads are awaiting.
        ensure(not self.participant.journal.halted(), "Market halted", "FINALITY_CONFLICT")
        if row["configDigest"] != recordDigest(config):
            self.abstain(key, "CONFIG_CHANGED")
            return
        forecast = config["forecast"]
        attempt = self.store.get("forecast_attempts", key)
        forecastStatus = evaluation.get(
            "forecastStatus", "DISABLED" if not forecast["enabled"] else "UNAVAILABLE"
        )
        if (
            not evaluation.get("forecastClosed")
            and attempt is None
            and self.predictor is not None
            and forecast["enabled"]
        ):
            try:
                # Do not pay for a forecast whose overhead cannot be converted for the bid.
                if forecast["maxChargeAtoms"] is not None:
                    PricingCatalog(pricing, runtime, self.schema, int(self.clock())).convert(
                        atoms(forecast["maxChargeAtoms"]), forecast["feeUnit"]
                    )
                request = self.predictor.requestBody(raw, runtime)
                remaining = int(baseline["estimate"]["expiresAt"]) - int(self.clock())
                if remaining > 0 and self.store.reserveForecast(
                    key, forecast, recordDigest(evaluation["context"]), int(self.clock())
                ):
                    try:
                        response = await self.predictor.predict(
                            request, min(forecast["timeoutSeconds"], remaining)
                        )
                        self.store.finishForecast(key, response)
                    except (AdapterError, TimeoutError) as error:
                        self.store.finishForecast(
                            key, reason=error.kind if isinstance(error, AdapterError) else "TIMEOUT"
                        )
                else:
                    forecastStatus = "LIMIT"
            except AdapterError as error:
                if error.kind == "FINALITY_CONFLICT":
                    raise
            attempt = self.store.get("forecast_attempts", key)
        evaluation.update(forecastClosed=True, forecastStatus=forecastStatus)
        self.store.updateCandidate(key, evaluation=evaluation)
        probabilities = None
        if attempt and attempt["response"] is not None:
            probabilities = {
                k: rational(v) for k, v in attempt["response"]["probabilities"].items()
            }
        result = estimateCost(
            task,
            runtime,
            pricing,
            evaluation["history"],
            now,
            self.schema,
            probabilities=probabilities,
            context=evaluation["context"],
            leadTimeSeconds=policy["leadTimeSeconds"],
        )
        estimate = result["estimate"]
        if estimate:
            self.store.saveEstimate(
                {
                    "estimate": estimate,
                    "baseline": baseline["estimate"],
                    "provenance": result["provenance"]
                    | {"forecastStatus": attempt["state"] if attempt else forecastStatus},
                    "pricing": pricing,
                }
            )
        overhead = deepcopy(config["overhead"])
        if attempt:
            catalog = PricingCatalog(pricing, runtime, self.schema, int(self.clock()))
            if overhead["forecastAtoms"] is not None:
                overhead["forecastAtoms"] = str(
                    atoms(overhead["forecastAtoms"])
                    + ceilAtoms(catalog.convert(atoms(attempt["reservedAtoms"]), attempt["unit"]))
                )
        decision = decideBid(
            task, estimate, reputation, overhead, policy, int(self.clock()), self.schema
        )
        self.store.saveDecision(key, decision)
