"""Bounded Web3 transport and qualified, durable finalized observations."""

import asyncio
import time

import aiohttp
from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from web3 import AsyncHTTPProvider

from modules.adapters.chain.codec import abiType, callData
from modules.agent_client.ports import AdapterError, checkStamp, ensure, recordCheck
from modules.agent_client.signing import contentDigest


class RpcError(AdapterError):
    def __init__(self, error):
        self.error = error
        super().__init__("UNAVAILABLE", "RPC rejected the request")


class Web3Rpc:
    def __init__(self, url):
        self.provider = AsyncHTTPProvider(
            url,
            request_kwargs={"timeout": aiohttp.ClientTimeout(total=10)},
            exception_retry_configuration=None,
        )
        self.limit = asyncio.Semaphore(4)

    async def call(self, method, *params):
        try:
            async with self.limit, asyncio.timeout(10):
                response = await self.provider.make_request(method, list(params))
        except (TimeoutError, OSError, aiohttp.ClientError) as error:
            raise AdapterError("UNAVAILABLE", "RPC transport unavailable") from error
        ensure(isinstance(response, dict), "Malformed RPC response")
        if "error" in response:
            raise RpcError(response["error"])
        ensure("result" in response, "Missing RPC result")
        return response["result"]

    async def close(self):
        await self.provider.disconnect()


def quantity(value):
    ensure(isinstance(value, str) and value.startswith("0x"), "RPC quantity")
    try:
        number = int(value, 16)
    except ValueError as error:
        raise AdapterError("INVALID_DATA", "RPC quantity") from error
    ensure(number >= 0 and hex(number) == value, "Noncanonical RPC quantity")
    return number


class ChainConnection:
    def __init__(
        self, rpc, facts, genesisHash, journal, codec, readAbi, maxAgeSeconds, *, clock=time.time
    ):
        self.rpc, self.facts, self.genesisHash = rpc, facts, genesisHash
        self.journal, self.codec, self.schema = journal, codec, codec.schema
        self.readAbi = {entry["name"]: entry for entry in readAbi}
        ensure(type(maxAgeSeconds) is int and maxAgeSeconds > 0, "Finalized freshness limit")
        self.maxAgeSeconds, self.clock = maxAgeSeconds, clock
        for name, kind in (
            ("chainId", "Uint256"),
            ("market", "Address"),
            ("identityRegistry", "Address"),
            ("validator", "Address"),
            ("marketCodeHash", "Digest"),
            ("identityCodeHash", "Digest"),
            ("deploymentBlock", "Uint64"),
            ("deploymentBlockHash", "Digest"),
        ):
            recordCheck(facts[name], kind, self.schema)
        recordCheck(genesisHash, "Digest", self.schema)
        ensure(int(facts["chainId"]) > 0, "Zero chain")
        journal.bindChain(
            {
                name: facts[name]
                for name in (
                    "chainId",
                    "market",
                    "identityRegistry",
                    "marketCodeHash",
                    "identityCodeHash",
                    "validator",
                    "deploymentBlock",
                    "deploymentBlockHash",
                )
            }
            | {"genesisHash": genesisHash}
        )

    def stamp(self, block):
        ensure(isinstance(block, dict), "Block unavailable", "UNAVAILABLE")
        ensure(all(name in block for name in ("number", "hash", "timestamp")), "Incomplete block")
        stamp = {
            "source": "MONAD",
            "chainId": self.facts["chainId"],
            "blockNumber": str(quantity(block["number"])),
            "blockHash": block["hash"],
            "blockTimestamp": str(quantity(block["timestamp"])),
            "finality": "FINALIZED",
        }
        checkStamp(stamp, self.schema)
        return stamp

    async def block(self, number):
        value = await self.rpc.call("eth_getBlockByNumber", hex(int(number)), False)
        stamp = self.stamp(value)
        ensure(stamp["blockNumber"] == str(int(number)), "Wrong block returned")
        return stamp

    async def checkCanonical(self, stamp):
        block = await self.block(stamp["blockNumber"])
        if block != stamp:
            self.journal.halt("Previously finalized block changed")
        return block

    async def finalized(self):
        ensure(not self.journal.halted(), "Market halted", "FINALITY_CONFLICT")
        stamp = self.stamp(await self.rpc.call("eth_getBlockByNumber", "finalized", False))
        age = self.clock() - int(stamp["blockTimestamp"])
        ensure(0 <= age <= self.maxAgeSeconds, "Finalized head stale or in future", "UNAVAILABLE")
        previous = self.journal.latestFinalized()
        if previous:
            height, blockHash = previous
            ensure(
                int(stamp["blockNumber"]) >= int(height), "Finalized head regressed", "UNAVAILABLE"
            )
            block = await self.block(height)
            if block["blockHash"] != blockHash:
                self.journal.halt("Historical finalized block changed")
        self.journal.observe(stamp)
        return stamp

    async def verifyMarket(self, stamp):
        chainId = quantity(await self.rpc.call("eth_chainId"))
        genesis = await self.block(0)
        if str(chainId) != self.facts["chainId"] or genesis["blockHash"] != self.genesisHash:
            self.journal.halt("Chain identity changed")
        deployment = await self.block(self.facts["deploymentBlock"])
        if deployment["blockHash"] != self.facts["deploymentBlockHash"]:
            self.journal.halt("Market deployment block changed")
        raw = await self.rpc.call(
            "eth_getCode", self.facts["market"], hex(int(stamp["blockNumber"]))
        )
        try:
            matches = (
                raw != "0x"
                and contentDigest(bytes.fromhex(raw[2:])) == self.facts["marketCodeHash"]
            )
        except (TypeError, ValueError):
            matches = False
        if not matches:
            self.journal.halt("Market runtime code changed")
        expected = (
            chainId,
            self.facts["market"],
            self.facts["identityRegistry"],
            self.facts["validator"],
            1,
        )
        if await self.read("readPolicy", (), stamp) != expected:
            self.journal.halt("Market policy differs from deployment")

    async def read(self, name, values, stamp):
        entry = self.readAbi[name]
        raw = await self.rpc.call(
            "eth_call",
            {"to": self.facts["market"], "data": callData(entry, values)},
            hex(int(stamp["blockNumber"])),
        )
        await self.checkCanonical(stamp)
        try:
            ensure(
                isinstance(raw, str) and raw.startswith("0x") and len(raw) <= 2 * 1048576 + 2,
                "Contract response encoding/limit",
            )
            types = [abiType(field) for field in entry["outputs"]]
            result = decode(types, bytes.fromhex(raw[2:]))
            ensure("0x" + encode(types, result).hex() == raw, "Noncanonical contract response")
            return result
        except (ValueError, TypeError, OverflowError, DecodingError) as error:
            raise AdapterError("INVALID_DATA", "Malformed contract read") from error

    async def qualify(self):
        stamp = await self.finalized()
        ensure(
            int(stamp["blockNumber"]) >= int(self.facts["deploymentBlock"]),
            "Deployment not finalized",
            "UNAVAILABLE",
        )
        await self.verifyMarket(stamp)
        return stamp
