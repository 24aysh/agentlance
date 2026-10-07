"""Explicitly invoked, qualified Monad testnet fixture scenarios; never auto-deploys.

AGENTLANCE_L3_ACTORS names a private JSON file with roles (address, keystore,
passwordEnv), identities (agentId, owner, signer, payout, permitSigner, profileDigest, profileFile),
artifacts (input, outputSchema, validationPolicy, result, evidence: {ref, file}),
evidenceFiles (the four report verification names), windowSeconds, maxGas and
maxGasPriceWei. Paths are relative to that file. Contract owners omit keystore;
their permitSigner is a controlled EOA whose signature the actual ERC-1271 owner
accepts. All normal roles use distinct encrypted keystores. No key is a CLI argument.

A qualified report is an input, not a claim made by this driver. Digest-bound local
RPC evidence records officialSource, chainId, genesisHash, executionRevision,
clientVersion, validUntil. Identity evidence records registry/version, validUntil,
proxyKind, codeObservations [{address, codeHash}], storageObservations
[{address, slot, value}], and successful transferClearsWallet/restoredWalletControl/
contractOwner probes. This driver rechecks code/storage and controlled identities.
"""

import getpass
import json
import os
import secrets
import subprocess
import tempfile
import time
from copy import deepcopy
from pathlib import Path

import httpx
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils import keccak, to_checksum_address

from modules.agent_client.signing import buildBidPermitTypedData
from modules.domain.records import addressBytes, parseJson, validateRecord
from modules.market_core.transitions import targetRef
from scripts.layer3_deployment import buildEvidence, validateDeploymentReport
from scripts.layer3_tools import toolPath, verifyToolchain
from tests.layer3_support import ROOT, Rpc, RpcError, WireCodec, abiType, callData, readJson

OFFICIAL_TESTNET = "https://docs.monad.xyz/developer-essentials/testnet"
SCENARIOS = {"solo", "parent-child", "timeouts", "malicious"}
EVIDENCE_NAMES = (
    "identityVerification",
    "rpcVerification",
    "validatorVerification",
    "scenarioEvidence",
)


class QualificationError(RuntimeError):
    """A verified prerequisite changed or could not be established."""


class PendingFinality(RuntimeError):
    """A transaction may exist, but cannot yet authorize subsequent actions."""


def requireFact(condition, message):
    if not condition:
        raise QualificationError(message)


def digest(raw):
    return "0x" + keccak(raw).hex()


def finalizedBlock(rpc):
    try:
        block = rpc.call("eth_getBlockByNumber", "finalized", False)
    except (RpcError, httpx.HTTPError):
        raise PendingFinality(
            "Finalized state unavailable; WAIT without a latest-block fallback"
        ) from None
    if not block or not all(key in block for key in ("number", "hash", "timestamp")):
        raise PendingFinality("RPC returned no finalized block; WAIT")
    previous = getattr(rpc, "_l3FinalizedBlock", None)
    if previous:
        requireFact(
            int(block["number"], 16) >= int(previous["number"], 16), "Finalized height regressed"
        )
        if block["number"] == previous["number"]:
            requireFact(block["hash"] == previous["hash"], "Finalized block hash changed")
        else:
            try:
                canonical = rpc.call("eth_getBlockByNumber", previous["number"], False)
            except (RpcError, httpx.HTTPError):
                raise PendingFinality("Prior finalized block cannot be reverified; WAIT") from None
            requireFact(
                canonical and canonical["hash"] == previous["hash"],
                "Previously finalized block was reorganized",
            )
    rpc._l3FinalizedBlock = dict(block)
    return block


def waitFinalized(rpc, transactionHash, deadline, *, clock=time.time, sleep=time.sleep):
    while clock() < deadline:
        block = finalizedBlock(rpc)
        try:
            receipt = rpc.call("eth_getTransactionReceipt", transactionHash)
            if receipt and int(receipt["blockNumber"], 16) <= int(block["number"], 16):
                canonical = rpc.call("eth_getBlockByNumber", receipt["blockNumber"], False)
                requireFact(
                    canonical is not None and canonical["hash"] == receipt["blockHash"],
                    "Receipt conflicts with finalized canonical block",
                )
                requireFact(
                    receipt["transactionHash"].lower() == transactionHash.lower(),
                    "RPC returned a different transaction receipt",
                )
                requireFact(
                    all(log["blockHash"] == receipt["blockHash"] for log in receipt["logs"]),
                    "Receipt contains logs from another block",
                )
                return receipt, block
        except (RpcError, httpx.HTTPError):
            raise PendingFinality(
                "Receipt/canonical-block verification unavailable; WAIT"
            ) from None
        sleep(2)
    raise PendingFinality("Task action deadline reached before canonical finality; WAIT")


