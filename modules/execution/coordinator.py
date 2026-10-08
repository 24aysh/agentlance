"""Durable local orchestration; all market authority remains with Participant."""

import asyncio
import time
from copy import deepcopy
from pathlib import Path

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.storage.economics import EconomicsStore
from modules.adapters.storage.execution import ExecutionStore
from modules.adapters.storage.journal import executionKey
from modules.agent_client.participant import makeCommand
from modules.agent_client.ports import AdapterError, checkTaskView, ensure
from modules.economics.history import ExecutionHistory
from modules.economics.pricing import PricingCatalog
from modules.economics.records import MON, ceilAtoms, rational, recordDigest
from modules.execution.rules import (
    TERMINAL,
    checkOutput,
    checkProfile,
    quantities,
    validateChildPlan,
)
from modules.market_core.transitions import CUTOFFS


class ExecutionCoordinator:
    def __init__(self, participant, profile, docker, agents=None, *, clock=time.time, history=None):
        checkProfile(profile, participant.schema)
        self.participant, self.journal = participant, participant.journal
        self.profile, self.clock = deepcopy(profile), clock
        self.store = ExecutionStore(self.journal, profile)
        self.docker, self.agents, self.jobs = docker, agents, {}
        # Independent local chains may share protocol references, but not private containers.
        databasePath = self.journal.db.execute("PRAGMA database_list").fetchone()[2]
        self.docker.namespace = recordDigest([databasePath, self.journal.settings])
        self.fundsLock = asyncio.Lock()
        self.monotonicCutoffs = {}
        self.readyImages = set()
        self.history = history or ExecutionHistory(
            EconomicsStore(self.journal, participant.agentRef, profile["operator"]),
            participant.schema,
            participant.market,
        )
        participant.coordinator = self
        self.reconstruct()

    def reconstruct(self):
        # Retained duties precede any new admission, including legacy records over capacity.
        for (raw,) in self.journal.db.execute("SELECT body FROM operations"):
            command = strictJson(raw)["command"]
            if command["command"] == "submitBid":
                ref = command["input"]["offer"]["taskRef"]
                key = executionKey({"taskRef": ref, "awardId": 1})
                if self.store.get("reservation", key) is None:
                    self.store.reserve(ref, imported=True)
        for row in self.journal.rows():
            ref = row["extension"]["executionRef"]["taskRef"]
            if (
                self.store.get("reservation", executionKey(row["extension"]["executionRef"]))
                is None
            ):
                self.store.reserve(ref, imported=True)

    async def reserveBid(self, task, bidAtoms):
        ensure(not self.journal.halted(), "Market halted", "FINALITY_CONFLICT")
        key = executionKey({"taskRef": task["taskRef"], "awardId": 1})
        saved = self.store.get("reservation", key)
        profile = saved["profile"] if saved else self.profile
        ensure(
            int(profile["delegation"]["ownWorkReserveAtoms"]) <= int(bidAtoms),
            "Own reserve exceeds bid",
            "UNAVAILABLE",
        )
        ensure(
            int(self.clock()) < int(profile["limits"]["validUntil"]),
            "Expired charge bound",
            "UNAVAILABLE",
        )
        PricingCatalog(
            profile["pricing"], profile["runtime"], self.participant.schema, int(self.clock())
        )
        executor = self.executor(profile)
        digest = recordDigest(profile)
        if digest not in self.readyImages:
            await executor.preflight(profile)
            self.readyImages.add(digest)
        decision = None
        for _, candidate in self.history.store.items("economic_candidates"):
            if candidate["task"]["taskRef"] == task["taskRef"]:
                decision = candidate["decision"]
        if decision and decision["estimateId"]:
            estimate = self.history.store.get("economic_estimates", decision["estimateId"])
            ensure(
                estimate["estimate"]["runtimeDigest"] == recordDigest(profile["runtime"]),
                "Bid runtime differs from executable profile",
                "CONFLICT",
            )
        return self.store.reserve(task["taskRef"], decision=decision)

    def executor(self, profile):
        if profile["executor"]["kind"] == "AGENTS":
            ensure(self.agents is not None, "Agents adapter unavailable", "UNAVAILABLE")
            return self.agents
        return self.docker

    def admit(self, view):
        ref = view["task"]["taskRef"]
        row = self.store.reserve(ref)
        reserve = (
            view["ownWorkReserveAtoms"]
            if view["status"] == "RUNNING"
            else row["profile"]["delegation"]["ownWorkReserveAtoms"]
        )
        ensure(
            int(reserve) <= int(view["allocation"]["reservedAtoms"]), "Own reserve exceeds award"
        )
        self.store.reservationState(ref, "COMMITTED", view["stamp"])
        return reserve

    async def observeReservations(self):
        for key, reservation in self.store.rows("reservation"):
            if reservation["state"] == "RELEASED":
                continue
            ref = reservation["taskRef"]
            row = self.journal.get({"taskRef": ref, "awardId": 1})
            if row and row["phase"] in TERMINAL and key not in self.jobs:
                if any(
                    s["state"] == "STARTED"
                    for s in self.store.steps({"taskRef": ref, "awardId": 1})
                ):
                    continue
                self.store.reservationState(ref, "RELEASED", row["stamp"], row["phase"])
                continue
            view = await self.participant.market.readTask(ref)
            checkTaskView(view, self.participant.schema)
            self.journal.observe(view["stamp"])
            if view["stamp"]["finality"] != "FINALIZED":
                continue
            intent = self.journal.findIntent("bid:" + key)
            if intent and intent["attempted"]:
                result = await self.participant.market.readOperation(intent["operationId"])
                if result["state"] in {"PENDING", "UNKNOWN"}:
                    continue
            own = (
                view["winningBid"] is not None
                and view["winningBid"]["offer"]["agentRef"] == self.participant.agentRef
            )
            if own:
                self.store.reservationState(ref, "COMMITTED", view["stamp"])
            elif view["status"] != "OPEN" or (
                not intent
                and int(view["stamp"]["blockTimestamp"])
                >= int(view["task"]["terms"]["biddingClose"])
            ):
                self.store.reservationState(ref, "RELEASED", view["stamp"], "NO_OWN_DUTY")

    def startExecution(self, ref, raw):
        key, now = executionKey(ref), int(self.clock())
        if self.store.get("run", key) is None:
            row = self.journal.get(ref)
            profile = self.store.get("reservation", key)["profile"]
            cutoff = min(
                now + profile["limits"]["wallSeconds"],
                int(row["task"]["terms"]["resultBy"]) - profile["limits"]["commitSeconds"],
            )
            ensure(now < cutoff, "Insufficient execution window", "TIMEOUT")
            self.journal.update(ref, phase="READY")
            self.store.claim(ref, raw, now, cutoff)
        run = self.store.get("run", key)
        if key not in self.jobs and now >= run["nextRead"]:
            self.store.updateRun(ref, nextRead=now + run["profile"]["limits"]["pollSeconds"])
            self.jobs[key] = asyncio.create_task(self.advanceRun(ref))

    async def authorize(self, ref):
        ensure(not self.journal.halted(), "Market halted", "FINALITY_CONFLICT")
        run = self.store.get("run", executionKey(ref))
        now = int(self.clock())
        cutoff = self.monotonicCutoffs.setdefault(
            executionKey(ref), time.monotonic() + max(0, run["cutoff"] - now)
        )
        ensure(time.monotonic() < cutoff, "Monotonic execution deadline", "TIMEOUT")
        ensure(
            run["lastClock"] <= now < run["cutoff"],
            "Execution deadline or clock rollback",
            "TIMEOUT",
        )
        async with asyncio.timeout(10):
            view = await self.participant.market.observeAward(ref)
        self.participant.checkAward(self.journal.get(ref)["extension"], view, self.journal.get(ref))
        ensure(
            view["stamp"]["finality"] == "FINALIZED"
            and view["status"] == "RUNNING"
            and view["ownWorkReserveAtoms"] == self.journal.get(ref)["ownWorkReserveAtoms"]
            and int(view["stamp"]["blockTimestamp"]) < int(view["task"]["terms"]["resultBy"]),
            "No active accepted obligation",
            "STOPPED",
        )
        self.store.updateRun(ref, lastClock=now)
        return view

    async def invoke(self, ref, stepId, kind, request, reserved, call):
        step = self.store.get("step", recordDigest([ref, stepId]))
        if step:
            ensure(
                step["requestDigest"] == recordDigest(request), "Step request changed", "CONFLICT"
            )
            if step["state"] == "COMPLETE":
                return self.journal.readContent(step["outputDigest"])
            ensure(step["state"] == "PREPARED", "Ambiguous/failed step cannot repeat", "UNKNOWN")
        await self.authorize(ref)
        run = self.store.get("run", executionKey(ref))
        profile = run["profile"]
        charge = "0" if kind == "WORK" else profile["limits"]["callChargeAtoms"]
        catalog = PricingCatalog(
            profile["pricing"], profile["runtime"], self.participant.schema, int(self.clock())
        )
        ensure(
            catalog.priceUsage(reserved) <= int(charge),
            "Call charge bound below resource price",
            "LIMIT",
        )
        step = self.store.prepareStep(
            ref, stepId, kind, request, reserved, int(self.clock()), charge
        )
        step = self.store.startStep(step)
        try:
            async with asyncio.timeout(
                min(
                    profile["limits"]["requestSeconds"] * max(1, profile["limits"]["modelTurns"]),
                    run["cutoff"] - int(self.clock()),
                )
            ):
                raw, usage = await call()
            self.store.finishStep(step, raw=raw, usage=usage)
            return raw
        except BaseException as error:
            self.store.finishStep(step, state="UNKNOWN", diagnostic=type(error).__name__)
            raise

    async def runStep(self, ref, stepId, request):
        run = self.store.get("run", executionKey(ref))
        profile = run["profile"]
        sdk = profile["executor"]["kind"] == "AGENTS"
        reserved = quantities(profile, transform=0 if sdk else 1)

        async def call():
            if sdk:

                async def invoke(stepId, kind, request, usage, call):
                    return await self.invoke(ref, stepId, kind, request, usage, call)

                raw = await self.agents.run(ref, stepId, request, profile, invoke)
            else:
                raw = await self.docker.run(ref, stepId, request, profile)
            return raw, reserved

        try:
            return await self.invoke(
                ref, stepId, "WORK" if sdk else "TOOL", request, reserved, call
            )
        finally:
            if not sdk:
                await self.docker.stop(ref, stepId)

    def proposal(self, ref, view, profile, raw):
        value = strictJson(raw)
        plan = {
            "version": 1,
            "executionRef": ref,
            "profileDigest": recordDigest(profile),
            "mode": "SOLO",
            "slots": [],
        }
        if not profile["delegation"]["enabled"]:
            return plan
        ensure(set(value) == {"left", "right"}, "Reference manager template", "UNSUPPORTED")
        fixture = Path(__file__).resolve().parents[2] / "specs/fixtures/layer-2"
        schemaRef = self.participant.content.publishContent(
            (fixture / "output-schema.json").read_bytes()
        )
        policyRef = self.participant.content.publishContent((fixture / "policy.json").read_bytes())
        now, end = int(view["stamp"]["blockTimestamp"]), int(view["task"]["terms"]["resultBy"])
        plan["mode"] = "DELEGATE"
        for slot in ("left", "right"):
            childInput = {"value": value[slot]}
            terms = deepcopy(view["task"]["terms"])
            terms.update(
                input=self.participant.content.publishContent(jsonBytes(childInput)),
                outputSchema=schemaRef,
                validationPolicy=policyRef,
                budgetAtoms=profile["delegation"]["childBudgetAtoms"],
                refundAddress=self.participant.signer,
                retryOf=None,
                biddingClose=str(now + 120),
                allocationBy=str(now + 240),
                acceptBy=str(now + 360),
                resultBy=str(end - 300),
                validationBy=str(end - 180),
                delegation={"maxDepth": terms["delegation"]["maxDepth"], "maxChildren": 0},
            )
            plan["slots"].append(
                {"id": slot, "terms": terms, "input": childInput, "fallbackInput": childInput}
            )
        return plan

    async def freezePlan(self, ref, plan, view):
        run = self.store.get("run", executionKey(ref))
        profile = run["profile"]
        validateChildPlan(plan, view, profile, self.participant.schema, self.participant.signer)
        for slot in plan["slots"]:
            terms = slot["terms"]
            ensure(
                terms["input"] == self.participant.content.publishContent(jsonBytes(slot["input"])),
                "Child input must be published by this coordinator",
            )
            schemaRaw = await self.participant.content.fetchBytes(
                terms["outputSchema"]["uri"], 65536, terms["outputSchema"]["digest"]
            )
            policyRaw = await self.participant.content.fetchBytes(
                terms["validationPolicy"]["uri"], 65536, terms["validationPolicy"]["digest"]
            )
            from modules.agent_client.ports import recordCheck

            recordCheck(strictJson(schemaRaw), "OutputShape", self.participant.schema)
            recordCheck(strictJson(policyRaw), "ValidationPolicy", self.participant.schema)
            ensure(
                strictJson(policyRaw)["outputSchemaDigest"] == terms["outputSchema"]["digest"],
                "Child policy binding",
            )
        deposit = sum(int(s["terms"]["budgetAtoms"]) for s in plan["slots"])
        async with self.fundsLock:
            gas = int(profile["delegation"]["gasAtoms"]) if deposit else 0
            if deposit:
                balance = await self.participant.market.availableFunds()
                held = sum(self.pendingFunds(other) for _, other in self.store.rows("run"))
                ensure(
                    balance >= held + deposit + gas,
                    "Fresh child funding unavailable",
                    "UNAVAILABLE",
                )
            self.store.updateRun(ref, plan=plan, depositReserved=str(deposit), gasReserved=str(gas))

    @staticmethod
    def pendingFunds(run):
        if run["plan"] is None or run.get("fundsReleased", False):
            return 0
        # Subtract only creations reflected in the pending wallet balance; reserve the rest.
        sent = sum(
            int(s["terms"]["budgetAtoms"])
            for s in run["plan"]["slots"]
            if run["children"].get(s["id"], {}).get("taskRef")
        )
        return max(0, int(run["depositReserved"]) - sent) + int(run["gasReserved"])

    async def advanceRun(self, ref):
        try:
            # A managed STARTED external effect after process loss is never repeated.
            for step in self.store.steps(ref):
                if step["state"] == "STARTED":
                    await self.docker.stop(ref, step["stepId"])
                    self.store.finishStep(step, state="UNKNOWN", diagnostic="Process lost")
            ensure(
                not any(s["state"] == "UNKNOWN" for s in self.store.steps(ref)),
                "Interrupted external effect",
                "UNKNOWN",
            )
            view = await self.authorize(ref)
            run = self.store.get("run", executionKey(ref))
            profile = run["profile"]
            raw = self.journal.readContent(run["inputDigest"])
            if run["plan"] is None:
                previousPlan = self.store.get("step", recordDigest([ref, "plan"]))
                proposal = (
                    previousPlan["request"]["proposal"]
                    if previousPlan
                    else self.proposal(ref, view, profile, raw)
                )
                if profile["delegation"]["enabled"]:
                    output = await self.runStep(
                        ref,
                        "plan",
                        {"kind": "PLAN", "input": strictJson(raw), "proposal": proposal},
                    )
                    try:
                        plan = strictJson(output)
                        await self.freezePlan(ref, plan, await self.authorize(ref))
                    except AdapterError:
                        if not profile["delegation"]["fallback"]:
                            raise
                        await self.freezePlan(
                            ref, proposal | {"mode": "SOLO", "slots": []}, await self.authorize(ref)
                        )
                else:
                    await self.freezePlan(ref, proposal, view)
                run = self.store.get("run", executionKey(ref))
            if run["plan"]["mode"] == "SOLO":
                output = await self.runStep(ref, "solo", {"kind": "SOLO", "input": strictJson(raw)})
            else:
                outputs = await self.advanceChildren(ref)
                if outputs is None:
                    return
                self.store.updateRun(ref, stage="SYNTHESIZING")
                output = await self.runStep(
                    ref, "synthesize", {"kind": "SYNTHESIZE", "input": outputs}
                )
            shapeRef = run["task"]["terms"]["outputSchema"]
            shape = await self.participant.content.fetchBytes(
                shapeRef["uri"], 65536, shapeRef["digest"]
            )
            checkOutput(output, strictJson(shape), profile["limits"]["outputBytes"])
            await self.authorize(ref)
            self.participant.content.publishResult(ref, output)
            self.store.updateRun(ref, stage="ARTIFACT_READY", usageStatus="COMPLETED")
        except (AdapterError, TimeoutError, ValueError) as error:
            await self.stopRun(
                ref,
                "TIMED_OUT"
                if isinstance(error, TimeoutError) or getattr(error, "kind", None) == "TIMEOUT"
                else "ABORTED",
                str(error),
            )
        except asyncio.CancelledError:
            run = self.store.get("run", executionKey(ref))
            status = "TIMED_OUT" if int(self.clock()) >= run["cutoff"] else "ABORTED"
            await self.stopRun(ref, status, "Execution cancelled")
            raise
        except Exception as error:
            await self.stopRun(ref, "ABORTED", type(error).__name__)
        finally:
            run = self.store.get("run", executionKey(ref))
            if run["usageStatus"] is not None:
                self.queueUsage(ref)

    async def stopRun(self, ref, status, diagnostic):
        for step in self.store.steps(ref):
            await self.docker.stop(ref, step["stepId"])
            if step["state"] == "STARTED":
                self.store.finishStep(step, state="UNKNOWN", diagnostic=diagnostic)
        row = self.journal.get(ref)
        if row["phase"] not in TERMINAL and row["result"] is None:
            self.journal.update(ref, phase="INTERRUPTED", detail=diagnostic)
        self.store.updateRun(ref, stage="INTERRUPTED", usageStatus=status, diagnostic=diagnostic)

    async def publishSlot(self, ref, slot, run):
        slotId, profile = slot["id"], run["profile"]
        progress = run["children"].get(slotId, {})
        requestId = recordDigest(["l6-child-v1", ref, recordDigest(run["plan"]), slotId])
        intent = self.journal.findIntent("child:" + requestId)
        if not intent:
            view = await self.authorize(ref)
            now = max(int(self.clock()), int(view["stamp"]["blockTimestamp"]))
            ensure(
                now + profile["delegation"]["publishSeconds"] < int(slot["terms"]["biddingClose"]),
                "Child publication cutoff",
                "TIMEOUT",
            )
            pending = [
                s["terms"]
                for s in run["plan"]["slots"]
                if s["id"] != slotId
                and run["children"].get(s["id"], {}).get("attempted")
                and not run["children"].get(s["id"], {}).get("taskRef")
            ]
            validateChildPlan(
                run["plan"] | {"slots": [slot]},
                view,
                profile,
                self.participant.schema,
                self.participant.signer,
                pending,
            )
        self.reserveGas(ref, "child:" + requestId)
        progress.update(requestId=requestId, attempted=True)
        self.store.updateRun(ref, children=run["children"] | {slotId: progress})
        try:
            result = await self.participant.publishChild(ref["taskRef"], slot["terms"], requestId)
        except AdapterError as error:
            if error.kind == "CONFLICT" and error.detail == "Sender has an unresolved transaction":
                return progress
            raise
        progress["operationId"] = result["operationId"]
        if result["state"] == "APPLIED" and result["stamp"]["finality"] == "FINALIZED":
            self.journal.observe(result["stamp"])
            tasks = [
                event["payload"]["task"]
                for event in result["events"]
                if event["name"] == "TaskCreated"
            ]
            ensure(
                len(tasks) == 1
                and tasks[0]["parentRef"] == ref["taskRef"]
                and tasks[0]["terms"] == slot["terms"],
                "Child creation binding",
            )
            progress.update(taskRef=tasks[0]["taskRef"], task=tasks[0])
        elif result["state"] == "REJECTED":
            progress["missing"] = result["error"]
        self.store.updateRun(ref, children=run["children"] | {slotId: progress})
        return progress

    def reserveGas(self, ref, scope):
        if self.store.get("progress", scope):
            return
        run = self.store.get("run", executionKey(ref))
        market = self.participant.market
        gas = getattr(market, "maxGas", 0) * getattr(market, "maxGasPriceWei", 0)
        spent = sum(
            int(r["gas"]) for _, r in self.store.rows("progress") if r["executionRef"] == ref
        )
        ensure(gas > 0 and spent + gas <= int(run["gasReserved"]), "Child gas allowance", "LIMIT")
        self.store.save("progress", scope, {"executionRef": ref, "gas": str(gas)}, immutable=True)

    async def progressChild(self, ref, child):
        if child["status"] == "SETTLED":
            return
        terms, now = child["task"]["terms"], int(child["stamp"]["blockTimestamp"])
        action = None
        if child["status"] == "OPEN" and int(terms["biddingClose"]) <= now < int(
            terms["allocationBy"]
        ):
            action = "allocateTask"
        elif now >= int(terms[CUTOFFS[child["status"]][0]]):
            action = "expireTask"
        if not action:
            return
        scope = "child-progress:" + recordDigest(
            [ref, child["task"]["taskRef"], child["status"], action]
        )
        intent = self.journal.findIntent(scope)
        if not intent:
            self.reserveGas(ref, scope)
        try:
            await self.participant.submitIntent(
                scope, makeCommand(action, taskRef=child["task"]["taskRef"])
            )
        except AdapterError as error:
            if error.kind != "CONFLICT":
                raise
            # Another caller may have progressed the same child; never invent a terminal result.
            await self.participant.market.readTask(child["task"]["taskRef"])

    async def advanceChildren(self, ref, cleanup=False):
        run = self.store.get("run", executionKey(ref))
        outputs, pending = {}, False
        for slot in run["plan"]["slots"]:
            run = self.store.get("run", executionKey(ref))
            progress = run["children"].get(slot["id"], {})
            if not progress.get("taskRef") and not progress.get("missing"):
                requestId = recordDigest(
                    ["l6-child-v1", ref, recordDigest(run["plan"]), slot["id"]]
                )
                intent = self.journal.findIntent("child:" + requestId)
                native = self.journal.nativeOperation(intent["operationId"]) if intent else None
                provedUnsent = getattr(self.participant.market, "recoverUnknown", False) and (
                    native is None or native["transactionHash"] is None
                )
                if cleanup and (not progress.get("attempted") or provedUnsent):
                    progress["missing"] = "PARENT_STOPPED"
                    self.store.updateRun(ref, children=run["children"] | {slot["id"]: progress})
                    continue
                try:
                    progress = await self.publishSlot(ref, slot, run)
                except AdapterError as error:
                    if error.kind not in {"TIMEOUT", "INVALID_PLAN", "LIMIT"} or progress.get(
                        "attempted"
                    ):
                        raise
                    progress["missing"] = error.kind
            if progress.get("taskRef"):
                child = await self.participant.market.readTask(progress["taskRef"])
                checkTaskView(child, self.participant.schema)
                self.journal.observe(child["stamp"])
                ensure(
                    child["task"] == progress["task"]
                    and child["task"]["parentRef"] == ref["taskRef"]
                    and child["task"]["rootRef"] == run["task"]["rootRef"],
                    "Child lineage",
                )
                if child["stamp"]["finality"] != "FINALIZED":
                    pending = True
                    continue
                if child["status"] != "SETTLED":
                    await self.progressChild(ref, child)
                    pending = True
                    continue
                receipt = child["receipt"]
                progress["receipt"] = receipt
                if (
                    receipt["reason"] == "SUCCESS"
                    and not progress.get("outputDigest")
                    and not cleanup
                ):
                    try:
                        artifact = child["result"]["artifact"]
                        ensure(
                            receipt["resultDigest"] == artifact["digest"]
                            and receipt["winner"] == child["winningBid"]["offer"]["agentRef"]
                            and receipt["validation"]["validationPolicyDigest"]
                            == slot["terms"]["validationPolicy"]["digest"]
                            and receipt["validation"]["verdict"] == "PASS"
                            and receipt["validation"]["validator"]
                            == run["task"]["terms"]["validator"]
                            and child["result"]["executionRef"]
                            == {"taskRef": progress["taskRef"], "awardId": 1},
                            "Child success binding",
                        )
                        raw = await self.participant.content.fetchBytes(
                            artifact["uri"], 1048576, artifact["digest"]
                        )
                        shapeRef = slot["terms"]["outputSchema"]
                        shape = await self.participant.content.fetchBytes(
                            shapeRef["uri"], 65536, shapeRef["digest"]
                        )
                        checkOutput(raw, strictJson(shape))
                        progress["outputDigest"] = self.journal.storeContent(raw)
                    except AdapterError as error:
                        progress["fetchAttempts"] = progress.get("fetchAttempts", 0) + 1
                        if progress["fetchAttempts"] < run["profile"]["limits"]["readAttempts"]:
                            pending = True
                        else:
                            progress["missing"] = error.kind
                elif receipt["reason"] != "SUCCESS":
                    progress["missing"] = receipt["reason"]
            self.store.updateRun(ref, children=run["children"] | {slot["id"]: progress})
            if cleanup:
                continue
            if progress.get("outputDigest"):
                outputs[slot["id"]] = strictJson(self.journal.readContent(progress["outputDigest"]))
            elif progress.get("missing"):
                ensure(
                    run["profile"]["delegation"]["fallback"],
                    "Missing child and fallback disabled",
                    "STOPPED",
                )
                raw = await self.runStep(
                    ref,
                    "fallback:" + slot["id"],
                    {"kind": "FALLBACK", "input": slot["fallbackInput"]},
                )
                outputs[slot["id"]] = strictJson(raw)
            else:
                pending = True
        current = self.store.get("run", executionKey(ref))
        if all(
            (
                current["children"].get(slot["id"], {}).get("receipt") is not None
                or current["children"].get(slot["id"], {}).get("missing")
            )
            for slot in current["plan"]["slots"]
        ):
            self.store.updateRun(ref, fundsReleased=True)
        if cleanup:
            return None
        view = await self.authorize(ref)
        self.store.updateRun(ref, stage="WAITING_CHILDREN")
        return None if pending or view["activeChildren"] else outputs

    def queueUsage(self, ref):
        key = executionKey(ref)
        if self.store.get("outbox", key):
            return
        run, steps = self.store.get("run", key), self.store.steps(ref)
        profile = run["profile"]
        catalog = PricingCatalog(
            profile["pricing"], profile["runtime"], self.participant.schema, 0, historical=True
        )
        # A run with no steps proves that no local call started, but it is not an
        # execution-cost sample. Keep it out of L5 history while retaining the
        # explicit zero-usage observation.
        items, incomplete = [], not steps
        for step in steps:
            if step["kind"] == "WORK":
                continue
            incomplete |= step["usage"] is None
            measured = {
                (u["resource"], u["quantityUnit"]): u["quantity"] for u in (step["usage"] or [])
            }
            for resource in profile["runtime"]["resources"]:
                quantity = measured.get((resource["resource"], resource["quantityUnit"]))
                cost = None
                if quantity is not None:
                    rate, unit = catalog.rates[(resource["resource"], resource["quantityUnit"])]
                    cost = str(ceilAtoms(catalog.convert(rate * rational(quantity), unit)))
                items.append(
                    resource
                    | {
                        "expenseId": recordDigest([ref, step["stepId"], resource]),
                        "category": "EXECUTION",
                        "quantity": quantity,
                        "costAtoms": cost,
                        "unit": MON,
                        "provenance": "INCOMPLETE" if quantity is None else "OPERATOR_REPORTED",
                        "providerRef": None,
                    }
                )
        if not items and steps:
            incomplete = any(step["state"] != "COMPLETE" for step in steps)
        reservation = self.store.get("reservation", key)
        decision = reservation["decision"]
        report = {
            "schemaVersion": 1,
            "reportId": recordDigest(["execution-usage-v1", ref]),
            "executionRef": ref,
            "estimateId": decision["estimateId"] if decision else None,
            "observedAt": str(int(self.clock())),
            "completeness": "INCOMPLETE" if incomplete else "COMPLETE",
            "items": items,
        }
        context = {
            "operator": profile["operator"],
            "agentRef": self.participant.agentRef,
            "task": run["task"],
            "runtime": profile["runtime"],
            "status": run["usageStatus"],
            "zeroUsage": not steps,
            "evidence": "LOCAL" if self.journal.settings.get("mode") == "monad" else "FIXTURE",
        }
        self.store.save(
            "outbox",
            key,
            {
                "scope": "OWN_EXECUTION",
                "report": report,
                "context": context,
                "pricing": profile["pricing"],
                "supersedes": None,
                "state": "PENDING",
                "attempts": 0,
                "nextAttempt": 0,
                "diagnostic": None,
            },
            immutable=True,
        )

    async def flushUsage(self):
        for key, row in self.store.rows("outbox"):
            run = self.store.get("run", key)
            if (
                row["state"] == "DELIVERED"
                or row["attempts"] >= run["profile"]["limits"]["deliveryAttempts"]
                or int(self.clock()) < row["nextAttempt"]
            ):
                continue
            row.update(attempts=row["attempts"] + 1, nextAttempt=int(self.clock()) + 2)
            self.store.save("outbox", key, row)
            try:
                await self.history.recordUsage(row["report"], row["context"], row["supersedes"])
                row["state"] = "DELIVERED"
            except (AdapterError, TimeoutError) as error:
                row["diagnostic"] = str(error)
            self.store.save("outbox", key, row)

    async def recordUnstarted(self):
        for row in self.journal.rows():
            ref = row["extension"]["executionRef"]
            key = executionKey(ref)
            if row["phase"] not in TERMINAL or row["startClaimed"] or self.store.get("run", key):
                continue
            reservation = self.store.get("reservation", key)
            if reservation is None:
                continue
            view = await self.participant.market.observeAward(ref)
            self.participant.checkAward(row["extension"], view, row)
            if view["stamp"]["finality"] != "FINALIZED":
                continue
            now = int(self.clock())
            self.store.save(
                "run",
                key,
                {
                    "version": 1,
                    "executionRef": ref,
                    "task": view["task"],
                    "bid": view["winningBid"],
                    "profile": reservation["profile"],
                    "inputDigest": view["task"]["terms"]["input"]["digest"],
                    "startedAt": None,
                    "cutoff": now,
                    "lastClock": now,
                    "stage": "STOPPED",
                    "plan": None,
                    "children": {},
                    "depositReserved": "0",
                    "gasReserved": "0",
                    "nextRead": now,
                    "diagnostic": "Award never started",
                    "usageStatus": "TIMED_OUT",
                },
                immutable=True,
            )
            self.queueUsage(ref)

    async def tick(self):
        await self.recordUnstarted()
        for key, job in list(self.jobs.items()):
            if job.done():
                del self.jobs[key]
                if not job.cancelled():
                    job.result()
        for key, run in self.store.rows("run"):
            if key in self.jobs:
                if self.journal.halted() or int(self.clock()) >= run["cutoff"]:
                    self.jobs[key].cancel()
                continue
            ref = run["executionRef"]
            row = self.journal.get(ref)
            if row["phase"] in TERMINAL or row["result"] is not None:
                if row["result"] is not None and run["usageStatus"] is None:
                    run = self.store.updateRun(ref, stage="ARTIFACT_READY", usageStatus="COMPLETED")
                if run["usageStatus"]:
                    self.queueUsage(ref)
                if run["plan"] and run["plan"]["slots"] and int(self.clock()) >= run["nextRead"]:
                    self.store.updateRun(
                        ref, nextRead=int(self.clock()) + run["profile"]["limits"]["pollSeconds"]
                    )
                    await self.advanceChildren(ref, cleanup=True)
                continue
            if int(self.clock()) >= run["nextRead"]:
                self.store.updateRun(
                    ref, nextRead=int(self.clock()) + run["profile"]["limits"]["pollSeconds"]
                )
                self.jobs[key] = asyncio.create_task(self.advanceRun(ref))
        await self.flushUsage()
        await self.observeReservations()

    async def close(self):
        for task in self.jobs.values():
            task.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        self.jobs.clear()
