"""Existing MarketPort over finalized contract reads and durable native intents."""

import asyncio
import time
from copy import deepcopy

from eth_utils import to_checksum_address

from modules.adapters.chain.rpc import RpcError, quantity
from modules.agent_client.ports import AdapterError, ensure
from modules.agent_client.signing import contentDigest


def operationResult(operationId, state="UNKNOWN", error=None, events=(), stamp=None):
    return {
        "operationId": operationId,
        "state": state,
        "error": error,
        "events": list(events),
        "stamp": stamp,
    }


class NativeSender:
    """One durable nonce queue for explicitly registered contract destinations."""

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
        self.destinations = {}

    def register(self, address, destination):
        ensure(address not in self.destinations, "Duplicate transaction destination", "CONFLICT")
        self.destinations[address] = destination

    def destination(self, op):
        address = op.get("destination", self.chain.facts["market"])
        ensure(address in self.destinations, "Unconfigured pending destination", "UNAVAILABLE")
        return self.destinations[address]

    async def submit(self, destination, body, operationId):
        ensure(isinstance(operationId, str) and 0 < len(operationId) <= 128, "Operation ID")
        ensure(self.account is not None, "No configured signer", "UNAVAILABLE")
        async with self.lock:
            op = self.journal.nativeOperation(operationId)
            if op:
                ensure(
                    all(op.get(key) == value for key, value in body.items()),
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
            transaction, op["deadline"] = await destination.prepareNative(body, stamp)
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
            try:
                await self.rpc.call("eth_call", transaction, hex(int(stamp["blockNumber"])))
                estimate = quantity(await self.rpc.call("eth_estimateGas", transaction))
            except RpcError as error:
                code = destination.rejectNative(error)
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
                balance >= int(body["valueAtoms"]) + gas * gasPrice,
                "Insufficient funded balance",
                "UNAVAILABLE",
            )
            transaction.update(
                chainId=int(self.chain.facts["chainId"]),
                nonce=nonce,
                gas=gas,
                gasPrice=gasPrice,
                value=int(body["valueAtoms"]),
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
            and expected["to"] == op.get("destination", self.chain.facts["market"])
            and expected["value"] == int(op["valueAtoms"])
            and expected["chainId"] == int(self.chain.facts["chainId"])
            and expected["data"] == self.destination(op).nativeData(op),
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
        return await self.destination(op).finishNative(op, stamp)
