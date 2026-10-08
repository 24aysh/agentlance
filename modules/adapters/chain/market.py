"""Existing MarketPort over finalized contract reads and durable native intents."""

import time
from copy import deepcopy

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from modules.adapters.chain.native import NativeSender, operationResult
from modules.adapters.chain.rpc import RpcError, quantity
from modules.agent_client.ports import AdapterError, checkTaskView, ensure, recordCheck


def protocolError(error, catalog):
    raw = error.error.get("data") if isinstance(error.error, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("data")
    selector = "0x" + keccak(text="ProtocolError(uint16)")[:4].hex()
    if not isinstance(raw, str) or not raw.startswith(selector) or len(raw) != 74:
        return None
    try:
        code = decode(["uint16"], bytes.fromhex(raw[10:]))[0]
    except (ValueError, OverflowError, DecodingError):
        return None
    return next((name for name, value in catalog["errors"].items() if value == code), None)


class MonadMarket:
    recoverUnknown = True

    def __init__(
        self,
        chain,
        account=None,
        registry=None,
        *,
        maxGas=2_000_000,
        maxGasPriceWei=10**11,
        maxAttempts=3,
        clock=time.time,
    ):
        self.native = NativeSender(
            chain,
            account,
            registry,
            maxGas=maxGas,
            maxGasPriceWei=maxGasPriceWei,
            maxAttempts=maxAttempts,
            clock=clock,
        )
        self.chain, self.rpc, self.codec = chain, chain.rpc, chain.codec
        self.schema, self.journal = chain.schema, chain.journal
        self.account, self.registry, self.signer = account, registry, self.native.signer
        self.maxGas, self.maxGasPriceWei = maxGas, maxGasPriceWei
        self.lock, self.clock = self.native.lock, clock
        self.native.register(chain.facts["market"], self)

    def checkRef(self, ref, kind="TaskRef"):
        recordCheck(ref, kind, self.schema)
        ensure(ref["chainId"] == self.chain.facts["chainId"], "Foreign chain")
        field = "market" if kind == "TaskRef" else "identityRegistry"
        ensure(ref[field] == self.chain.facts[field], "Foreign reference")
        return self.codec.convert({"$ref": f"#/$defs/{kind}"}, ref)

    async def readTaskAt(self, taskRef, stamp):
        ref = self.checkRef(taskRef)
        try:
            values = (await self.chain.read("readTask", [ref], stamp))[0]
        except RpcError as error:
            if protocolError(error, self.codec.catalog) == "UNKNOWN_TASK":
                raise AdapterError("NOT_FOUND", "Task not found") from error
            raise
        kinds = {
            "task": {"$ref": "#/$defs/TaskSpec"},
            "status": {"enum": list(self.codec.catalog["states"])},
        }
        for name, kind in (
            ("allocation", "Allocation"),
            ("winningBid", "Bid"),
            ("result", "ResultCommitment"),
            ("receipt", "SettlementReceipt"),
        ):
            kinds[name] = {"anyOf": [{"$ref": f"#/$defs/{kind}"}, {"type": "null"}]}
        for name in ("ownWorkReserveAtoms", "reservedChildBudgets", "committedChildPayouts"):
            kinds[name] = {"$ref": "#/$defs/Uint96"}
        for name in ("childrenCreated", "activeChildren"):
            kinds[name] = {"type": "integer"}
        fields = self.chain.readAbi["readTask"]["outputs"][0]["components"]
        view = {
            field["name"]: self.codec.convert(kinds[field["name"]], value, decoding=True)
            for field, value in zip(fields, values, strict=True)
        } | {"stamp": stamp}
        checkTaskView(view, self.schema)
        ensure(view["task"]["taskRef"] == taskRef, "Wrong task returned")
        return view

    async def readTask(self, taskRef):
        return await self.readTaskAt(taskRef, await self.chain.qualify())

    async def readReputation(self, taskRef, agentRef):
        from modules.agent_client.ports import checkReputation
        from modules.market_core.reputation import calculateProbability

        stamp = await self.chain.qualify()
        task = (await self.readTaskAt(taskRef, stamp))["task"]
        values = [
            self.checkRef(agentRef, "AgentRef"),
            task["terms"]["taskFamily"],
            int(task["reputationSnapshotBlock"]),
        ]
        successes, failures = await self.chain.read("readCounters", values, stamp)
        result = {
            "taskRef": taskRef,
            "agentRef": agentRef,
            "taskFamily": task["terms"]["taskFamily"],
            "snapshotBlock": task["reputationSnapshotBlock"],
            "counters": {"successes": str(successes), "failures": str(failures)},
            "p": calculateProbability(successes, failures),
            "stamp": stamp,
        }
        checkReputation(result, self.schema)
        return result

    async def observeAward(self, executionRef):
        recordCheck(executionRef, "ExecutionRef", self.schema)
        return await self.readTask(executionRef["taskRef"])

    async def readOptional(self, name, kind, refs):
        stamp = await self.chain.qualify()
        values = [self.checkRef(ref, refKind) for ref, refKind in refs]
        try:
            wire = (await self.chain.read(name, values, stamp))[0]
        except RpcError as error:
            if protocolError(error, self.codec.catalog) == "UNKNOWN_TASK":
                raise AdapterError("NOT_FOUND", "Task not found") from error
            raise
        result = self.codec.convert(
            {"anyOf": [{"$ref": f"#/$defs/{kind}"}, {"type": "null"}]}, wire, decoding=True
        )
        if result is not None:
            recordCheck(result, kind, self.schema)
            ref = result["offer"]["taskRef"] if kind == "Bid" else result["taskRef"]
            ensure(ref == refs[0][0], "Read result binding")
            if kind == "Bid":
                ensure(result["offer"]["agentRef"] == refs[1][0], "Bid identity binding")
        return {"bid" if kind == "Bid" else "receipt": result, "stamp": stamp}

    async def readBid(self, taskRef, agentRef):
        return await self.readOptional(
            "readBid", "Bid", [(taskRef, "TaskRef"), (agentRef, "AgentRef")]
        )

    async def readSettlement(self, taskRef):
        return await self.readOptional(
            "readSettlement", "SettlementReceipt", [(taskRef, "TaskRef")]
        )

    async def submitSignedBid(self, command, operationId):
        return await self.submit(command, operationId, "submitBid")

    async def acceptAward(self, command, operationId):
        return await self.submit(command, operationId, "acceptAward")

    async def publishChild(self, command, valueAtoms, operationId):
        return await self.submit(command, operationId, "createChildTask", valueAtoms)

    async def commitResult(self, command, operationId):
        return await self.submit(command, operationId, "submitResult")

    async def allocateTask(self, command, operationId):
        return await self.submit(command, operationId, "allocateTask")

    async def expireTask(self, command, operationId):
        return await self.submit(command, operationId, "expireTask")

    async def settleVerdict(self, command, operationId):
        return await self.submit(command, operationId, "settleVerdict")

    async def availableFunds(self):
        await self.chain.qualify()
        return quantity(await self.rpc.call("eth_getBalance", self.signer, "pending"))

    async def submit(self, command, operationId, name, valueAtoms="0"):
        recordCheck(command, "Command", self.schema)
        recordCheck(valueAtoms, "Uint96", self.schema)
        ensure(command["command"] == name, "Port command mismatch")
        body = {"command": command, "valueAtoms": valueAtoms, "signer": self.signer}
        return await self.native.submit(self, body, operationId)

    async def readOperation(self, operationId):
        return await self.native.readOperation(operationId)

    def nativeData(self, op):
        return self.codec.commandData(op["command"]["command"], op["command"]["input"])

    def rejectNative(self, error):
        return protocolError(error, self.codec.catalog)

    async def prepareNative(self, body, stamp):
        command, valueAtoms = body["command"], body["valueAtoms"]
        name, op = command["command"], {}
        data = command["input"]
        ref = (
            data["offer"]["taskRef"]
            if name == "submitBid"
            else data.get(
                "taskRef",
                data.get(
                    "parentRef",
                    data.get("executionRef", data.get("record", {}).get("executionRef", {})).get(
                        "taskRef"
                    ),
                ),
            )
        )
        view = await self.readTaskAt(ref, stamp)
        if name == "submitBid":
            ensure(self.registry is not None, "Registry qualification required", "UNAVAILABLE")
            await self.registry.verifyDependencies(stamp)
            self.checkRef(data["offer"]["agentRef"], "AgentRef")
        terms, now = view["task"]["terms"], int(stamp["blockTimestamp"])
        if name == "expireTask":
            from modules.market_core.transitions import CUTOFFS

            ensure(view["status"] in CUTOFFS, "Already terminal", "CONFLICT")
            ensure(now >= int(terms[CUTOFFS[view["status"]][0]]), "Expiry premature", "CONFLICT")
            op["deadline"] = None  # Expiry has a lower cutoff, never an upper deadline.
        else:
            deadlineName = {
                "submitBid": "biddingClose",
                "acceptAward": "acceptBy",
                "submitResult": "resultBy",
                "createChildTask": "resultBy",
                "allocateTask": "allocationBy",
                "settleVerdict": "validationBy",
            }[name]
            op["deadline"] = terms[deadlineName]
            if name == "allocateTask":
                ensure(now >= int(terms["biddingClose"]), "Allocation premature", "CONFLICT")
            if name == "submitBid" and data["permit"] is not None:
                op["deadline"] = str(min(int(op["deadline"]), int(data["permit"]["expiry"])))
            if name == "settleVerdict":
                op["deadline"] = str(min(int(op["deadline"]), int(data["record"]["expiry"])))
            ensure(now < int(op["deadline"]), "Action expired", "CONFLICT")
        ensure(
            valueAtoms == (data["terms"]["budgetAtoms"] if name == "createChildTask" else "0"),
            "Native funding mismatch",
        )
        return {
            "from": self.signer,
            "to": self.chain.facts["market"],
            "data": self.nativeData(body),
            "value": hex(int(valueAtoms)),
            "gas": hex(self.maxGas),
        }, op["deadline"]

    async def finishNative(self, op, stamp):
        receipt, expected = op["receipt"], op["transaction"]
        events = [
            self.codec.event(log)
            for log in receipt["logs"]
            if log["address"].lower() == expected["to"]
        ]
        commandName = op["command"]["command"]
        if commandName in {"allocateTask", "expireTask", "settleVerdict"}:
            data = op["command"]["input"]
            ref = (
                data["record"]["executionRef"]["taskRef"]
                if commandName == "settleVerdict"
                else data["taskRef"]
            )
            names = [event["name"] for event in events]
            allowed = (
                [["TaskSettled"]]
                if commandName in {"expireTask", "settleVerdict"}
                else [["TaskAwarded"], ["TaskSettled"]]
            )
            ensure(names in allowed, "Progress event mismatch")
            for event in events:
                payload = event["payload"]
                record = payload.get("allocation", payload.get("receipt", payload))
                ensure(record["taskRef"] == ref, "Progress task binding")
            if commandName == "settleVerdict":
                ensure(
                    events[0]["payload"]["receipt"]["validation"] == data["record"],
                    "Verdict event binding",
                )
            await self.readTaskAt(ref, stamp)
            self.journal.observe(stamp)
            op["result"] = operationResult(op["operationId"], "APPLIED", events=events, stamp=stamp)
            self.journal.saveNative(op)
            return deepcopy(op["result"])
        expectedName = {
            "submitBid": "BidAccepted",
            "acceptAward": "AwardAccepted",
            "createChildTask": "TaskCreated",
            "submitResult": "ResultSubmitted",
        }[op["command"]["command"]]
        ensure(len(events) == 1 and events[0]["name"] == expectedName, "Command event mismatch")
        payload = events[0]["payload"]
        data = op["command"]["input"]
        if expectedName == "BidAccepted":
            ensure(payload["bid"]["offer"] == data["offer"], "Bid event mismatch")
            ref = data["offer"]["taskRef"]
        elif expectedName == "TaskCreated":
            ensure(
                payload["task"]["parentRef"] == data["parentRef"]
                and payload["task"]["terms"] == data["terms"],
                "Child event mismatch",
            )
            ref = payload["task"]["taskRef"]
        else:
            record = payload if expectedName == "AwardAccepted" else payload["result"]
            ensure(record["executionRef"] == data["executionRef"], "Execution event mismatch")
            field = "ownWorkReserveAtoms" if expectedName == "AwardAccepted" else "artifact"
            ensure(record[field] == data[field], "Committed payload mismatch")
            ref = data["executionRef"]["taskRef"]
        await self.readTaskAt(ref, stamp)
        self.journal.observe(stamp)
        op["result"] = operationResult(op["operationId"], "APPLIED", events=events, stamp=stamp)
        self.journal.saveNative(op)
        return deepcopy(op["result"])
