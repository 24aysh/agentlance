"""Existing MarketPort over finalized contract reads and durable native intents."""

import asyncio
import time
from copy import deepcopy

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address

from modules.adapters.chain.rpc import RpcError, quantity
from modules.agent_client.ports import AdapterError, checkTaskView, ensure, recordCheck
from modules.agent_client.signing import contentDigest


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


def operationResult(operationId, state="UNKNOWN", error=None, events=(), stamp=None):
    return {
        "operationId": operationId,
        "state": state,
        "error": error,
        "events": list(events),
        "stamp": stamp,
    }


class MonadMarket:
    # Only transports with a durable persist-before-send guarantee may opt in.
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
        self.chain, self.rpc, self.codec = chain, chain.rpc, chain.codec
        self.schema, self.journal = chain.schema, chain.journal
        self.account, self.registry = account, registry
        self.signer = account.address.lower() if account else None
        if self.signer:
            self.journal.bindSender(self.signer)
        ensure(type(maxGas) is int and 21000 <= maxGas <= 30_000_000, "Gas cap")
        ensure(type(maxGasPriceWei) is int and 0 < maxGasPriceWei < 2**256, "Gas-price cap")
        ensure(type(maxAttempts) is int and 1 <= maxAttempts <= 10, "Broadcast attempt limit")
        self.maxGas, self.maxGasPriceWei, self.maxAttempts = maxGas, maxGasPriceWei, maxAttempts
        self.clock, self.lock = clock, asyncio.Lock()

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

    async def availableFunds(self):
        await self.chain.qualify()
        return quantity(await self.rpc.call("eth_getBalance", self.signer, "pending"))

    async def submit(self, command, operationId, name, valueAtoms="0"):
        recordCheck(command, "Command", self.schema)
        recordCheck(valueAtoms, "Uint96", self.schema)
        ensure(command["command"] == name, "Port command mismatch")
        ensure(isinstance(operationId, str) and 0 < len(operationId) <= 128, "Operation ID")
        ensure(self.account is not None, "No configured signer", "UNAVAILABLE")
        body = {"command": command, "valueAtoms": valueAtoms, "signer": self.signer}
        async with self.lock:
            op = self.journal.nativeOperation(operationId)
            if op:
                ensure(
                    all(op[key] == value for key, value in body.items()),
                    "Operation body changed",
                    "CONFLICT",
                )
                if op["transactionHash"] or op["result"]:
                    return await self.reconcile(op)
            else:
                op = deepcopy(body) | {
                    "operationId": operationId,
                    "transactionHash": None,
                    "raw": None,
                    "transaction": None,
                    "attempts": 0,
                    "lastAttempt": 0,
                    "deadline": None,
                    "result": None,
                    "receipt": None,
                    "diagnostic": None,
                }
                self.journal.saveNative(op)
            stamp = await self.chain.qualify()
            data = command["input"]
            ref = (
                data["offer"]["taskRef"]
                if name == "submitBid"
                else data.get(
                    "taskRef", data.get("parentRef", data.get("executionRef", {}).get("taskRef"))
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
                ensure(
                    now >= int(terms[CUTOFFS[view["status"]][0]]), "Expiry premature", "CONFLICT"
                )
                op["deadline"] = None  # Expiry has a lower cutoff, never an upper deadline.
            else:
                deadlineName = {
                    "submitBid": "biddingClose",
                    "acceptAward": "acceptBy",
                    "submitResult": "resultBy",
                    "createChildTask": "resultBy",
                    "allocateTask": "allocationBy",
                }[name]
                op["deadline"] = terms[deadlineName]
                if name == "allocateTask":
                    ensure(now >= int(terms["biddingClose"]), "Allocation premature", "CONFLICT")
                if name == "submitBid" and data["permit"] is not None:
                    op["deadline"] = str(min(int(op["deadline"]), int(data["permit"]["expiry"])))
                ensure(now < int(op["deadline"]), "Action expired", "CONFLICT")
            ensure(
                valueAtoms == (data["terms"]["budgetAtoms"] if name == "createChildTask" else "0"),
                "Native funding mismatch",
            )
            for other in self.journal.nativeOperations():
                if (
                    other["operationId"] != operationId
                    and other["transactionHash"]
                    and other["result"] is None
                ):
                    await self.reconcile(other)
                    ensure(
                        self.journal.nativeOperation(other["operationId"])["result"] is not None,
                        "Sender has an unresolved transaction",
                        "CONFLICT",
                    )
            transaction = {
                "from": self.signer,
                "to": self.chain.facts["market"],
                "data": self.codec.commandData(name, data),
                "value": hex(int(valueAtoms)),
                "gas": hex(self.maxGas),
            }
            try:
                await self.rpc.call("eth_call", transaction, hex(int(stamp["blockNumber"])))
                estimate = quantity(await self.rpc.call("eth_estimateGas", transaction))
            except RpcError as error:
                code = protocolError(error, self.codec.catalog)
                if code:
                    op["result"] = operationResult(operationId, "REJECTED", code, stamp=stamp)
                    self.journal.saveNative(op)
                    return op["result"]
                raise
            await self.chain.checkCanonical(stamp)
            gas = (estimate * 12 + 9) // 10
            gasPrice = quantity(await self.rpc.call("eth_gasPrice"))
            ensure(
                21000 <= gas <= self.maxGas and 0 < gasPrice <= self.maxGasPriceWei,
                "Gas or fee cap exceeded",
                "UNAVAILABLE",
            )
            nonce = quantity(await self.rpc.call("eth_getTransactionCount", self.signer, "latest"))
            pending = quantity(
                await self.rpc.call("eth_getTransactionCount", self.signer, "pending")
            )
            ensure(nonce == pending, "External pending nonce conflict", "CONFLICT")
            completed = [
                item["transaction"]["nonce"]
                for item in self.journal.nativeOperations()
                if item["transaction"] and item["result"]
            ]
            ensure(
                not completed or nonce == max(completed) + 1,
                "External nonce consumption",
                "CONFLICT",
            )
            balance = quantity(await self.rpc.call("eth_getBalance", self.signer, "pending"))
            ensure(
                balance >= int(valueAtoms) + gas * gasPrice,
                "Insufficient funded balance",
                "UNAVAILABLE",
            )
            transaction.update(
                chainId=int(self.chain.facts["chainId"]),
                nonce=nonce,
                gas=gas,
                gasPrice=gasPrice,
                value=int(valueAtoms),
            )
            signed = self.account.sign_transaction(
                transaction
                | {"from": self.account.address, "to": to_checksum_address(transaction["to"])}
            )
            op.update(
                transaction=transaction,
                raw="0x" + bytes(signed.raw_transaction).hex(),
                transactionHash="0x" + bytes(signed.hash).hex(),
            )
            self.journal.saveNative(op)
            return await self.reconcile(op)

    async def readOperation(self, operationId):
        async with self.lock:
            op = self.journal.nativeOperation(operationId)
            if not op:
                return operationResult(operationId)
            return await self.reconcile(op)

    async def reconcile(self, op):
        stamp = await self.chain.qualify()
        if op["result"] is not None:
            if op["result"]["stamp"]:
                await self.chain.checkCanonical(op["result"]["stamp"])
            ensure(op["diagnostic"] is None, op["diagnostic"] or "Native transaction failed")
            return deepcopy(op["result"])
        txHash = op["transactionHash"]
        if txHash is None:
            return operationResult(op["operationId"])
        ensure(
            contentDigest(bytes.fromhex(op["raw"][2:])) == txHash, "Signed journal bytes corrupt"
        )
        expected = op["transaction"]
        ensure(
            expected["from"] == op["signer"] == self.signer
            and expected["to"] == self.chain.facts["market"]
            and expected["value"] == int(op["valueAtoms"])
            and expected["chainId"] == int(self.chain.facts["chainId"])
            and expected["data"]
            == self.codec.commandData(op["command"]["command"], op["command"]["input"]),
            "Stored native intent binding changed",
        )
        receipt = await self.rpc.call("eth_getTransactionReceipt", txHash)
        if receipt is not None:
            ensure(receipt["transactionHash"].lower() == txHash, "Receipt transaction mismatch")
            height = quantity(receipt["blockNumber"])
            canonical = await self.chain.block(height)
            if canonical["blockHash"] == receipt["blockHash"]:
                op["receipt"] = receipt
                self.journal.saveNative(op)
                if height <= int(stamp["blockNumber"]):
                    return await self.finish(op, canonical)
                return operationResult(op["operationId"], "PENDING")
            # An orphaned, unfinalized receipt is not a finalized contradiction.
            op["receipt"] = None
            self.journal.saveNative(op)
        transaction = await self.rpc.call("eth_getTransactionByHash", txHash)
        if transaction is not None:
            ensure(transaction["hash"].lower() == txHash, "Transaction hash mismatch")
            return operationResult(op["operationId"], "PENDING")
        nonce = quantity(await self.rpc.call("eth_getTransactionCount", self.signer, "latest"))
        ensure(
            nonce <= op["transaction"]["nonce"],
            "Nonce consumed without known transaction",
            "CONFLICT",
        )
        now = int(self.clock())
        if (
            (op["deadline"] is None or int(stamp["blockTimestamp"]) < int(op["deadline"]))
            and op["attempts"] < self.maxAttempts
            and (op["attempts"] == 0 or now - op["lastAttempt"] >= 2)
        ):
            op["attempts"] += 1
            op["lastAttempt"] = now
            self.journal.saveNative(op)
            try:
                actual = await self.rpc.call("eth_sendRawTransaction", op["raw"])
                ensure(actual.lower() == txHash, "Broadcast returned wrong hash")
                return operationResult(op["operationId"], "PENDING")
            except AdapterError as error:
                if error.kind != "UNAVAILABLE":
                    raise
        return operationResult(op["operationId"])

    async def finish(self, op, stamp):
        receipt = op["receipt"]
        actual = await self.rpc.call("eth_getTransactionByHash", op["transactionHash"])
        ensure(isinstance(actual, dict), "Finalized transaction unavailable", "UNAVAILABLE")
        expected = op["transaction"]
        ensure(
            actual["hash"].lower() == op["transactionHash"]
            and actual["from"].lower() == op["signer"]
            and actual["to"].lower() == expected["to"]
            and actual["input"].lower() == expected["data"]
            and quantity(actual["value"]) == expected["value"]
            and quantity(actual["nonce"]) == expected["nonce"]
            and actual["blockHash"] == stamp["blockHash"],
            "Finalized transaction binding",
        )
        ensure(
            all(
                log["blockHash"] == stamp["blockHash"]
                and log["transactionHash"].lower() == op["transactionHash"]
                for log in receipt["logs"]
            ),
            "Receipt log binding",
        )
        if quantity(receipt["status"]) == 0:
            # Standard JSON-RPC receipts do not contain authenticated revert bytes.
            op["diagnostic"] = "Finalized failed transaction; revert bytes unavailable"
            op["result"] = operationResult(op["operationId"], stamp=stamp)
            self.journal.saveNative(op)
            raise AdapterError("INVALID_DATA", op["diagnostic"])
        ensure(quantity(receipt["status"]) == 1, "Unknown receipt status")
        events = [
            self.codec.event(log)
            for log in receipt["logs"]
            if log["address"].lower() == expected["to"]
        ]
        commandName = op["command"]["command"]
        if commandName in {"allocateTask", "expireTask"}:
            ref = op["command"]["input"]["taskRef"]
            names = [event["name"] for event in events]
            allowed = (
                [["TaskSettled"]]
                if commandName == "expireTask"
                else [["TaskAwarded"], ["TaskSettled"]]
            )
            ensure(names in allowed, "Progress event mismatch")
            for event in events:
                payload = event["payload"]
                record = payload.get("allocation", payload.get("receipt", payload))
                ensure(record["taskRef"] == ref, "Progress task binding")
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
