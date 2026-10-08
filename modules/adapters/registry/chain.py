"""Pinned ERC-8004 identity reads; profile bytes still belong to resolution.py."""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from modules.adapters.chain.codec import ZERO, callData
from modules.adapters.chain.rpc import RpcError
from modules.agent_client.ports import AdapterError, checkIdentity, checkStamp, ensure, recordCheck
from modules.agent_client.signing import contentDigest

REFERENCE_REVISION = "b9e466c250744a7e06b13dff9d3c2844ed64f825"


class MonadRegistry:
    def __init__(self, chain, abi, verification):
        self.chain, self.rpc, self.schema = chain, chain.rpc, chain.schema
        self.verification = verification
        self.feedback = None
        ensure(
            verification["sourceRevision"] == REFERENCE_REVISION,
            "Unsupported registry revision",
            "UNSUPPORTED",
        )
        self.functions = {item["name"]: item for item in abi if item["type"] == "function"}
        self.events = {}
        for item in abi:
            if item["type"] == "event":
                signature = item["name"] + "(" + ",".join(f["type"] for f in item["inputs"]) + ")"
                self.events["0x" + keccak(text=signature).hex()] = item

    async def verifyDependencies(self, stamp):
        evidence = self.verification
        recordCheck(evidence["deploymentBlock"], "Uint64", self.schema)
        recordCheck(evidence["deploymentBlockHash"], "Digest", self.schema)
        ensure(
            int(evidence["deploymentBlock"]) <= int(stamp["blockNumber"]),
            "Registry deployment not finalized",
            "UNAVAILABLE",
        )
        deployment = await self.chain.block(evidence["deploymentBlock"])
        ensure(
            deployment["blockHash"] == evidence["deploymentBlockHash"],
            "Registry deployment changed",
            "UNAVAILABLE",
        )
        ensure(
            int(evidence["validUntil"]) > self.chain.clock(),
            "Registry evidence expired",
            "UNAVAILABLE",
        )
        ensure(
            all(
                evidence["probes"].get(name) is True
                for name in (
                    "transferClearsWallet",
                    "restoredWalletControl",
                    "contractOwner",
                    "registrationDiscovery",
                )
            ),
            "Registry verification incomplete",
            "UNAVAILABLE",
        )
        ensure(
            evidence["proxyKind"] == "none" or bool(evidence["storageObservations"]),
            "Proxy observations missing",
        )
        observations = [
            {
                "address": self.chain.facts["identityRegistry"],
                "codeHash": self.chain.facts["identityCodeHash"],
            },
            *evidence["codeObservations"],
        ]
        for item in observations:
            raw = await self.rpc.call(
                "eth_getCode", item["address"], hex(int(stamp["blockNumber"]))
            )
            ensure(
                raw != "0x" and contentDigest(bytes.fromhex(raw[2:])) == item["codeHash"],
                "Registry code changed",
                "UNAVAILABLE",
            )
        for item in evidence["storageObservations"]:
            raw = await self.rpc.call(
                "eth_getStorageAt", item["address"], item["slot"], hex(int(stamp["blockNumber"]))
            )
            ensure(raw == item["value"], "Registry implementation/admin changed", "UNAVAILABLE")
        await self.chain.checkCanonical(stamp)
        if self.feedback is not None:
            await self.feedback.verifyDependencies(stamp)

    async def readIdentity(self, agentRef):
        recordCheck(agentRef, "AgentRef", self.schema)
        ensure(
            agentRef["chainId"] == self.chain.facts["chainId"]
            and agentRef["identityRegistry"] == self.chain.facts["identityRegistry"],
            "Registry namespace",
        )
        stamp = await self.chain.qualify()
        await self.verifyDependencies(stamp)
        values = []
        for name in ("ownerOf", "getAgentWallet", "tokenURI"):
            entry = self.functions[name]
            try:
                raw = await self.rpc.call(
                    "eth_call",
                    {
                        "to": agentRef["identityRegistry"],
                        "data": callData(entry, [int(agentRef["agentId"])]),
                    },
                    hex(int(stamp["blockNumber"])),
                )
                ensure(
                    isinstance(raw, str) and len(raw) <= 2 * (65536 + 96) + 2,
                    "Registry response limit",
                )
                values.append(decode([entry["outputs"][0]["type"]], bytes.fromhex(raw[2:]))[0])
            except (ValueError, DecodingError) as error:
                raise AdapterError("INVALID_DATA", "Malformed registry response") from error
        owner, wallet, uri = values
        ensure(owner != ZERO, "Missing identity owner", "NOT_FOUND")
        code = await self.rpc.call("eth_getCode", owner, hex(int(stamp["blockNumber"])))
        await self.chain.checkCanonical(stamp)
        result = {
            "agentRef": agentRef,
            "owner": owner,
            "verifiedWallet": None if wallet == ZERO else wallet,
            "registrationUri": uri,
            "ownerHasCode": code != "0x",
            "stamp": stamp,
        }
        checkIdentity(result, self.schema)
        return result

    async def checkContractSignature(self, owner, digest, signature, stamp, gasLimit=50000):
        ensure(gasLimit == 50000, "ERC-1271 gas cap")
        recordCheck(owner, "Address", self.schema)
        recordCheck(digest, "Digest", self.schema)
        recordCheck(signature, "Signature", self.schema)
        checkStamp(stamp, self.schema)
        ensure(
            stamp["source"] == "MONAD"
            and stamp["chainId"] == self.chain.facts["chainId"]
            and stamp["finality"] == "FINALIZED",
            "Signature observation namespace/finality",
        )
        head = await self.chain.qualify()
        ensure(
            int(stamp["blockNumber"]) <= int(head["blockNumber"]), "Signature block not finalized"
        )
        await self.chain.checkCanonical(stamp)
        data = keccak(text="isValidSignature(bytes32,bytes)")[:4] + encode(
            ["bytes32", "bytes"], [bytes.fromhex(digest[2:]), bytes.fromhex(signature[2:])]
        )
        # eth_call directly to a wallet permits simulated writes; ERC-1271 requires STATICCALL.
        # Creation-call initcode copies the payload, STATICCALLs the owner with exactly 50k gas,
        # then returns (success, returndata length, first word). Nothing is deployed or signed.
        size = len(data).to_bytes(2, "big")
        tail = (
            bytes.fromhex("6000396020604061")
            + size
            + bytes.fromhex("600073")
            + bytes.fromhex(owner[2:])
            + bytes.fromhex("61c350fa6000523d60205260606000f3")
        )
        code = b"\x61" + size + b"\x61" + (6 + len(tail)).to_bytes(2, "big") + tail + data
        try:
            raw = await self.rpc.call(
                "eth_call",
                {"data": "0x" + code.hex(), "gas": hex(250000)},
                hex(int(stamp["blockNumber"])),
            )
        except RpcError as error:
            message = str(error.error).lower()
            if "revert" not in message and "out of gas" not in message:
                raise
            return {
                "outcome": "OUT_OF_GAS" if "out of gas" in message else "REVERTED",
                "returnData": None,
            }
        await self.chain.checkCanonical(stamp)
        ensure(isinstance(raw, str) and len(raw) == 194, "Malformed static signature response")
        try:
            success, length, word = decode(["bool", "uint256", "bytes32"], bytes.fromhex(raw[2:]))
        except (ValueError, DecodingError) as error:
            raise AdapterError("INVALID_DATA", "Malformed static signature response") from error
        if not success:
            return {"outcome": "REVERTED", "returnData": None}
        ensure(length <= 32, "Oversized signature response")
        return {"outcome": "RETURNED", "returnData": "0x" + word[:length].hex()}

    def decodeLog(self, log):
        ensure(
            log["address"].lower() == self.chain.facts["identityRegistry"], "Foreign registry log"
        )
        entry = self.events.get(log["topics"][0])
        ensure(entry is not None, "Unknown registry event")
        indexed = [field for field in entry["inputs"] if field["indexed"]]
        ensure(len(log["topics"]) == len(indexed) + 1, "Registry topic count")
        values = {}
        try:
            for field, topic in zip(indexed, log["topics"][1:], strict=True):
                if field["type"] != "string":
                    values[field["name"]] = decode([field["type"]], bytes.fromhex(topic[2:]))[0]
            plain = [field for field in entry["inputs"] if not field["indexed"]]
            ensure(len(log["data"]) <= 2 * (65536 + 256) + 2, "Registry event limit")
            decoded = decode([field["type"] for field in plain], bytes.fromhex(log["data"][2:]))
            values.update(
                {field["name"]: value for field, value in zip(plain, decoded, strict=True)}
            )
        except (ValueError, DecodingError) as error:
            raise AdapterError("INVALID_DATA", "Malformed registry event") from error
        ref = {
            "chainId": self.chain.facts["chainId"],
            "identityRegistry": self.chain.facts["identityRegistry"],
            "agentId": str(values.get("agentId", values.get("tokenId"))),
        }
        recordCheck(ref, "AgentRef", self.schema)
        return ref