def loadActors(config, base):
    accounts, addresses = {}, {}
    # Explicitly refuse public local fixture keys, including the default Anvil mnemonic.
    forbidden = {
        Account.from_key(number.to_bytes(32, "big")).address.lower() for number in range(1, 257)
    }
    forbidden |= {
        "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266",
        "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
        "0x3c44cdddb6a900fa2b585dd299e03d12fa4293bc",
        "0x90f79bf6eb2c4f870365e785982e1f101e93b906",
        "0x15d34aaf54267db7d7c367839aaf71a00a2c6a65",
        "0x9965507d1a55bcc2695c58ba16fb37d819b0a4dc",
        "0x976ea74026e726554db657fa54763abd0c3a0aa9",
        "0x14dc79964da2c08b23698b3d3cc7ca32193d9955",
        "0x23618e81e3f5cdf7f54c3d65f7fbc0abf5b21e8f",
        "0xa0ee7a142d267c1f36714e4a8f75612f20a79720",
    }
    for role, details in config["roles"].items():
        address = details["address"]
        addressBytes(address)
        requireFact(
            address not in forbidden, "Public local fixture accounts are forbidden on testnet"
        )
        addresses[role] = address
        if details.get("keystore") is None:
            continue
        password = os.environ.get(details.get("passwordEnv", ""))
        if password is None:
            password = getpass.getpass(f"Keystore password for {role}: ")
        try:
            keystore = parseJson((base / details["keystore"]).read_bytes())
            account = Account.from_key(Account.decrypt(keystore, password))
        except (ValueError, OSError, KeyError):
            raise QualificationError(f"Could not unlock keystore for role {role}") from None
        finally:
            del password
        requireFact(
            account.address.lower() == address, f"Keystore/address mismatch for role {role}"
        )
        accounts[role] = account
    requireFact(
        len(set(addresses.values())) == len(addresses), "Demonstration roles must be distinct"
    )
    return accounts, addresses


def loadEvidence(report, config, base):
    evidence = {}
    for name in EVIDENCE_NAMES:
        raw = (base / config["evidenceFiles"][name]).read_bytes()
        requireFact(
            len(raw) <= 1024 * 1024 and digest(raw) == report[name]["digest"],
            f"Digest mismatch for retained {name}",
        )
        evidence[name] = parseJson(raw)
    return evidence


