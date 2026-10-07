"""Local EVM test transport and an independently advanced L1 comparison oracle.

This is test infrastructure, not a production MarketPort or a finality claim.
Public synthetic keys are used only on an isolated loopback Anvil.
"""

import json
import socket
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import httpx
from eth_abi import decode, encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from modules.adapters.chain.codec import WireCodec as ProtocolCodec
from modules.adapters.chain.codec import abiType, callData
from modules.agent_client.signing import buildBidPermitTypedData, verifyEoa
from modules.domain.records import agentKey, taskKey, validateRecord
from modules.market_core.reputation import snapshotCounters
from modules.market_core.state import (
    Applied,
    CommandContext,
    CorePolicy,
    CoreState,
    IdentityObservation,
    Rejected,
    SignatureObservation,
)
from modules.market_core.transitions import applyCommand
from scripts.layer3_tools import toolPath

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x" + "00" * 20


def readJson(path):
    return json.loads((ROOT / path).read_text())


class WireCodec(ProtocolCodec):
    def __init__(self):
        super().__init__(
            readJson("specs/schemas/protocol.schema.json"),
            readJson("specs/catalog.json"),
            readJson("specs/protocol.abi.json"),
        )


class RpcError(RuntimeError):
    def __init__(self, error):
        self.error = error
        super().__init__(str(error))


class Rpc:
    def __init__(self, url):
        self.client = httpx.Client(base_url=url, timeout=10, trust_env=False)
        self.sequence = 0

    def call(self, method, *params):
        self.sequence += 1
        response = self.client.post(
            "",
            json={"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": list(params)},
        )
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RpcError(data["error"])
        return data["result"]

    def close(self):
        self.client.close()

    def receipt(self, transaction):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            receipt = self.call("eth_getTransactionReceipt", transaction)
            if receipt is not None:
                return receipt
            time.sleep(0.02)
        raise TimeoutError(f"Local transaction not mined: {transaction}")


