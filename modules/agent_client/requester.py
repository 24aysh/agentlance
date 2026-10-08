"""Explicit requester intents over the existing durable native transaction queue."""

from copy import deepcopy

from modules.adapters.chain.rpc import quantity
from modules.agent_client.ports import AdapterError, ensure, recordCheck
from modules.domain.records import ProtocolViolation
from modules.economics.records import recordDigest
from modules.market_core.state import CommandContext, CorePolicy
from modules.market_core.transitions import validateTerms
from modules.validation.evaluator import validatePrerequisites


class Requester:
    def __init__(self, market, content):
        self.market, self.content = market, content
        self.chain, self.journal = market.chain, market.journal

    def operationId(self, requestId, sender=None):
        ensure(isinstance(requestId, str) and 1 <= len(requestId) <= 128, "Request ID")
        row = self.journal.db.execute("SELECT value FROM flags WHERE name='sender'").fetchone()
        sender = sender or self.market.signer or (row[0] if row else None)
        ensure(sender is not None, "Request sender required", "UNAVAILABLE")
        recordCheck(sender, "Address", self.market.schema)
        return recordDigest(
            [
                "requester-v1",
                self.chain.facts["chainId"],
                self.chain.facts["market"],
                sender,
                requestId,
            ]
        )

    async def validateTaskRequest(self, terms, requester):
        recordCheck(terms, "TaskTerms", self.market.schema)
        recordCheck(requester, "Address", self.market.schema)
        ensure(
            int(requester, 16) != 0 and int(terms["refundAddress"], 16) != 0,
            "Nonzero requester and refund address required",
        )
        stamp = await self.chain.qualify()
        facts = self.chain.facts
        policy = CorePolicy(
            int(facts["chainId"]), facts["market"], facts["identityRegistry"], facts["validator"]
        )
        try:
            validateTerms(
                terms,
                CommandContext(requester, int(stamp["blockNumber"]), int(stamp["blockTimestamp"])),
                policy,
                None,
            )
        except ProtocolViolation as error:
            raise AdapterError("INVALID_DATA", error.code) from error
        if terms["retryOf"] is not None:
            prior = await self.market.readTaskAt(terms["retryOf"], stamp)
            ensure(
                prior["status"] == "SETTLED"
                and prior["receipt"]["reason"] != "SUCCESS"
                and prior["task"]["requester"] == requester
                and prior["task"]["parentRef"] is None,
                "Invalid root retry",
            )
        raw = [
            await self.content.fetchBytes(terms[k]["uri"], limit, terms[k]["digest"])
            for k, limit in (
                ("input", 1048576),
                ("outputSchema", 65536),
                ("validationPolicy", 65536),
            )
        ]
        try:
            validatePrerequisites(terms, *raw)
        except ProtocolViolation as error:
            raise AdapterError("INVALID_DATA", error.code) from error
        await self.chain.checkCanonical(stamp)
        return {
            "terms": deepcopy(terms),
            "requester": requester,
            "depositAtoms": terms["budgetAtoms"],
            "stamp": stamp,
        }

    async def submit(self, command, requestId):
        recordCheck(command, "Command", self.market.schema)
        name, data = command["command"], command["input"]
        ensure(
            name in {"createTask", "cancelTask", "allocateTask", "expireTask", "withdrawCredit"},
            "Unsupported requester command",
        )
        ensure(self.market.signer is not None, "Requester signer required", "UNAVAILABLE")
        key = self.operationId(requestId)
        value = data["terms"]["budgetAtoms"] if name == "createTask" else "0"
        old = self.journal.nativeOperation(key)
        if old is not None:
            ensure(
                old["command"] == command
                and old["valueAtoms"] == value
                and old["signer"] == self.market.signer,
                "Request body changed",
                "CONFLICT",
            )
        elif name == "createTask":
            await self.validateTaskRequest(data["terms"], self.market.signer)
        if name == "withdrawCredit":
            ensure(
                int(data["amountAtoms"]) > 0 and int(data["receiver"], 16) != 0,
                "Positive amount and nonzero receiver required",
            )
        if name == "createTask":
            await self.market.createTask(command, value, key)
        else:
            await getattr(self.market, name)(command, key)
        return await self.readOperation(requestId)

    async def resume(self, requestId):
        key = self.operationId(requestId)
        old = self.journal.nativeOperation(key)
        ensure(old is not None, "Unknown request", "NOT_FOUND")
        # A saved, not-yet-signed operation still needs prepare/simulation/signing.
        return await self.submit(old["command"], requestId)

    async def readOperation(self, requestId, sender=None):
        """Never reconcile: NativeSender.readOperation is a write/rebroadcast operation."""
        key = self.operationId(requestId, sender)
        op = self.journal.nativeOperation(key)
        ensure(op is not None, "Unknown request", "NOT_FOUND")
        head = await self.chain.qualify()
        result = op["result"]
        verified = result
        if result and result["stamp"]:
            await self.chain.checkCanonical(result["stamp"])
        receipt, label = op["receipt"], "UNSENT"
        if op["transactionHash"]:
            label = "UNKNOWN"
            current = await self.chain.rpc.call("eth_getTransactionReceipt", op["transactionHash"])
            if current is not None:
                stamp = await self.chain.block(quantity(current["blockNumber"]))
                ensure(
                    current["transactionHash"].lower() == op["transactionHash"], "Receipt binding"
                )
                if current["blockHash"] == stamp["blockHash"]:
                    await self.market.native.verifyReceipt(op | {"receipt": current}, stamp)
                    receipt = current
                    if (
                        int(stamp["blockNumber"]) <= int(head["blockNumber"])
                        and quantity(current["status"]) == 1
                    ):
                        verified = await self.market.verifyNative(op | {"receipt": current}, stamp)
                    label = (
                        "MINED_UNFINALIZED"
                        if int(stamp["blockNumber"]) > int(head["blockNumber"])
                        else "FINALIZED_REVERTED"
                        if quantity(current["status"]) == 0
                        else "FINALIZED_APPLIED"
                        if verified and verified["state"] == "APPLIED"
                        else "UNKNOWN"
                    )
            elif await self.chain.rpc.call("eth_getTransactionByHash", op["transactionHash"]):
                label = "PENDING"
        elif result and result["state"] == "REJECTED":
            label = "REJECTED"
        taskRef = next(
            (
                e["payload"]["task"]["taskRef"]
                for e in (verified or {}).get("events", [])
                if e["name"] == "TaskCreated"
            ),
            None,
        )
        return {
            "requestId": requestId,
            "operationId": key,
            "result": result,
            "transactionHash": op["transactionHash"],
            "receipt": {
                k: receipt[k]
                for k in ("transactionHash", "blockHash", "blockNumber", "status", "gasUsed")
            }
            if receipt
            else None,
            "diagnostic": op["diagnostic"],
            "confirmation": label,
            "taskRef": taskRef,
            "stamp": head,
        }