class TestnetRun:
    def __init__(self, rpc, report, config, accounts, addresses, evidence, output):
        self.rpc, self.report, self.config = rpc, report, config
        self.accounts, self.actors, self.evidence = accounts, addresses, evidence
        self.output = Path(output)
        self.facts = report["manifestFacts"]
        self.chainId = int(self.facts["chainId"])
        self.market = self.facts["market"]
        self.codec = WireCodec()
        self.readAbi = readJson("specs/contracts.read.abi.json")
        self.signingTypes = readJson("specs/signing/types.json")
        self.window = config["windowSeconds"]
        requireFact(
            type(self.window) is int and 120 <= self.window <= 3600,
            "windowSeconds must be 120..3600",
        )
        requireFact(
            type(config["maxGas"]) is int and 21000 <= config["maxGas"] <= 30_000_000,
            "Invalid explicit gas cap",
        )
        requireFact(0 < int(config["maxGasPriceWei"]) < 2**256, "Invalid explicit gas-price cap")
        self.receipts, self.createdCredits = [], {}
        self.journal = {
            "network": self.facts.get("network", "unqualified-test-transport"),
            "chainId": self.facts["chainId"],
            "market": self.market,
            "fixtureArtifacts": True,
            "status": "preflight",
            "reportDigest": digest(
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            ),
            "transactions": [],
            "settlements": [],
            "liveGate": "scenario evidence only; full L3 gate not implied",
        }

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.journal, indent=2) + "\n")
        temporary.replace(self.output)

    def read(self, name, values=(), block=None):
        block = block or finalizedBlock(self.rpc)
        entry = next(value for value in self.readAbi if value["name"] == name)
        result = self.rpc.call(
            "eth_call", {"to": self.market, "data": callData(entry, values)}, block["number"]
        )
        return decode([abiType(value) for value in entry["outputs"]], bytes.fromhex(result[2:]))

    def registryAddress(self, method, agentId, block):
        data = keccak(text=method + "(uint256)")[:4] + encode(["uint256"], [int(agentId)])
        raw = self.rpc.call(
            "eth_call",
            {"to": self.facts["identityRegistry"], "data": "0x" + data.hex()},
            block["number"],
        )
        requireFact(len(raw) == 66, "Registry returned a malformed address")
        return decode(["address"], bytes.fromhex(raw[2:]))[0]

    def verifyDependencies(self, block):
        for address, expected in (
            (self.market, self.facts["marketCodeHash"]),
            (self.facts["identityRegistry"], self.facts["identityCodeHash"]),
        ):
            raw = self.rpc.call("eth_getCode", address, block["number"])
            requireFact(
                raw != "0x" and digest(bytes.fromhex(raw[2:])) == expected,
                "Qualified contract code changed",
            )
        identity = self.evidence["identityVerification"]
        requireFact(
            identity["identityRegistry"] == self.facts["identityRegistry"]
            and identity["identityVersion"] == self.facts["identityVersion"],
            "Registry source/version evidence mismatch",
        )
        requireFact(
            int(identity["validUntil"]) > time.time(), "Registry qualification evidence expired"
        )
        requireFact(
            all(
                identity["probes"].get(name) is True
                for name in ("transferClearsWallet", "restoredWalletControl", "contractOwner")
            ),
            "Registry qualification probes incomplete",
        )
        requireFact(
            identity["proxyKind"] == "none" or bool(identity["storageObservations"]),
            "Proxy dependency observations missing",
        )
        for observation in identity["codeObservations"]:
            raw = self.rpc.call("eth_getCode", observation["address"], block["number"])
            requireFact(
                digest(bytes.fromhex(raw[2:])) == observation["codeHash"],
                "Registry implementation/admin code changed",
            )
        for observation in identity["storageObservations"]:
            actual = self.rpc.call(
                "eth_getStorageAt", observation["address"], observation["slot"], block["number"]
            )
            requireFact(
                actual.lower() == observation["value"],
                "Registry proxy implementation/admin storage changed",
            )

    def verifyIdentity(self, name, block, *, fresh=False):
        identity = self.config["identities"][name]
        owner = self.registryAddress("ownerOf", identity["agentId"], block)
        wallet = self.registryAddress("getAgentWallet", identity["agentId"], block)
        requireFact(owner == self.actors[identity["owner"]], f"Identity owner changed for {name}")
        requireFact(
            wallet == self.actors[identity["payout"]], f"Verified wallet changed for {name}"
        )
        ownerCode = self.rpc.call("eth_getCode", owner, block["number"])
        requireFact(
            ownerCode != "0x" or identity["permitSigner"] == identity["owner"],
            "A code-free identity owner must sign its own permit",
        )
        ref = (self.chainId, self.facts["identityRegistry"], int(identity["agentId"]))
        if fresh:
            requireFact(
                self.read(
                    "readCounters", [ref, "structured-output-v1", int(block["number"], 16)], block
                )
                == (0, 0),
                f"Scenario requires a fresh identity: {name}",
            )
        return identity

    def validateInputs(self, scenario):
        required = {"requester", "relayer", "validator", "receiver"}
        if scenario == "parent-child":
            required.add("signer")
        requireFact(
            required <= self.accounts.keys() and required <= self.actors.keys(),
            "Required requester/relayer/validator/receiver signing roles are missing",
        )
        requireFact(
            set(self.config.get("artifacts", {}))
            == {"input", "outputSchema", "validationPolicy", "result", "evidence"},
            "All five fixture artifact references and exact bytes are required",
        )
        for artifact in self.config["artifacts"].values():
            validateRecord(artifact["ref"], "ContentRef", self.codec.schema)
        names = {
            "solo": ["solo"],
            "parent-child": ["parent", "child"],
            "timeouts": ["noShow", "executionTimeout", "validatorTimeout", "validationFailed"],
            "malicious": ["malicious", "contractOwner"],
        }[scenario]
        for name in names:
            requireFact(
                name in self.config.get("identities", {}), f"Missing controlled identity: {name}"
            )
            identity = self.config["identities"][name]
            requireFact(
                all(
                    identity.get(field) in self.actors
                    for field in ("owner", "signer", "payout", "permitSigner")
                ),
                f"Missing identity role for {name}",
            )
            identityRoles = {
                self.actors[identity[field]] for field in ("owner", "signer", "payout")
            }
            requireFact(
                len(identityRoles) == 3 and self.actors["validator"] not in identityRoles,
                "Identity owner/signer/payout must be distinct and exclude the validator",
            )
            if scenario == "malicious":
                requireFact(
                    self.actors[identity["signer"]] != self.actors["relayer"],
                    "Malicious scenario requires an execution signer distinct from its relayer",
                )
            validateRecord(
                {
                    "taskRef": self.taskRef(1),
                    "agentRef": {
                        "chainId": str(self.chainId),
                        "identityRegistry": self.facts["identityRegistry"],
                        "agentId": identity.get("agentId"),
                    },
                    "executionSigner": self.actors[identity["signer"]],
                    "payout": self.actors[identity["payout"]],
                    "bidAtoms": "0",
                    "profileDigest": identity.get("profileDigest"),
                },
                "BidOffer",
                self.codec.schema,
            )
            for field in ("signer", "payout", "permitSigner"):
                requireFact(
                    identity[field] in self.accounts,
                    f"Controlled signing role missing: {identity[field]}",
                )
            if name == "validationFailed":
                requireFact(
                    identity["owner"] in self.accounts,
                    "Direct-owner bid requires an owner keystore",
                )
        requireFact(
            len({self.config["identities"][name]["agentId"] for name in names}) == len(names),
            "Scenario identities must be distinct",
        )
        return names

    def preflight(self, scenario):
        names = self.validateInputs(scenario)
        requireFact(
            self.chainId == 10143 and self.facts["network"] == "monad-testnet",
            "Only the official Monad testnet is supported",
        )
        requireFact(int(self.rpc.call("eth_chainId"), 16) == self.chainId, "RPC chain ID mismatch")
        genesis = self.rpc.call("eth_getBlockByNumber", "0x0", False)
        requireFact(
            genesis and genesis["hash"] == self.report["genesisHash"], "RPC genesis mismatch"
        )
        network = self.evidence["rpcVerification"]
        requireFact(
            network["officialSource"] == OFFICIAL_TESTNET
            and network["chainId"] == self.facts["chainId"]
            and network["genesisHash"] == self.report["genesisHash"]
            and network["executionRevision"] == self.report["executionRevision"]
            and int(network["validUntil"]) > time.time(),
            "Official network qualification missing, expired or inconsistent",
        )
        requireFact(
            self.rpc.call("web3_clientVersion") == network["clientVersion"],
            "RPC client changed; requalify its execution revision",
        )
        block = finalizedBlock(self.rpc)
        requireFact(
            abs(time.time() - int(block["timestamp"], 16)) < 120,
            "Finalized RPC state is stale or clock is inconsistent",
        )
        deployment, _ = waitFinalized(self.rpc, self.report["deploymentTxHash"], time.time() + 10)
        requireFact(
            int(deployment["status"], 16) == 1
            and deployment["contractAddress"].lower() == self.market
            and int(deployment["blockNumber"], 16) == int(self.facts["deploymentBlock"])
            and deployment["blockHash"] == self.facts["deploymentBlockHash"],
            "Deployment receipt does not match qualified market",
        )
        compiled = readJson(".scratch/layer3/out/AgentLanceMarket.sol/AgentLanceMarket.json")
        creation = bytes.fromhex(compiled["bytecode"]["object"].removeprefix("0x"))
        requireFact(
            digest(creation) == self.report["build"]["creationCodeHash"],
            "Local immutable build differs from qualification",
        )
        transaction = self.rpc.call("eth_getTransactionByHash", self.report["deploymentTxHash"])
        expectedInput = creation + encode(
            ["address", "address"], [self.facts["identityRegistry"], self.facts["validator"]]
        )
        requireFact(
            transaction and bytes.fromhex(transaction["input"][2:]) == expectedInput,
            "Deployment input/constructor differs from local build",
        )
        requireFact(
            buildEvidence(compiled) == self.report["build"],
            "Source/compiler/settings/layout differ from qualified build",
        )
        self.verifyDependencies(block)
        requireFact(
            self.read("readPolicy", block=block)
            == (
                self.chainId,
                self.market,
                self.facts["identityRegistry"],
                self.facts["validator"],
                1,
            ),
            "Market readPolicy differs from report",
        )
        requireFact(
            self.actors["validator"] == self.facts["validator"],
            "Validator keystore role differs from report",
        )
        challenge = encode_defunct(
            text=(
                f"AgentLance L3 fixture control:{self.chainId}:{self.market}:"
                + secrets.token_hex(32)
            )
        )
        signed = self.accounts["validator"].sign_message(challenge)
        requireFact(
            Account.recover_message(challenge, signature=signed.signature).lower()
            == self.facts["validator"],
            "Validator challenge failed",
        )
        requireFact(
            self.evidence["validatorVerification"]["validator"] == self.facts["validator"],
            "Retained validator evidence belongs to another address",
        )
        self.journal["validatorChallenge"] = {
            "message": challenge.body.decode(),
            "signature": "0x" + signed.signature.hex(),
        }
        for name in names:
            identity = self.verifyIdentity(name, block, fresh=True)
            for role in (identity["signer"], identity["payout"], identity["permitSigner"]):
                requireFact(role in self.accounts, f"Controlled signing role missing: {role}")
            if name == "contractOwner":
                code = self.rpc.call("eth_getCode", self.actors[identity["owner"]], block["number"])
                requireFact(
                    code != "0x", "Contract-owner scenario requires an actual ERC-1271 owner"
                )
        if scenario == "parent-child":
            requireFact(
                self.config["identities"]["parent"]["signer"] == "signer",
                "Parent identity must use the configured child-funding signer role",
            )
        self.journal["qualificationBlock"] = {
            key: block[key] for key in ("number", "hash", "timestamp")
        }
        self.journal["status"] = "running"
        self.save()

    def taskRef(self, taskId):
        return {"chainId": str(self.chainId), "market": self.market, "taskId": str(taskId)}

    def wireRef(self, ref):
        return self.codec.convert({"$ref": "#/$defs/TaskRef"}, ref)

    def artifact(self, name):
        return deepcopy(self.config["artifacts"][name]["ref"])

    def terms(self, *, parent=False, child=False):
        now = int(finalizedBlock(self.rpc)["timestamp"], 16)
        window = self.window
        return {
            "policyVersion": 1,
            "refundAddress": self.actors["signer" if child else "requester"],
            "asset": {"kind": "NATIVE", "symbol": "MON", "decimals": 18},
            "budgetAtoms": "200" if parent else "60" if child else "100",
            "input": self.artifact("input"),
            "outputSchema": self.artifact("outputSchema"),
            "taskFamily": "structured-output-v1",
            "alphaNum": "1",
            "alphaDen": "200" if parent else "80" if child else "100",
            "biddingClose": str(now + window),
            "allocationBy": str(now + 2 * window),
            "acceptBy": str(now + 3 * window),
            "resultBy": str(now + (20 if parent else 6) * window),
            "validationBy": str(now + (22 if parent else 8) * window),
            "validator": self.facts["validator"],
            "validationPolicy": self.artifact("validationPolicy"),
            "delegation": {
                "maxDepth": 1 if parent or child else 0,
                "maxChildren": 1 if parent else 0,
            },
            "retryOf": None,
        }

    def waitUntil(self, timestamp, deadline):
        while time.time() < deadline:
            block = finalizedBlock(self.rpc)
            if int(block["timestamp"], 16) >= timestamp:
                return
            time.sleep(2)
        raise PendingFinality("Task window elapsed while waiting for a finalized timestamp")

    def command(self, name, data, role, deadline, *, value=0, expectedError=None):
        command = {"schemaVersion": 1, "command": name, "input": data}
        validateRecord(command, "Command", self.codec.schema)
        block = finalizedBlock(self.rpc)
        requireFact(
            time.time() < deadline and int(block["timestamp"], 16) < deadline,
            "Action deadline already reached",
        )
        if name in {"createTask", "createChildTask", "submitBid"}:
            self.verifyDependencies(block)
        beforeTask = None
        if expectedError and name != "withdrawCredit":
            beforeTask = self.read("readTask", [self.wireRef(targetRef(name, data))], block)
        account = self.accounts[role]
        call = {
            "from": account.address.lower(),
            "to": self.market,
            "data": self.codec.commandData(name, data),
            "value": hex(value),
        }
        try:
            self.rpc.call("eth_call", call, block["number"])
        except RpcError as failure:
            expected = (
                "0x"
                + (
                    keccak(text="ProtocolError(uint16)")[:4]
                    + encode(["uint16"], [self.codec.catalog["errors"].get(expectedError, 65535)])
                ).hex()
            )
            requireFact(
                expectedError is not None and failure.error.get("data") == expected,
                "Preflight returned an unexpected protocol rejection",
            )
        else:
            requireFact(expectedError is None, "Adversarial command unexpectedly succeeded")
        gasPrice = int(self.rpc.call("eth_gasPrice"), 16)
        requireFact(
            0 < gasPrice <= int(self.config["maxGasPriceWei"]),
            "Gas price exceeds configured spend cap",
        )
        gas = (
            self.config["maxGas"]
            if expectedError
            else (int(self.rpc.call("eth_estimateGas", call), 16) * 12 + 9) // 10
        )
        requireFact(
            21000 <= gas <= self.config["maxGas"],
            "Estimated transaction exceeds configured gas cap",
        )
        requireFact(
            int(self.rpc.call("eth_getBalance", call["from"], block["number"]), 16)
            >= value + gas * gasPrice,
            f"Insufficient value-plus-fee funding for {role}",
        )
        nonce = int(self.rpc.call("eth_getTransactionCount", call["from"], "pending"), 16)
        transaction = {
            "chainId": self.chainId,
            "to": to_checksum_address(self.market),
            "data": call["data"],
            "value": value,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": gasPrice,
        }
        signed = account.sign_transaction(transaction)
        txHash = "0x" + signed.hash.hex()
        entry = {
            "hash": txHash,
            "command": name,
            "from": call["from"],
            "status": "broadcast-outcome-pending",
        }
        self.journal["transactions"].append(entry)
        self.save()  # Keep the exact recovery hash even if the send request times out.
        requireFact(time.time() < deadline, "Action deadline reached before broadcast")
        try:
            returned = self.rpc.call("eth_sendRawTransaction", "0x" + signed.raw_transaction.hex())
        except (RpcError, httpx.HTTPError):
            raise PendingFinality(
                "Broadcast outcome unknown; retain hash and reconcile without resending"
            ) from None
        requireFact(returned.lower() == txHash, "RPC returned an unexpected transaction hash")
        receipt, confirmed = waitFinalized(self.rpc, txHash, deadline)
        success = int(receipt["status"], 16) == 1
        if expectedError is not None:
            trace = self.rpc.call("debug_traceTransaction", txHash, {"tracer": "callTracer"})
            expected = (
                "0x"
                + (
                    keccak(text="ProtocolError(uint16)")[:4]
                    + encode(["uint16"], [self.codec.catalog["errors"][expectedError]])
                ).hex()
            )
            requireFact(
                trace.get("output") == expected, "Mined rejection bytes differ from expected error"
            )
        requireFact(
            success == (expectedError is None), "Mined transaction status differs from preflight"
        )
        events = [
            self.codec.event(log)
            for log in receipt["logs"]
            if log["address"].lower() == self.market
        ]
        requireFact(len(events) == (1 if success else 0), "Unexpected canonical market event count")
        entry.update(
            status="finalized",
            blockNumber=receipt["blockNumber"],
            blockHash=receipt["blockHash"],
            gasUsed=receipt["gasUsed"],
            events=events,
            expectedError=expectedError,
            finality={key: confirmed[key] for key in ("number", "hash", "timestamp")},
        )
        if success and events[0]["name"] == "TaskSettled":
            settled = events[0]["payload"]["receipt"]
            actual = self.read("readSettlement", [self.wireRef(settled["taskRef"])], confirmed)[0]
            decoded = self.codec.convert(
                {"anyOf": [{"$ref": "#/$defs/SettlementReceipt"}, {"type": "null"}]},
                actual,
                decoding=True,
            )
            requireFact(decoded == settled, "Stored settlement differs from finalized event")
            requireFact(
                int(settled["paidAtoms"]) + int(settled["refundAtoms"])
                == int(settled["budgetAtoms"]),
                "Settlement does not conserve its budget",
            )
            for recipient, amount in (
                (settled["payout"], int(settled["paidAtoms"])),
                (settled["refundAddress"], int(settled["refundAtoms"])),
            ):
                if amount:
                    self.createdCredits[recipient] = self.createdCredits.get(recipient, 0) + amount
            self.receipts.append(settled)
            self.journal["settlements"].append(settled)
        if expectedError and beforeTask is not None:
            requireFact(
                self.read("readTask", [self.wireRef(targetRef(name, data))], confirmed)
                == beforeTask,
                "Rejected transaction changed its task records",
            )
        if success:
            self.verifyEvent(events[0], confirmed)
        self.verifyAccounting(confirmed)
        self.save()
        return events[0] if events else None

    def verifyEvent(self, event, block):
        name, payload = event["name"], event["payload"]
        if name == "CreditWithdrawn":
            requireFact(payload["receiver"] == self.actors["receiver"], "Wrong withdrawal receiver")
            return
        field = {
            "TaskCreated": "task",
            "BidAccepted": "bid",
            "TaskAwarded": "allocation",
            "ResultSubmitted": "result",
            "TaskSettled": "receipt",
        }.get(name)
        record = payload[field] if field else payload
        ref = record.get("taskRef") or record.get("executionRef", {}).get("taskRef")
        if name == "BidAccepted":
            ref = record["offer"]["taskRef"]
            agent = self.codec.convert({"$ref": "#/$defs/AgentRef"}, record["offer"]["agentRef"])
            raw = self.read("readBid", [self.wireRef(ref), agent], block)[0]
            stored = self.codec.convert(
                {"anyOf": [{"$ref": "#/$defs/Bid"}, {"type": "null"}]}, raw, decoding=True
            )
            requireFact(stored == record, "Stored bid differs from finalized event")
        view = self.read("readTask", [self.wireRef(ref)], block)[0]
        status = {
            "TaskCreated": 0,
            "BidAccepted": 0,
            "TaskAwarded": 1,
            "AwardAccepted": 2,
            "ResultSubmitted": 3,
            "TaskSettled": 4,
        }[name]
        requireFact(view[1] == status, "Finalized task state differs from emitted transition")
        if name in ("TaskCreated", "TaskAwarded", "ResultSubmitted"):
            kind, index = {
                "TaskCreated": ("TaskSpec", 0),
                "TaskAwarded": ("Allocation", 2),
                "ResultSubmitted": ("ResultCommitment", 9),
            }[name]
            source = view[index] if index == 0 else view[index][1]
            requireFact(
                self.codec.convert({"$ref": "#/$defs/" + kind}, source, decoding=True) == record,
                "Stored canonical record differs from finalized event",
            )
        if name == "TaskSettled" and record["winner"]:
            agent = self.codec.convert({"$ref": "#/$defs/AgentRef"}, record["winner"])
            counters = self.read(
                "readCounters", [agent, "structured-output-v1", int(block["number"], 16)], block
            )
            expected = {"NONE": (0, 0), "SUCCESS": (1, 0), "FAILURE": (0, 1)}[
                record["counterEffect"]
            ]
            requireFact(counters == expected, "Fresh identity counter effect differs from receipt")
            spec = self.codec.convert({"$ref": "#/$defs/TaskSpec"}, view[0], decoding=True)
            snapshot = int(spec["reputationSnapshotBlock"])
            requireFact(
                self.read("readCounters", [agent, "structured-output-v1", snapshot], block)
                == (0, 0),
                "Creation snapshot counters changed after settlement",
            )
            self.journal.setdefault("counters", []).append(
                {
                    "agentRef": record["winner"],
                    "blockNumber": block["number"],
                    "snapshotBlock": str(snapshot),
                    "snapshotSuccesses": "0",
                    "snapshotFailures": "0",
                    "successes": str(counters[0]),
                    "failures": str(counters[1]),
                }
            )

    def verifyAccounting(self, block):
        taskCount, deposited, escrow, credit, withdrawn = self.read("readAccounting", block=block)
        requireFact(deposited == escrow + credit + withdrawn, "Aggregate money conservation failed")
        balance = int(self.rpc.call("eth_getBalance", self.market, block["number"]), 16)
        requireFact(balance >= escrow + credit, "Market liabilities exceed its native balance")
        self.journal["accounting"] = {
            "taskCount": str(taskCount),
            "depositedAtoms": str(deposited),
            "escrowAtoms": str(escrow),
            "creditAtoms": str(credit),
            "withdrawnAtoms": str(withdrawn),
        }

    def create(self, *, parent=False, parentRef=None):
        terms = self.terms(parent=parent, child=parentRef is not None)
        data = {"terms": terms}
        if parentRef is not None:
            data["parentRef"] = parentRef
        event = self.command(
            "createChildTask" if parentRef else "createTask",
            data,
            "signer" if parentRef else "requester",
            int(terms["biddingClose"]),
            value=int(terms["budgetAtoms"]),
        )
        requireFact(
            event["name"] == "TaskCreated" and event["payload"]["task"]["terms"] == terms,
            "Task creation did not retain exact terms",
        )
        return event["payload"]["task"]["taskRef"], terms

    def bid(self, ref, terms, identityName, *, direct=False, domainChange=None, invalid=False):
        identity = self.verifyIdentity(identityName, finalizedBlock(self.rpc))
        offer = {
            "taskRef": ref,
            "agentRef": {
                "chainId": str(self.chainId),
                "identityRegistry": self.facts["identityRegistry"],
                "agentId": identity["agentId"],
            },
            "executionSigner": self.actors[identity["signer"]],
            "payout": self.actors[identity["payout"]],
            "bidAtoms": "10" if identityName == "child" else "20",
            "profileDigest": identity["profileDigest"],
        }
        permit, signature = None, None
        if not direct:
            permit = {
                **deepcopy(offer),
                "owner": self.actors[identity["owner"]],
                "nonce": str(secrets.randbits(256)),
                "expiry": terms["biddingClose"],
            }
            domain = {
                "name": "AgentLance",
                "version": "1",
                "chainId": self.chainId,
                "verifyingContract": self.market,
            }
            typed = buildBidPermitTypedData(permit, domain, self.signingTypes)
            typed["domain"].update(domainChange or {})
            signature = (
                "0x"
                + self.accounts[identity["permitSigner"]]
                .sign_message(encode_typed_data(full_message=typed))
                .signature.hex()
            )
            if invalid:
                signature = "0x" + "00" * 65
        data = {"offer": offer, "permit": permit, "signature": signature}
        event = self.command(
            "submitBid",
            data,
            identity["owner"] if direct else "relayer",
            int(terms["biddingClose"]),
            expectedError="INVALID_SIGNATURE" if domainChange or invalid else None,
        )
        if event:
            requireFact(
                event["name"] == "BidAccepted" and event["payload"]["bid"]["offer"] == offer,
                "Bid record differs from frozen offer",
            )
        return identity

    def allocate(self, ref, terms):
        self.waitUntil(int(terms["biddingClose"]), int(terms["allocationBy"]))
        return self.command("allocateTask", {"taskRef": ref}, "relayer", int(terms["allocationBy"]))

    def accept(self, ref, terms, identity, *, reserve="0", unauthorized=False):
        return self.command(
            "acceptAward",
            {"executionRef": {"taskRef": ref, "awardId": 1}, "ownWorkReserveAtoms": reserve},
            "relayer" if unauthorized else identity["signer"],
            int(terms["acceptBy"]),
            expectedError="UNAUTHORIZED" if unauthorized else None,
        )

    def result(self, ref, terms, identity):
        return self.command(
            "submitResult",
            {"executionRef": {"taskRef": ref, "awardId": 1}, "artifact": self.artifact("result")},
            identity["signer"],
            int(terms["resultBy"]),
        )

    def verdict(self, ref, terms, identity, *, passed=True, malformed=None):
        record = {
            "schemaVersion": 1,
            "executionRef": {"taskRef": ref, "awardId": 1},
            "agentRef": {
                "chainId": str(self.chainId),
                "identityRegistry": self.facts["identityRegistry"],
                "agentId": identity["agentId"],
            },
            "resultDigest": self.artifact("result")["digest"],
            "validationPolicyDigest": terms["validationPolicy"]["digest"],
            "verdict": "PASS" if passed else "FAIL",
            "evidence": self.artifact("evidence"),
            "validator": self.facts["validator"],
            "nonce": str(secrets.randbits(256)),
            "expiry": terms["validationBy"],
            "signature": "0x01",
        }
        message = {
            "taskId": ref["taskId"],
            "identityRegistry": self.facts["identityRegistry"],
            "agentId": identity["agentId"],
            "awardId": 1,
            "resultDigest": record["resultDigest"],
            "validationPolicyDigest": record["validationPolicyDigest"],
            "verdict": 1 if passed else 0,
            "evidenceDigest": record["evidence"]["digest"],
            "validator": self.facts["validator"],
            "nonce": record["nonce"],
            "expiry": record["expiry"],
        }
        typed = {
            "types": {
                name: self.signingTypes[name] for name in ("EIP712Domain", "ValidationVerdict")
            },
            "primaryType": "ValidationVerdict",
            "domain": {
                "name": "AgentLance",
                "version": "1",
                "chainId": self.chainId,
                "verifyingContract": self.market,
            },
            "message": message,
        }
        signature = (
            self.accounts["validator"].sign_message(encode_typed_data(full_message=typed)).signature
        )
        if malformed == "high-s":
            order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
            signature = (
                signature[:32]
                + (order - int.from_bytes(signature[32:64], "big")).to_bytes(32, "big")
                + bytes([55 - signature[64]])
            )
        record["signature"] = "0x" + signature.hex()
        if malformed == "result":
            record["resultDigest"] = "0x" + "ff" * 32
        return self.command(
            "settleVerdict",
            {"record": record},
            "relayer",
            int(terms["validationBy"]),
            expectedError={"result": "RESULT_MISMATCH", "high-s": "INVALID_SIGNATURE"}.get(
                malformed
            ),
        )

    def expire(self, ref, terms, cutoff):
        deadline = int(terms[cutoff]) + self.window
        self.waitUntil(int(terms[cutoff]), deadline)
        return self.command("expireTask", {"taskRef": ref}, "relayer", deadline)

    def finish(self):
        block = finalizedBlock(self.rpc)
        receiverBefore = int(
            self.rpc.call("eth_getBalance", self.actors["receiver"], block["number"]), 16
        )
        for owner, amount in self.createdCredits.items():
            role = next(name for name, address in self.actors.items() if address == owner)
            requireFact(
                self.read("readCredit", [owner])[0] >= amount,
                "New settlement credit is unavailable",
            )
            event = self.command(
                "withdrawCredit",
                {"receiver": self.actors["receiver"], "amountAtoms": str(amount)},
                role,
                time.time() + self.window,
            )
            requireFact(
                event["name"] == "CreditWithdrawn"
                and event["payload"]["owner"] == owner
                and event["payload"]["amountAtoms"] == str(amount),
                "Withdrawal event mismatch",
            )
        block = finalizedBlock(self.rpc)
        receiverAfter = int(
            self.rpc.call("eth_getBalance", self.actors["receiver"], block["number"]), 16
        )
        requireFact(
            receiverAfter - receiverBefore == sum(self.createdCredits.values()),
            "Withdrawal receiver balance delta differs from new settlement credits",
        )
        self.journal["withdrawnScenarioAtoms"] = str(sum(self.createdCredits.values()))
        self.journal["status"] = "passed"
        self.save()

    def runScenario(self, scenario):
        self.journal["scenario"] = scenario
        if scenario == "parent-child":
            parent, terms = self.create(parent=True)
            identity = self.bid(parent, terms, "parent")
            self.allocate(parent, terms)
            self.accept(parent, terms, identity, reserve="20")
            child, childTerms = self.create(parentRef=parent)
            childIdentity = self.bid(child, childTerms, "child")
            self.allocate(child, childTerms)
            self.accept(child, childTerms, childIdentity)
            self.result(child, childTerms, childIdentity)
            self.verdict(child, childTerms, childIdentity)
            self.result(parent, terms, identity)
            self.verdict(parent, terms, identity)
            requireFact(
                [r["paidAtoms"] for r in self.receipts] == ["40", "100"], "Tree payment mismatch"
            )
        elif scenario == "timeouts":
            for ending in (
                "CANCELLED",
                "UNALLOCATED",
                "ALLOCATION_EXPIRED",
                "NO_SHOW",
                "EXECUTION_TIMEOUT",
                "VALIDATOR_TIMEOUT",
                "VALIDATION_FAILED",
            ):
                ref, terms = self.create()
                if ending == "CANCELLED":
                    self.command(
                        "cancelTask", {"taskRef": ref}, "requester", int(terms["biddingClose"])
                    )
                elif ending == "UNALLOCATED":
                    self.allocate(ref, terms)
                elif ending == "ALLOCATION_EXPIRED":
                    self.expire(ref, terms, "allocationBy")
                else:
                    name = {
                        "NO_SHOW": "noShow",
                        "EXECUTION_TIMEOUT": "executionTimeout",
                        "VALIDATOR_TIMEOUT": "validatorTimeout",
                        "VALIDATION_FAILED": "validationFailed",
                    }[ending]
                    identity = self.bid(ref, terms, name, direct=ending == "VALIDATION_FAILED")
                    self.allocate(ref, terms)
                    if ending == "NO_SHOW":
                        self.expire(ref, terms, "acceptBy")
                    else:
                        self.accept(ref, terms, identity)
                        if ending == "EXECUTION_TIMEOUT":
                            self.expire(ref, terms, "resultBy")
                        else:
                            self.result(ref, terms, identity)
                            if ending == "VALIDATOR_TIMEOUT":
                                self.expire(ref, terms, "validationBy")
                            else:
                                self.verdict(ref, terms, identity, passed=False)
                requireFact(
                    self.receipts[-1]["reason"] == ending
                    and self.receipts[-1]["refundAtoms"] == "100",
                    "Timeout/refund receipt mismatch",
                )
        else:
            for name in ["malicious", "contractOwner"] if scenario == "malicious" else ["solo"]:
                ref, terms = self.create()
                if scenario == "malicious":
                    self.bid(ref, terms, name, domainChange={"chainId": self.chainId + 1})
                    self.bid(ref, terms, name, invalid=True)
                identity = self.bid(ref, terms, name)
                self.allocate(ref, terms)
                if scenario == "malicious":
                    self.accept(ref, terms, identity, unauthorized=True)
                self.accept(ref, terms, identity)
                self.result(ref, terms, identity)
                if scenario == "malicious":
                    self.verdict(ref, terms, identity, malformed="result")
                    self.verdict(ref, terms, identity, malformed="high-s")
                self.verdict(ref, terms, identity)
                requireFact(
                    self.receipts[-1]["reason"] == "SUCCESS"
                    and self.receipts[-1]["paidAtoms"] == "50",
                    "Solo payment mismatch",
                )
        self.finish()