@contextmanager
def localNode(workDir):
    workDir = Path(workDir)
    workDir.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (workDir / "anvil.log").open("w") as log:
        process = subprocess.Popen(
            [
                toolPath("anvil"),
                "--network",
                "monad",
                "--hardfork",
                "MonadTen",
                "--chain-id",
                "31337",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--silent",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        rpc = Rpc(f"http://127.0.0.1:{port}")
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    assert int(rpc.call("eth_chainId"), 16) == 31337
                    break
                except httpx.TransportError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Monad Anvil failed; see {workDir / 'anvil.log'}"
                        ) from None
                    time.sleep(0.05)
            rpc.call("anvil_setBlockTimestampInterval", 0)
            yield rpc
        finally:
            rpc.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def artifact(name, sourceName=None):
    # Foundry's locked build output is generated, never treated as a golden ABI.
    return readJson(f".scratch/layer3/out/{sourceName or name}.sol/{name}.json")


class Layer3Rig:
    def __init__(self, rpc, marketName="AgentLanceMarket"):
        self.rpc = rpc
        self.codec = WireCodec()
        self.types = readJson("specs/signing/types.json")
        self.keys = {
            name: number.to_bytes(32, "big")
            for number, name in enumerate(
                [
                    "requester",
                    "owner",
                    "signer",
                    "payout",
                    "relayer",
                    "validator",
                    "other",
                    "childOwner",
                    "childSigner",
                    "childPayout",
                    "receiver",
                ],
                101,
            )
        }
        self.actors = {
            name: Account.from_key(key).address.lower() for name, key in self.keys.items()
        }
        for address in self.actors.values():
            rpc.call("anvil_setBalance", address, hex(10**24))
            rpc.call("anvil_impersonateAccount", address)
        self.registry = self.deploy("TestIdentityRegistry")
        self.market = self.deploy(
            marketName, ["address", "address"], [self.registry, self.actors["validator"]]
        )
        self.policy = CorePolicy(31337, self.market, self.registry, self.actors["validator"])
        self.state = CoreState()
        self.identities = {}
        self.knownNonces = set()
        self.forcedExcess = 0
        self.events = []
        self.transactions = []
        self.readAbi = readJson("specs/contracts.read.abi.json")

    def deploy(self, name, types=(), values=(), *, sourceName=None):
        data = artifact(name, sourceName)["bytecode"]["object"]
        data = data if data.startswith("0x") else "0x" + data
        receipt = self.rpc.receipt(
            self.rpc.call(
                "eth_sendTransaction",
                {
                    "from": self.actors["requester"],
                    "data": data + encode(types, values).hex(),
                    "gas": hex(30_000_000),
                },
            )
        )
        assert int(receipt["status"], 16) == 1, (name, receipt)
        return receipt["contractAddress"].lower()

    def external(self, address, name, types, values, caller="requester"):
        data = (
            "0x"
            + (keccak(text=name + "(" + ",".join(types) + ")")[:4] + encode(types, values)).hex()
        )
        receipt = self.rpc.receipt(
            self.rpc.call(
                "eth_sendTransaction",
                {
                    "from": self.actors.get(caller, caller),
                    "to": address,
                    "data": data,
                    "gas": hex(5_000_000),
                },
            )
        )
        assert int(receipt["status"], 16) == 1, receipt
        return receipt

    def register(self, agentId=0, owner="owner", payout="payout"):
        owner, payout = self.actors.get(owner, owner), self.actors.get(payout, payout)
        self.external(
            self.registry,
            "setIdentity",
            ["uint256", "address", "address"],
            [agentId, owner, payout],
        )
        self.identities[agentId] = (owner, payout)
        return self.agentRef(agentId)

    def transfer(self, agentId, owner="other", wallet=ZERO):
        owner = self.actors.get(owner, owner)
        self.external(
            self.registry,
            "setIdentity",
            ["uint256", "address", "address"],
            [agentId, owner, wallet],
        )
        self.identities[agentId] = (owner, wallet)

    def agentRef(self, agentId):
        return {"chainId": "31337", "identityRegistry": self.registry, "agentId": str(agentId)}

    def taskRef(self, taskId):
        return {"chainId": "31337", "market": self.market, "taskId": str(taskId)}

    def now(self):
        return int(self.rpc.call("eth_getBlockByNumber", "latest", False)["timestamp"], 16)

    def advance(self, timestamp):
        assert timestamp >= self.now()
        self.rpc.call("evm_setNextBlockTimestamp", timestamp)
        self.rpc.call("evm_mine")

    def terms(self, *, budget=100, denominator=100, delegation=False, base=None, retry=None):
        base = self.now() if base is None else base
        content = {"uri": "ipfs://layer3-fixture", "digest": "0x" + "01" * 32}
        return {
            "policyVersion": 1,
            "input": deepcopy(content),
            "outputSchema": deepcopy(content),
            "validationPolicy": deepcopy(content),
            "taskFamily": "structured-output-v1",
            "budgetAtoms": str(budget),
            "asset": {"kind": "NATIVE", "symbol": "MON", "decimals": 18},
            "alphaNum": "1",
            "alphaDen": str(denominator),
            "biddingClose": str(base + 30),
            "allocationBy": str(base + 60),
            "acceptBy": str(base + 180),
            "resultBy": str(base + (1200 if delegation else 300)),
            "validationBy": str(base + (1320 if delegation else 420)),
            "validator": self.actors["validator"],
            "refundAddress": self.actors["requester"],
            "delegation": {
                "maxDepth": 1 if delegation else 0,
                "maxChildren": 4 if delegation else 0,
            },
            "retryOf": retry,
        }

    def typed(self, primary, record):
        domain = {
            "name": "AgentLance",
            "version": "1",
            "chainId": 31337,
            "verifyingContract": self.market,
        }
        if primary == "BidPermit":
            return buildBidPermitTypedData(record, domain, self.types)
        message = {
            "taskId": record["executionRef"]["taskRef"]["taskId"],
            "identityRegistry": record["agentRef"]["identityRegistry"],
            "agentId": record["agentRef"]["agentId"],
            "awardId": record["executionRef"]["awardId"],
            "resultDigest": record["resultDigest"],
            "validationPolicyDigest": record["validationPolicyDigest"],
            "verdict": self.codec.catalog["verdicts"][record["verdict"]],
            "evidenceDigest": record["evidence"]["digest"],
            "validator": record["validator"],
            "nonce": record["nonce"],
            "expiry": record["expiry"],
        }
        return {
            "types": {k: self.types[k] for k in ("EIP712Domain", primary)},
            "primaryType": primary,
            "domain": domain,
            "message": message,
        }

    def sign(self, primary, record, key):
        return (
            "0x"
            + Account.sign_message(
                encode_typed_data(full_message=self.typed(primary, record)), self.keys[key]
            ).signature.hex()
        )

    def bid(
        self,
        taskId=1,
        agentId=0,
        *,
        amount=20,
        signed=False,
        nonce=0,
        owner="owner",
        signer="signer",
        payout="payout",
    ):
        offer = {
            "taskRef": self.taskRef(taskId),
            "agentRef": self.agentRef(agentId),
            "executionSigner": self.actors[signer],
            "payout": self.actors[payout],
            "bidAtoms": str(amount),
            "profileDigest": "0x" + "00" * 32,
        }
        permit = (
            {
                **deepcopy(offer),
                "owner": self.actors[owner],
                "nonce": str(nonce),
                "expiry": str(self.now() + 2000),
            }
            if signed
            else None
        )
        return {
            "offer": offer,
            "permit": permit,
            "signature": self.sign("BidPermit", permit, owner) if signed else None,
        }

    def verdict(self, taskId=1, *, verdict="PASS", nonce=0):
        task = self.state.tasks[taskKey(self.taskRef(taskId))]
        record = {
            "schemaVersion": 1,
            "executionRef": {"taskRef": self.taskRef(taskId), "awardId": 1},
            "agentRef": deepcopy(task.allocation["winner"]),
            "resultDigest": task.result["artifact"]["digest"],
            "validationPolicyDigest": task.spec["terms"]["validationPolicy"]["digest"],
            "verdict": verdict,
            "evidence": {"uri": "ipfs://fixture-evidence", "digest": "0x" + "02" * 32},
            "validator": self.actors["validator"],
            "nonce": str(nonce),
            "expiry": str(self.now() + 2000),
            "signature": "0x01",
        }
        record["signature"] = self.sign("ValidationVerdict", record, "validator")
        return {"record": record}

    def context(
        self, name, data, caller, value, block, *, signatureValid=None, transferSucceeded=True
    ):
        facts = {
            "caller": caller,
            "valueAtoms": value,
            "blockNumber": int(block["number"], 16),
            "blockTimestamp": int(block["timestamp"], 16),
        }
        if name == "submitBid":
            offer, permit = data["offer"], data["permit"]
            identity = self.identities.get(int(offer["agentRef"]["agentId"]))
            facts["identityObservation"] = IdentityObservation(
                offer["agentRef"],
                identity is not None,
                identity[0] if identity else None,
                identity[1] if identity and identity[1] != ZERO else None,
            )
            if permit:
                typed = self.typed("BidPermit", permit)
                facts["signatureObservation"] = SignatureObservation(
                    "BidPermit",
                    31337,
                    self.market,
                    permit["owner"],
                    permit,
                    data["signature"],
                    verifyEoa(typed, data["signature"], permit["owner"])
                    if signatureValid is None
                    else signatureValid,
                )
                self.knownNonces.add((permit["owner"], "BidPermit", permit["nonce"]))
        if name == "settleVerdict":
            record = data["record"]
            facts["signatureObservation"] = SignatureObservation(
                "ValidationVerdict",
                31337,
                self.market,
                self.actors["validator"],
                record,
                record["signature"],
                verifyEoa(
                    self.typed("ValidationVerdict", record),
                    record["signature"],
                    self.actors["validator"],
                ),
            )
            self.knownNonces.add((self.actors["validator"], "ValidationVerdict", record["nonce"]))
        if name == "withdrawCredit":
            facts["transferSucceeded"] = transferSucceeded
        return CommandContext(**facts)

    def command(
        self,
        name,
        data,
        *,
        caller="relayer",
        value=None,
        expected=None,
        signatureValid=None,
        transferSucceeded=True,
        forwardedData=None,
    ):
        """Advance Solidity and L1 from the same independently prepared command facts.

        signatureValid is only for the fixture ERC-1271 owner's configured decision;
        transferSucceeded describes a known receiver fixture. Neither is inferred
        from transaction success. forwardedData invokes a receiver's real forwarding
        method while preserving its address as the caller of the inner market command.
        """
        if signatureValid is not None:
            assert name == "submitBid" and data["permit"] is not None
            assert type(signatureValid) is bool
        assert type(transferSucceeded) is bool
        caller = self.actors.get(caller, caller)
        if value is None:
            value = (
                int(data["terms"]["budgetAtoms"])
                if name in ("createTask", "createChildTask")
                else 0
            )
        command = {"schemaVersion": 1, "command": name, "input": deepcopy(data)}
        validateRecord(command, "Command", self.codec.schema)
        tx = {
            "from": caller,
            "to": self.market,
            "data": self.codec.commandData(name, data),
            "value": hex(value),
            "gas": hex(30_000_000),
        }
        # Fixture contracts may forward a genuine transaction. The market caller
        # remains that contract in L1; only the outer transaction sender changes.
        if forwardedData is not None:
            assert name == "withdrawCredit" and value == 0
            tx.update({"from": self.actors["requester"], "to": caller, "data": forwardedData})
        receiverBefore = (
            int(self.rpc.call("eth_getBalance", data["receiver"], "latest"), 16)
            if name == "withdrawCredit"
            else None
        )
        error = None
        try:
            self.rpc.call("eth_call", tx, "latest")
        except RpcError as failure:
            error = failure.error.get("data", "0x")
        before = deepcopy(self.state)
        receipt = self.rpc.receipt(self.rpc.call("eth_sendTransaction", tx))
        block = self.rpc.call("eth_getBlockByNumber", receipt["blockNumber"], False)
        result = applyCommand(
            self.state,
            command,
            self.context(
                name,
                data,
                caller,
                value,
                block,
                signatureValid=signatureValid,
                transferSucceeded=transferSucceeded,
            ),
            self.policy,
        )
        assert self.state == before, "Python oracle mutated input"
        events = [
            self.codec.event(log)
            for log in receipt["logs"]
            if log["address"].lower() == self.market
        ]
        if isinstance(result, Rejected):
            assert int(receipt["status"], 16) == 0 and not events, (name, result, receipt)
            assert result.code == expected, (result, expected)
            if value and name not in ("createTask", "createChildTask"):
                assert error == "0x" and result.code == "WRONG_VALUE"
            else:
                wanted = (
                    "0x"
                    + (
                        keccak(text="ProtocolError(uint16)")[:4]
                        + encode(["uint16"], [self.codec.catalog["errors"][result.code]])
                    ).hex()
                )
                assert error == wanted, (name, result.code, error, wanted)
        else:
            assert isinstance(result, Applied) and int(receipt["status"], 16) == 1, (
                name,
                error,
                receipt,
            )
            assert expected is None, expected
            assert error is None
            assert events == list(result.events), (name, events, result.events)
            self.state = result.state
            self.events.extend(events)
        if receiverBefore is not None:
            receiverAfter = int(self.rpc.call("eth_getBalance", data["receiver"], "latest"), 16)
            gasCost = (
                int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
                if data["receiver"] == tx["from"]
                else 0
            )
            received = 0 if isinstance(result, Rejected) else int(data["amountAtoms"])
            assert receiverAfter - receiverBefore == received - gasCost
        self.transactions.append(
            {
                "hash": receipt["transactionHash"],
                "command": name,
                "transactionCaller": tx["from"],
                "transactionTarget": tx["to"],
                "block": receipt["blockNumber"],
                "gasUsed": receipt["gasUsed"],
                "error": result.code if isinstance(result, Rejected) else None,
                "events": events,
            }
        )
        self.compare()
        return result

    def read(self, name, values=()):
        entry = next(v for v in self.readAbi if v["name"] == name)
        raw = self.rpc.call(
            "eth_call", {"to": self.market, "data": callData(entry, values)}, "latest"
        )
        return decode([abiType(f) for f in entry["outputs"]], bytes.fromhex(raw[2:]))

    def wireRef(self, ref, kind="TaskRef"):
        return self.codec.convert({"$ref": f"#/$defs/{kind}"}, ref)

    def compare(self):
        state = self.state
        assert self.read("readPolicy") == (
            31337,
            self.market,
            self.registry,
            self.actors["validator"],
            1,
        )
        taskSchemas = {
            "task": {"$ref": "#/$defs/TaskSpec"},
            "status": {"enum": list(self.codec.catalog["states"])},
        }
        for field, kind in (
            ("allocation", "Allocation"),
            ("winningBid", "Bid"),
            ("result", "ResultCommitment"),
            ("receipt", "SettlementReceipt"),
        ):
            taskSchemas[field] = {"anyOf": [{"$ref": f"#/$defs/{kind}"}, {"type": "null"}]}
        for field in ("ownWorkReserveAtoms", "reservedChildBudgets", "committedChildPayouts"):
            taskSchemas[field] = {"$ref": "#/$defs/Uint96"}
        for field in ("childrenCreated", "activeChildren"):
            taskSchemas[field] = {"type": "integer"}
        entry = next(v for v in self.readAbi if v["name"] == "readTask")
        fields = entry["outputs"][0]["components"]
        for task in state.tasks.values():
            value = self.read("readTask", [self.wireRef(task.spec["taskRef"])])[0]
            decoded = {
                f["name"]: self.codec.convert(taskSchemas[f["name"]], v, decoding=True)
                for f, v in zip(fields, value, strict=True)
            }
            winner = (
                task.bids[agentKey(task.allocation["winner"])]
                if task.allocation and task.allocation["winner"]
                else None
            )
            expected = {
                "task": task.spec,
                "status": task.status,
                "allocation": task.allocation,
                "winningBid": winner,
                "result": task.result,
                "receipt": task.receipt,
                "ownWorkReserveAtoms": str(task.ownWorkReserveAtoms),
                "childrenCreated": task.childrenCreated,
                "activeChildren": task.activeChildren,
                "reservedChildBudgets": str(task.reservedChildBudgets),
                "committedChildPayouts": str(task.committedChildPayouts),
            }
            assert decoded == expected, (decoded, expected)
            receipt = self.read("readSettlement", [self.wireRef(task.spec["taskRef"])])[0]
            assert (
                self.codec.convert(taskSchemas["receipt"], receipt, decoding=True) == task.receipt
            )
            for agentId in self.identities:
                ref = self.agentRef(agentId)
                bid = self.read(
                    "readBid", [self.wireRef(task.spec["taskRef"]), self.wireRef(ref, "AgentRef")]
                )[0]
                schema = {"anyOf": [{"$ref": "#/$defs/Bid"}, {"type": "null"}]}
                assert self.codec.convert(schema, bid, decoding=True) == task.bids.get(
                    agentKey(ref)
                )
        for address in set(self.actors.values()) | set(state.credits):
            assert self.read("readCredit", [address])[0] == state.credits.get(address, 0)
        escrow = sum(t.escrowAtoms for t in state.tasks.values())
        credit = sum(state.credits.values())
        assert self.read("readAccounting") == (
            state.taskCount,
            state.depositedAtoms,
            escrow,
            credit,
            state.withdrawnAtoms,
        )
        assert state.depositedAtoms == escrow + credit + state.withdrawnAtoms
        assert (
            int(self.rpc.call("eth_getBalance", self.market, "latest"), 16)
            == escrow + credit + self.forcedExcess
        )
        block = int(self.rpc.call("eth_blockNumber"), 16)
        for agentId in self.identities:
            ref = self.agentRef(agentId)
            heights = {
                block,
                0,
                *(int(t.spec["reputationSnapshotBlock"]) for t in state.tasks.values()),
            }
            for height in heights:
                expected = snapshotCounters(state.reputation, ref, "structured-output-v1", height)
                assert (
                    self.read(
                        "readCounters",
                        [self.wireRef(ref, "AgentRef"), "structured-output-v1", height],
                    )
                    == expected
                )
        for signer, primary, nonce in self.knownNonces:
            typeString = (
                primary
                + "("
                + ",".join(f["type"] + " " + f["name"] for f in self.types[primary])
                + ")"
            )
            used = (self.market, signer, primary, int(nonce)) in state.usedNonces
            assert (
                self.read("isNonceUsed", [signer, keccak(text=typeString), int(nonce)])[0] == used
            )

    def withdrawAll(self):
        for owner, balance in list(self.state.credits.items()):
            if balance:
                self.command(
                    "withdrawCredit",
                    {"receiver": self.actors["receiver"], "amountAtoms": str(balance)},
                    caller=owner,
                )

    def report(self):
        return {
            "network": "isolated-monad-anvil",
            "chainId": "31337",
            "market": self.market,
            "identityRegistry": self.registry,
            "syntheticRegistry": True,
            "transactions": self.transactions,
            "accounting": dict(
                zip(
                    (
                        "taskCount",
                        "depositedAtoms",
                        "escrowTotalAtoms",
                        "creditTotalAtoms",
                        "withdrawnAtoms",
                    ),
                    map(str, self.read("readAccounting")),
                    strict=True,
                )
            ),
            "testnetVerified": False,
        }
