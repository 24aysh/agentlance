"""Qualified receipt exports using the market sender's single durable nonce queue."""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from modules.adapters.chain.codec import abiType, callData
from modules.adapters.chain.native import operationResult
from modules.adapters.registry.chain import REFERENCE_REVISION
from modules.agent_client.ports import AdapterError, ensure
from modules.agent_client.signing import contentDigest
from modules.economics.records import recordDigest

IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
OWNER_SLOT = "0x9016d09d72d40fdae2fd8ceac6b6234c7706214fd39c1cd1e609a0528c199300"


class FeedbackAdapter:
    def __init__(self, market, address, publisherAbi, registryAbi, verification):
        self.market, self.chain, self.native = market, market.chain, market.native
        self.address, self.verification = address, verification
        self.publisher = {x["name"]: x for x in publisherAbi if x["type"] in {"function", "event"}}
        self.registry = {x["name"]: x for x in registryAbi if x["type"] == "function"}
        ensure(
            verification["sourceRevision"] == REFERENCE_REVISION
            and verification["publisher"] == address
            and verification["market"] == self.chain.facts["market"]
            and verification["identityRegistry"] == self.chain.facts["identityRegistry"]
            and verification["publisherAbiDigest"] == recordDigest(publisherAbi)
            and verification["reputationAbiDigest"] == recordDigest(registryAbi),
            "Feedback verification binding",
        )
        self.native.register(address, self)

    async def read(self, address, entry, args, stamp):
        raw = await self.chain.rpc.call(
            "eth_call",
            {"to": address, "data": callData(entry, args)},
            hex(int(stamp["blockNumber"])),
        )
        ensure(isinstance(raw, str) and len(raw) <= 131074, "Feedback response limit")
        try:
            return decode([abiType(x) for x in entry["outputs"]], bytes.fromhex(raw[2:]))
        except (ValueError, DecodingError) as error:
            raise AdapterError("INVALID_DATA", "Malformed feedback response") from error

    async def verifyDependencies(self, stamp):
        evidence = self.verification
        ensure(
            int(evidence["validUntil"]) > self.chain.clock(),
            "Feedback qualification expired",
            "UNAVAILABLE",
        )
        deployment = await self.chain.block(evidence["deploymentBlock"])
        ensure(
            deployment["blockHash"] == evidence["deploymentBlockHash"]
            and int(evidence["deploymentBlock"]) <= int(stamp["blockNumber"]),
            "Publisher deployment changed",
            "UNAVAILABLE",
        )
        observations = {x["address"]: x["codeHash"] for x in evidence["codeObservations"]}
        ensure(
            {self.address, evidence["reputationRegistry"], evidence["identityRegistry"]}
            <= observations.keys(),
            "Missing registry code observations",
        )
        for address, digest in observations.items():
            raw = await self.chain.rpc.call("eth_getCode", address, hex(int(stamp["blockNumber"])))
            ensure(
                raw != "0x" and contentDigest(bytes.fromhex(raw[2:])) == digest,
                "Feedback dependency code changed",
                "UNAVAILABLE",
            )
        for address in (evidence["identityRegistry"], evidence["reputationRegistry"]):
            observed = await self.chain.rpc.call(
                "eth_getStorageAt", address, IMPLEMENTATION_SLOT, hex(int(stamp["blockNumber"]))
            )
            proxies = [proxy for proxy in evidence["proxies"] if proxy["address"] == address]
            ensure(
                (int(observed, 16) == 0 and not proxies)
                or (len(proxies) == 1 and "0x" + observed[-40:] == proxies[0]["implementation"]),
                "Unqualified registry proxy",
                "UNAVAILABLE",
            )
        for proxy in evidence["proxies"]:
            ensure(
                proxy["kind"] == "uups" and proxy["implementation"] in observations,
                "Unsupported/unqualified proxy",
            )
            slots = {
                x["slot"]: x["value"]
                for x in evidence["storageObservations"]
                if x["address"] == proxy["address"]
            }
            ensure(
                IMPLEMENTATION_SLOT in slots
                and OWNER_SLOT in slots
                and "0x" + slots[IMPLEMENTATION_SLOT][-40:] == proxy["implementation"],
                "Missing implementation/owner qualification",
            )
        for item in evidence["storageObservations"]:
            value = await self.chain.rpc.call(
                "eth_getStorageAt", item["address"], item["slot"], hex(int(stamp["blockNumber"]))
            )
            ensure(value == item["value"], "Feedback implementation/admin changed", "UNAVAILABLE")
        for name, expected in [
            ("chainId", int(self.chain.facts["chainId"])),
            ("market", evidence["market"]),
            ("identityRegistry", evidence["identityRegistry"]),
            ("reputationRegistry", evidence["reputationRegistry"]),
        ]:
            ensure(
                (await self.read(self.address, self.publisher[name], [], stamp))[0] == expected,
                "Publisher configuration changed",
                "UNAVAILABLE",
            )
        ensure(
            (
                await self.read(
                    evidence["reputationRegistry"], self.registry["getIdentityRegistry"], [], stamp
                )
            )[0]
            == evidence["identityRegistry"],
            "Reputation identity binding",
            "UNAVAILABLE",
        )
        await self.chain.checkCanonical(stamp)

    def nativeData(self, op):
        return callData(self.publisher["publish"], [self.market.checkRef(op["taskRef"])])

    def rejectNative(self, error):
        # Registry reverts are operational unavailability, never a market error code.
        return None

    async def prepareNative(self, body, stamp):
        await self.verifyDependencies(stamp)
        view = await self.market.readTaskAt(body["taskRef"], stamp)
        ensure(
            view["receipt"] is not None and view["receipt"]["counterEffect"] != "NONE",
            "Receipt not exportable",
            "CONFLICT",
        )
        agentId = int(view["receipt"]["winner"]["agentId"])
        data = keccak(text="isAuthorizedOrOwner(address,uint256)")[:4] + encode(
            ["address", "uint256"], [self.address, agentId]
        )
        authorization = await self.chain.rpc.call(
            "eth_call",
            {"to": self.verification["identityRegistry"], "data": "0x" + data.hex()},
            hex(int(stamp["blockNumber"])),
        )
        ensure(
            authorization == "0x" + "00" * 32,
            "Publisher is an owner/operator or identity authorization is unavailable",
            "UNAVAILABLE",
        )
        await self.chain.checkCanonical(stamp)
        return {
            "from": self.native.signer,
            "to": self.address,
            "data": self.nativeData(body),
            "value": "0x0",
            "gas": hex(self.native.maxGas),
        }, None

    async def readPublicationAt(self, taskRef, stamp):
        await self.verifyDependencies(stamp)
        published, index = await self.read(
            self.address, self.publisher["readPublication"], [self.market.checkRef(taskRef)], stamp
        )
        ensure(published == (index > 0), "Publication index binding")
        feedback = None
        if published:
            view = await self.market.readTaskAt(taskRef, stamp)
            receipt = view["receipt"]
            ensure(
                receipt is not None
                and receipt["winner"] is not None
                and receipt["counterEffect"] != "NONE",
                "Published receipt missing",
            )
            value, decimals, tag1, tag2, revoked = await self.read(
                self.verification["reputationRegistry"],
                self.registry["readFeedback"],
                [int(receipt["winner"]["agentId"]), self.address, index],
                stamp,
            )
            ensure(
                value == (1 if receipt["counterEffect"] == "SUCCESS" else 0)
                and decimals == 0
                and tag1 == "agentlance-v1"
                and tag2 == view["task"]["terms"]["taskFamily"]
                and not revoked,
                "Registry feedback differs from receipt",
                "UNAVAILABLE",
            )
            feedback = {
                "agentRef": receipt["winner"],
                "client": self.address,
                "value": value,
                "valueDecimals": decimals,
                "tag1": tag1,
                "tag2": tag2,
                "isRevoked": revoked,
            }
        await self.chain.checkCanonical(stamp)
        return {
            "published": published,
            "feedbackIndex": str(index),
            "feedback": feedback,
            "stamp": stamp,
        }

    async def readPublication(self, taskRef):
        return await self.readPublicationAt(taskRef, await self.chain.qualify())

    async def publish(self, taskRef, operationId):
        self.market.checkRef(taskRef)
        return await self.native.submit(
            self,
            {
                "destination": self.address,
                "taskRef": taskRef,
                "valueAtoms": "0",
                "signer": self.native.signer,
            },
            operationId,
        )

    async def finishNative(self, op, stamp):
        publication = await self.readPublicationAt(op["taskRef"], stamp)
        ensure(publication["published"], "Successful publication without mapping")
        logs = [x for x in op["receipt"]["logs"] if x["address"].lower() == self.address]
        entry = self.publisher["ReceiptPublished"]
        signature = "ReceiptPublished(" + ",".join(abiType(x) for x in entry["inputs"]) + ")"
        ensure(len(logs) <= 1, "Duplicate publisher event")
        for log in logs:
            ensure(log["topics"] == ["0x" + keccak(text=signature).hex()], "Publisher event topic")
            ref, index = decode(
                [abiType(x) for x in entry["inputs"]], bytes.fromhex(log["data"][2:])
            )
            ensure(
                tuple(ref) == tuple(self.market.checkRef(op["taskRef"]))
                and str(index) == publication["feedbackIndex"],
                "Publisher event binding",
            )
        self.chain.journal.observe(stamp)
        op["result"] = operationResult(op["operationId"], "APPLIED", stamp=stamp)
        self.chain.journal.saveNative(op)
        return op["result"]
