"""Live-driver boundaries use isolated RPC responses, never a public endpoint."""

import json
import time

import httpx
import pytest
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from scripts.layer3_testnet import (
    PendingFinality,
    QualificationError,
    digest,
    finalizedBlock,
    loadActors,
    loadEvidence,
    waitFinalized,
)
from scripts.layer3_testnet import (
    TestnetRun as LiveRun,
)
from tests.layer3_support import RpcError

HASH = "0x" + "ab" * 32
TX = "0x" + "cd" * 32
ADDRESS = "0x" + "12" * 20
BLOCK = {"number": "0x7", "timestamp": hex(int(time.time())), "hash": HASH}


class FakeRpc:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def call(self, method, *params):
        self.calls.append((method, params))
        value = self.answers[method]
        if isinstance(value, BaseException):
            raise value
        return value(*params) if callable(value) else value


def receipt(transaction=TX, status="0x1"):
    return {
        "transactionHash": transaction,
        "status": status,
        "blockNumber": "0x7",
        "blockHash": HASH,
        "logs": [],
        "gasUsed": "0x5208",
    }


def testFinalityWaitsAndChecksCanonicalBlock():
    now, sleeps = [10], []
    observations = iter([None, receipt()])
    rpc = FakeRpc(
        {"eth_getBlockByNumber": BLOCK, "eth_getTransactionReceipt": lambda _: next(observations)}
    )

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    result, block = waitFinalized(rpc, TX, 20, clock=lambda: now[0], sleep=sleep)
    assert result == receipt() and block == BLOCK and sleeps == [2]
    assert ("eth_getBlockByNumber", ("0x7", False)) in rpc.calls
    assert all("latest" not in args for _, args in rpc.calls)


@pytest.mark.parametrize("answer", [None, RpcError({"code": -32601, "message": "unsupported"})])
def testFinalityUnavailableDoesNotFallBack(answer):
    rpc = FakeRpc({"eth_getBlockByNumber": answer})
    with pytest.raises(PendingFinality, match="WAIT"):
        finalizedBlock(rpc)
    assert rpc.calls == [("eth_getBlockByNumber", ("finalized", False))]


def testFinalityConflictAndDeadline():
    conflicting = receipt()
    conflicting["blockHash"] = TX
    rpc = FakeRpc({"eth_getBlockByNumber": BLOCK, "eth_getTransactionReceipt": conflicting})
    with pytest.raises(QualificationError, match="conflicts"):
        waitFinalized(rpc, TX, 20, clock=lambda: 10)
    with pytest.raises(PendingFinality, match="deadline"):
        waitFinalized(rpc, TX, 10, clock=lambda: 10)
    rpc = FakeRpc({"eth_getBlockByNumber": dict(BLOCK)})
    finalizedBlock(rpc)
    rpc.answers["eth_getBlockByNumber"] = {**BLOCK, "number": "0x6"}
    with pytest.raises(QualificationError, match="regressed"):
        finalizedBlock(rpc)
    rpc.answers["eth_getBlockByNumber"] = {**BLOCK, "hash": TX}
    with pytest.raises(QualificationError, match="hash changed"):
        finalizedBlock(rpc)


def makeRun(tmp_path, rpc):
    account = Account.from_key(keccak(text="driver test only never fund"))
    config = {"windowSeconds": 120, "maxGas": 100000, "maxGasPriceWei": "10"}
    report = {"manifestFacts": {"chainId": "10143", "market": ADDRESS}}
    return LiveRun(
        rpc,
        report,
        config,
        {"requester": account},
        {"requester": account.address.lower(), "receiver": "0x" + "34" * 20},
        {},
        tmp_path / "scenario.json",
    )


