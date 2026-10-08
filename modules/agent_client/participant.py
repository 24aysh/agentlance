"""One external participant. Protocol reads authorize work; A2A only carries hints."""

import asyncio

from modules.adapters.a2a.profile import ProfileError, awardHint, checkHint, strictJson
from modules.adapters.registry.resolution import resolveProfile, selectInterface
from modules.adapters.storage.journal import executionKey
from modules.agent_client.ports import AdapterError, checkTaskView, ensure, recordCheck
from modules.agent_client.signing import contentDigest


def makeCommand(name, **data):
    return {"schemaVersion": 1, "command": name, "input": data}


class Participant:
    def __init__(
        self,
        agentRef,
        signer,
        market,
        registry,
        content,
        journal,
        schema,
        execute,
        checkInput,
        supportedDigests,
    ):
        self.agentRef, self.signer, self.market, self.registry = agentRef, signer, market, registry
        self.content, self.journal, self.schema = content, journal, schema
        self.execute, self.checkInput, self.supportedDigests = execute, checkInput, supportedDigests
        self.lock = asyncio.Lock()
        self.coordinator = None

    async def supportTask(self, task):
        terms = task["terms"]
        ensure(
            (terms["outputSchema"]["digest"], terms["validationPolicy"]["digest"])
            == self.supportedDigests,
            "Worker does not support this schema/policy",
            "UNSUPPORTED",
        )
        blobs = []
        for name, limit in (
            ("input", 1048576),
            ("outputSchema", 65536),
            ("validationPolicy", 65536),
        ):
            ref = terms[name]
            blobs.append(await self.content.fetchBytes(ref["uri"], limit, ref["digest"]))
        recordCheck(strictJson(blobs[1]), "OutputShape", self.schema)
        policy = strictJson(blobs[2])
        recordCheck(policy, "ValidationPolicy", self.schema)
        ensure(
            policy["outputSchemaDigest"] == terms["outputSchema"]["digest"], "Policy schema binding"
        )
        self.checkInput(blobs[0])
        return blobs[0]

    async def submitIntent(self, scope, command, valueAtoms="0"):
        intent = self.journal.intent(scope, command, valueAtoms, self.signer)
        result = None
        if intent["attempted"]:
            result = await self.market.readOperation(intent["operationId"])
        if result is None or (
            result["state"] == "UNKNOWN" and getattr(self.market, "recoverUnknown", False)
        ):
            intent["attempted"] = True
            self.journal.saveIntent(intent)
            methods = {
                "submitBid": self.market.submitSignedBid,
                "acceptAward": self.market.acceptAward,
                "submitResult": self.market.commitResult,
                "createChildTask": self.market.publishChild,
            }
            if command["command"] in {"allocateTask", "expireTask"}:
                methods[command["command"]] = getattr(self.market, command["command"])
            method = methods[command["command"]]
            if command["command"] == "createChildTask":
                result = await method(command, valueAtoms, intent["operationId"])
            else:
                result = await method(command, intent["operationId"])
        intent["result"] = result
        self.journal.saveIntent(intent)
        return result

    async def prepareBid(self, taskRef, bidAtoms, signPermit, nonce, expiry):
        async with self.lock:
            ensure(not self.journal.halted(), "Market halted", "FINALITY_CONFLICT")
            existing = await self.market.readBid(taskRef, self.agentRef)
            if existing["bid"] is not None:
                return existing
            scope = "bid:" + executionKey({"taskRef": taskRef, "awardId": 1})
            intent = self.journal.findIntent(scope)
            if intent:
                ensure(
                    intent["command"]["input"]["offer"]["bidAtoms"] == bidAtoms,
                    "Changed bid intent",
                    "CONFLICT",
                )
                return await self.submitIntent(scope, intent["command"])
            view = await self.market.readTask(taskRef)
            checkTaskView(view, self.schema)
            self.journal.observe(view["stamp"])
            ensure(
                view["stamp"]["finality"] == "FINALIZED"
                and view["status"] == "OPEN"
                and int(view["stamp"]["blockTimestamp"])
                < int(view["task"]["terms"]["biddingClose"]),
                "Task not open for bidding",
            )
            await self.supportTask(view["task"])
            resolved = await resolveProfile(
                self.agentRef,
                self.registry,
                self.content,
                self.schema,
                self.agentRef["chainId"],
                self.agentRef["identityRegistry"],
            )
            ensure(resolved.registry["verifiedWallet"] is not None, "WALLET_UNSET")
            self.journal.storeContent(resolved.cardBytes)
            self.journal.storeContent(resolved.registrationBytes)
            offer = {
                "taskRef": taskRef,
                "agentRef": self.agentRef,
                "bidAtoms": bidAtoms,
                "executionSigner": self.signer,
                "payout": resolved.registry["verifiedWallet"],
                "profileDigest": resolved.profile["agentCard"]["digest"],
            }
            permit = offer | {"owner": resolved.registry["owner"], "nonce": nonce, "expiry": expiry}
            if self.coordinator is not None:
                await self.coordinator.reserveBid(view["task"], bidAtoms)
            signature = signPermit(permit)
            command = makeCommand("submitBid", offer=offer, permit=permit, signature=signature)
            recordCheck(command, "Command", self.schema)
            return await self.submitIntent(scope, command)

    async def publishChild(self, parentRef, terms, requestId):
        command = makeCommand("createChildTask", parentRef=parentRef, terms=terms)
        recordCheck(command, "Command", self.schema)
        return await self.submitIntent("child:" + requestId, command, terms["budgetAtoms"])

    def checkAward(self, extension, view, existing=None):
        checkTaskView(view, self.schema)
        self.journal.observe(view["stamp"])
        ref, bid = extension["executionRef"], view["winningBid"]
        if existing and existing["task"] is not None:
            if existing["task"] != view["task"] or existing["bid"] != bid:
                self.journal.halt("Finalized obligation changed")

        if (
            view["task"]["taskRef"] != ref["taskRef"]
            or bid is None
            or extension["agentRef"] != self.agentRef
            or bid["offer"]["agentRef"] != self.agentRef
            or bid["offer"]["executionSigner"] != self.signer
            or view["allocation"]["awardId"] != ref["awardId"]
        ):
            raise ProfileError("PROFILE_AWARD_MISMATCH", view["status"], view["receipt"])
        terms = view["task"]["terms"]
        if (
            extension["inputDigest"] != terms["input"]["digest"]
            or extension["validationPolicyDigest"] != terms["validationPolicy"]["digest"]
        ):
            raise ProfileError("PROFILE_CONFLICT" if existing else "PROFILE_AWARD_MISMATCH")
        return terms

    async def receiveHint(self, message):
        extension = checkHint(message, self.schema)
        async with self.lock:
            existing = self.journal.checkMessage(message, extension)
            if existing:
                return self.journal.remember(message, extension)
            try:
                view = await self.market.observeAward(extension["executionRef"])
            except AdapterError as error:
                if error.kind == "NOT_FOUND":
                    raise ProfileError("PROFILE_AWARD_MISMATCH") from error
                raise
            return self.admitAward(message, extension, view)

    def admitAward(self, message, extension, view):
        terms = self.checkAward(extension, view)
        now = int(view["stamp"]["blockTimestamp"])
        if view["status"] not in {"AWARDED", "RUNNING"} or now >= int(
            terms["acceptBy" if view["status"] == "AWARDED" else "resultBy"]
        ):
            raise ProfileError("PROFILE_EXPIRED", view["status"], view["receipt"])
        if self.coordinator is not None:
            self.coordinator.admit(view)
        return self.journal.remember(message, extension)

    async def observeOwnAward(self, ref):
        async with self.lock:
            view = await self.market.observeAward(ref)
            ensure(view["stamp"]["finality"] == "FINALIZED", "Award not finalized", "UNAVAILABLE")
            message = awardHint(view, "watcher:" + contentDigest(executionKey(ref).encode())[2:])
            extension = checkHint(message, self.schema)
            existing = self.journal.get(ref)
            self.checkAward(extension, view, existing)
            if existing:
                return existing
            return self.admitAward(message, extension, view)

    async def advance(self, ref):
        async with self.lock:
            row = self.journal.get(ref)
            if row is None or row["phase"] in {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}:
                return
            try:
                await self.advanceRun(ref, row)
            except ProfileError as error:
                if row["result"] is None:
                    self.journal.update(
                        ref, phase="STOPPED", diagnostic=error.diagnostic, detail=error.detail
                    )
                else:
                    self.journal.update(ref, detail=error.detail)
            except AdapterError as error:
                # Operational failures are visible; they never authorize a retry of execution.
                self.journal.update(ref, detail=str(error))

    async def advanceRun(self, ref, row):
        view = await self.market.observeAward(ref)
        terms = self.checkAward(row["extension"], view, row)
        if view["stamp"]["finality"] != "FINALIZED":
            return
        if row["task"] is None:
            row = self.journal.update(
                ref, task=view["task"], bid=view["winningBid"], stamp=view["stamp"]
            )
        if view["status"] in {"SUBMITTED", "SETTLED"}:
            if row["result"] is not None and view["result"] is not None:
                if row["result"] != view["result"]["artifact"]:
                    raise ProfileError("PROFILE_CONFLICT")
                self.journal.update(
                    ref, phase="RESULT_RECORDED", detail="Canonical result recorded"
                )
                return
            raise ProfileError("PROFILE_EXPIRED", view["status"], view["receipt"])
        now = int(view["stamp"]["blockTimestamp"])
        if now >= int(terms["acceptBy"] if view["status"] == "AWARDED" else terms["resultBy"]):
            raise ProfileError("PROFILE_EXPIRED", view["status"], view["receipt"])
        if row["result"] is not None:
            if view["status"] != "RUNNING" or view["activeChildren"]:
                return
            self.journal.readContent(row["result"]["digest"])
            self.journal.update(ref, phase="RESULT_PENDING")
            result = await self.submitIntent(
                "result:" + executionKey(ref),
                makeCommand("submitResult", executionRef=ref, artifact=row["result"]),
            )
            if result["state"] == "REJECTED":
                self.journal.update(ref, detail=result["error"])
            return
        card = self.journal.readContent(view["winningBid"]["offer"]["profileDigest"])
        selectInterface(strictJson(card))
        raw = await self.supportTask(view["task"])
        if self.coordinator is not None:
            reserve = self.coordinator.admit(view)
            row = self.journal.update(ref, ownWorkReserveAtoms=reserve)
        if view["status"] == "AWARDED":
            self.journal.update(ref, phase="ACCEPT_PENDING")
            result = await self.submitIntent(
                "accept:" + executionKey(ref),
                makeCommand(
                    "acceptAward", executionRef=ref, ownWorkReserveAtoms=row["ownWorkReserveAtoms"]
                ),
            )
            if result["state"] == "REJECTED":
                self.journal.update(ref, phase="STOPPED", detail=result["error"])
            return
        ensure(
            view["status"] == "RUNNING"
            and view["ownWorkReserveAtoms"] == row["ownWorkReserveAtoms"],
            "Accepted reserve changed",
            "CONFLICT",
        )
        # Fetching can take time. Reobserve immediately before the durable start claim.
        fresh = await self.market.observeAward(ref)
        self.checkAward(row["extension"], fresh, row)
        if (
            fresh["stamp"]["finality"] != "FINALIZED"
            or fresh["status"] != "RUNNING"
            or int(fresh["stamp"]["blockTimestamp"]) >= int(terms["resultBy"])
        ):
            return
        if self.coordinator is not None:
            self.coordinator.startExecution(ref, raw)
            return
        self.journal.update(ref, phase="READY")
        if self.journal.claim(ref):
            try:
                output = self.execute(raw)
                ensure(isinstance(output, bytes), "Worker must return exact bytes")
                self.content.publishResult(ref, output)
            except Exception as error:
                self.journal.update(
                    ref, phase="INTERRUPTED", detail=f"Worker failed: {type(error).__name__}"
                )

    async def tick(self):
        # Saved results must progress even if local execution analytics have backpressure.
        for row in self.journal.rows():
            await self.advance(row["extension"]["executionRef"])
        if self.coordinator is not None:
            await self.coordinator.tick()