def runTestnet(reportPath, scenario):
    requireFact(scenario in SCENARIOS, "Choose one named testnet scenario")
    verifyToolchain()
    subprocess.run(
        [toolPath("forge"), "build", "--locked", "--offline", "--quiet"], cwd=ROOT, check=True
    )
    reportPath = Path(reportPath).resolve()
    report = parseJson(reportPath.read_bytes())
    configPath = os.environ.get("AGENTLANCE_L3_ACTORS")
    requireFact(
        bool(configPath), "AGENTLANCE_L3_ACTORS must name local actor/evidence configuration"
    )
    configPath = Path(configPath).resolve()
    config = parseJson(configPath.read_bytes())
    manifest = (
        parseJson((configPath.parent / config["completeManifest"]).read_bytes())
        if config.get("completeManifest")
        else None
    )
    validateDeploymentReport(report, manifest)
    evidence = loadEvidence(report, config, configPath.parent)
    codec = WireCodec()
    for artifact in config["artifacts"].values():
        validateRecord(artifact["ref"], "ContentRef", codec.schema)
        raw = (configPath.parent / artifact["file"]).read_bytes()
        requireFact(
            len(raw) <= 1024 * 1024 and digest(raw) == artifact["ref"]["digest"],
            "Fixture artifact bytes do not match their committed digest",
        )
    for identity in config["identities"].values():
        raw = (configPath.parent / identity["profileFile"]).read_bytes()
        requireFact(
            len(raw) <= 1024 * 1024 and digest(raw) == identity["profileDigest"],
            "Retained Agent Card bytes differ from profileDigest",
        )
    accounts, addresses = loadActors(config, configPath.parent)
    scratch = ROOT / ".scratch/layer3"
    scratch.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="testnet-", dir=scratch)) / "scenario.json"
    rpc = Rpc(report["manifestFacts"]["rpcUrls"][0])
    run = TestnetRun(rpc, report, config, accounts, addresses, evidence, output)
    try:
        run.preflight(scenario)
        run.runScenario(scenario)
    except Exception as error:
        run.journal["status"] = "WAIT" if isinstance(error, PendingFinality) else "failed"
        # Never serialize provider errors or configuration containing secrets.
        run.journal["failure"] = {
            "type": type(error).__name__,
            "message": str(error)
            if isinstance(error, (QualificationError, PendingFinality))
            else "Scenario failed; inspect local inputs and retained transaction hashes",
        }
        run.save()
        raise QualificationError(
            f"Testnet scenario did not complete; retained evidence: {output}"
        ) from None
    finally:
        rpc.close()
    return output