def testBroadcastTimeoutRetainsRecoveryHashWithoutSecrets(tmp_path):
    secret = "provider-password-must-not-escape"
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": BLOCK,
            "eth_call": "0x",
            "eth_estimateGas": "0x5208",
            "eth_gasPrice": "0x1",
            "eth_getBalance": hex(10**12),
            "eth_getTransactionCount": "0x0",
            "eth_sendRawTransaction": httpx.ReadTimeout(secret),
        }
    )
    run = makeRun(tmp_path, rpc)
    with pytest.raises(PendingFinality, match="without resending") as failure:
        run.command(
            "withdrawCredit",
            {"receiver": run.actors["receiver"], "amountAtoms": "1"},
            "requester",
            time.time() + 30,
        )
    saved = run.output.read_text()
    assert secret not in saved and secret not in str(failure.value)
    assert run.accounts["requester"].key.hex() not in saved
    transaction = json.loads(saved)["transactions"][0]
    raw = next(args[0] for method, args in rpc.calls if method == "eth_sendRawTransaction")
    assert transaction["hash"] == digest(bytes.fromhex(raw[2:]))
    assert transaction["status"] == "broadcast-outcome-pending"
    assert sum(method == "eth_sendRawTransaction" for method, _ in rpc.calls) == 1


def testGasCapAbstainsBeforeBroadcast(tmp_path):
    rpc = FakeRpc({"eth_getBlockByNumber": BLOCK, "eth_call": "0x", "eth_gasPrice": "0xb"})
    run = makeRun(tmp_path, rpc)
    with pytest.raises(QualificationError, match="spend cap"):
        run.command(
            "withdrawCredit",
            {"receiver": run.actors["receiver"], "amountAtoms": "1"},
            "requester",
            time.time() + 30,
        )
    assert not any(method == "eth_sendRawTransaction" for method, _ in rpc.calls)


def testEncryptedActorsAndSecretHygiene(tmp_path, monkeypatch):
    account = Account.from_key(keccak(text="isolated encrypted keystore test"))
    secret = "unit-test-password"
    path = tmp_path / "key.json"
    path.write_text(json.dumps(Account.encrypt(account.key, secret, kdf="pbkdf2", iterations=1000)))
    config = {
        "roles": {
            "owner": {
                "address": account.address.lower(),
                "keystore": "key.json",
                "passwordEnv": "TEST_L3_KEY_PASSWORD",
            }
        }
    }
    monkeypatch.setenv("TEST_L3_KEY_PASSWORD", secret)
    accounts, addresses = loadActors(config, tmp_path)
    assert accounts["owner"].address.lower() == addresses["owner"]
    monkeypatch.setenv("TEST_L3_KEY_PASSWORD", "wrong-" + secret)
    with pytest.raises(QualificationError) as failure:
        loadActors(config, tmp_path)
    assert secret not in str(failure.value)
    config["roles"]["owner"]["address"] = Account.from_key(
        (101).to_bytes(32, "big")
    ).address.lower()
    with pytest.raises(QualificationError, match="fixture"):
        loadActors(config, tmp_path)


def testEvidenceDigestAndProxyChanges(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"verified":true}')
    names = ("identityVerification", "rpcVerification", "validatorVerification", "scenarioEvidence")
    report = {name: {"digest": digest(path.read_bytes())} for name in names}
    config = {"evidenceFiles": {name: path.name for name in names}}
    assert loadEvidence(report, config, tmp_path)["identityVerification"] == {"verified": True}
    path.write_text('{"verified":false}')
    with pytest.raises(QualificationError, match="Digest mismatch"):
        loadEvidence(report, config, tmp_path)
    rpc = FakeRpc({"eth_getCode": "0x6000", "eth_getStorageAt": "0x" + "ff" * 32})
    run = makeRun(tmp_path, rpc)
    run.facts.update(
        identityRegistry="0x" + "56" * 20,
        identityVersion="qualified-source",
        marketCodeHash=digest(bytes.fromhex("6000")),
        identityCodeHash=digest(bytes.fromhex("6000")),
    )
    run.evidence["identityVerification"] = {
        "identityRegistry": run.facts["identityRegistry"],
        "identityVersion": "qualified-source",
        "validUntil": str(int(time.time()) + 100),
        "proxyKind": "eip1967",
        "codeObservations": [],
        "storageObservations": [
            {"address": run.facts["identityRegistry"], "slot": "0x0", "value": "0x" + "00" * 32}
        ],
        "probes": {
            name: True
            for name in ("transferClearsWallet", "restoredWalletControl", "contractOwner")
        },
    }
    with pytest.raises(QualificationError, match="proxy implementation/admin storage changed"):
        run.verifyDependencies(BLOCK)
    run.config["identities"] = {
        "solo": {
            "agentId": "0",
            "owner": "receiver",
            "payout": "requester",
            "signer": "requester",
            "permitSigner": "requester",
        }
    }
    observed = iter([run.actors["receiver"], run.actors["requester"]])
    rpc.answers["eth_call"] = lambda *_args: "0x" + encode(["address"], [next(observed)]).hex()
    rpc.answers["eth_getCode"] = "0x"
    with pytest.raises(QualificationError, match="code-free identity owner"):
        run.verifyIdentity("solo", BLOCK)
    # A contract owner may intentionally use another controlled signing account.
    observed = iter([run.actors["receiver"], run.actors["requester"]])
    rpc.answers["eth_getCode"] = "0x6000"
    assert run.verifyIdentity("solo", BLOCK) == run.config["identities"]["solo"]


def testPreflightRefusesWrongNetworkBeforeAnyWrite(tmp_path):
    rpc = FakeRpc({"eth_chainId": "0x1"})
    run = makeRun(tmp_path, rpc)
    run.facts["network"] = "monad-testnet"
    with pytest.raises(QualificationError, match="signing roles are missing"):
        run.preflight("solo")
    assert not rpc.calls
    for role in ("relayer", "validator", "receiver"):
        run.accounts[role] = Account.from_key(keccak(text="isolated role " + role))
        run.actors[role] = run.accounts[role].address.lower()
    content = {"ref": {"uri": "ipfs://unit-fixture", "digest": HASH}}
    run.config["artifacts"] = {
        name: content
        for name in ("input", "outputSchema", "validationPolicy", "result", "evidence")
    }
    run.facts["identityRegistry"] = "0x" + "56" * 20
    run.config["identities"] = {
        "solo": {
            "agentId": "0",
            "owner": "requester",
            "signer": "relayer",
            "payout": "receiver",
            "permitSigner": "requester",
            "profileDigest": HASH,
        }
    }
    del run.config["artifacts"]["result"]
    with pytest.raises(QualificationError, match="All five"):
        run.preflight("solo")
    assert not rpc.calls
    run.config["artifacts"]["result"] = content
    run.config["identities"]["solo"]["agentId"] = "00"
    with pytest.raises(ValueError):
        run.preflight("solo")
    assert not rpc.calls
    run.config["identities"]["solo"]["agentId"] = "0"
    run.config["identities"]["solo"]["signer"] = "validator"
    with pytest.raises(QualificationError, match="exclude the validator"):
        run.preflight("solo")
    assert not rpc.calls
    run.config["identities"]["solo"]["signer"] = "requester"
    with pytest.raises(QualificationError, match="must be distinct"):
        run.preflight("solo")
    assert not rpc.calls
    run.config["identities"]["solo"]["signer"] = "relayer"
    run.config["identities"]["malicious"] = dict(run.config["identities"]["solo"])
    run.config["identities"]["contractOwner"] = {**run.config["identities"]["solo"], "agentId": "1"}
    with pytest.raises(QualificationError, match="distinct from its relayer"):
        run.preflight("malicious")
    assert not rpc.calls
    with pytest.raises(QualificationError, match="chain ID mismatch"):
        run.preflight("solo")
    assert rpc.calls == [("eth_chainId", ())]
