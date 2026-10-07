"""Original golden values and independently advanced, seeded command sequences."""

import json
from copy import deepcopy
from itertools import permutations
from random import Random

import pytest
from eth_abi import decode

from modules.domain.records import taskKey
from tests.integration.test_layer3_transactions import createRoot
from tests.layer3_support import (
    ROOT,
    Layer3Rig,
    RpcError,
    abiType,
    artifact,
    callData,
    localNode,
    readJson,
)

pytestmark = pytest.mark.l3socket


@pytest.fixture(scope="module")
def pure(tmp_path_factory):
    with localNode(tmp_path_factory.mktemp("l3-goldens")) as rpc:
        rig = Layer3Rig(rpc)
        address = rig.deploy("ConformanceHarness")
        yield rig, address, artifact("ConformanceHarness")["abi"]


def pureCall(pure, name, values):
    rig, address, abi = pure
    entry = next(e for e in abi if e.get("name") == name)
    raw = rig.rpc.call("eth_call", {"to": address, "data": callData(entry, values)}, "latest")
    return decode([abiType(f) for f in entry["outputs"]], bytes.fromhex(raw[2:]))


@pytest.mark.parametrize(
    "case", readJson("specs/fixtures/market.json")["cases"], ids=lambda c: c["id"]
)
def testMarketGoldens(pure, case):
    rig = pure[0]
    source = case["input"]
    bids = []
    for bid in source["bids"]:
        value = {
            "schemaVersion": 1,
            "offer": {
                "taskRef": rig.taskRef(1),
                "agentRef": bid["agentRef"],
                "bidAtoms": bid["bidAtoms"],
                "executionSigner": rig.actors["signer"],
                "payout": rig.actors["payout"],
                "profileDigest": "0x" + "00" * 32,
            },
            "ownerAtBid": rig.actors["owner"],
            "snapshotBlock": "1",
            "counters": {"successes": "0", "failures": "0"},
            "p": bid["p"],
            "score": "0",
        }
        bids.append(rig.codec.convert({"$ref": "#/$defs/Bid"}, value))
    for ordering in permutations(bids):
        args = [int(source[k]) for k in ("budgetAtoms", "alphaNum", "alphaDen")] + [ordering]
        if case["expected"]["outcome"] == "REJECTED":
            with pytest.raises(RpcError) as failure:
                pureCall(pure, "evaluateAuction", args)
            assert (
                int(failure.value.error["data"][-64:], 16)
                == rig.codec.catalog["errors"][case["expected"]["reason"]]
            )
        else:
            result = pureCall(pure, "evaluateAuction", args)[0]
            decoded = rig.codec.convert({"$ref": "#/$defs/Allocation"}, result, decoding=True)
            expected = case["expected"]
            assert {key: decoded[key] for key in expected} == expected


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in readJson("specs/fixtures/reputation.json")["cases"]
        if c["action"]["operation"] == "probability"
    ],
    ids=lambda c: c["id"],
)
def testProbabilityGoldens(pure, case):
    assert pureCall(
        pure, "probability", [int(case["initial"][k]) for k in ("successes", "failures")]
    ) == (case["expected"]["p"],)


def vectorRecord(vector):
    typed = vector["typedData"]
    m, d = typed["message"], typed["domain"]
    ref = {"chainId": d["chainId"], "market": d["verifyingContract"], "taskId": m["taskId"]}
    agent = {
        "chainId": d["chainId"],
        "identityRegistry": m["identityRegistry"],
        "agentId": m["agentId"],
    }
    if typed["primaryType"] == "BidPermit":
        return {
            "taskRef": ref,
            "agentRef": agent,
            **{
                k: m[k]
                for k in (
                    "owner",
                    "executionSigner",
                    "payout",
                    "bidAtoms",
                    "profileDigest",
                    "nonce",
                    "expiry",
                )
            },
        }
    return {
        "schemaVersion": 1,
        "executionRef": {"taskRef": ref, "awardId": m["awardId"]},
        "agentRef": agent,
        "resultDigest": m["resultDigest"],
        "validationPolicyDigest": m["validationPolicyDigest"],
        "verdict": "PASS" if m["verdict"] else "FAIL",
        "evidence": {"uri": "ipfs://fixture", "digest": m["evidenceDigest"]},
        "validator": m["validator"],
        "nonce": m["nonce"],
        "expiry": m["expiry"],
        "signature": vector["signature"],
    }


@pytest.mark.parametrize(
    "vector", readJson("specs/fixtures/signatures.json")["positive"], ids=lambda c: c["id"]
)
def testOriginalSigningVectors(pure, vector):
    rig = pure[0]
    kind = vector["id"]
    record = vectorRecord(vector)
    domain = vector["typedData"]["domain"]
    bid = kind == "BidPermit"
    wire = rig.codec.convert(
        {"$ref": "#/$defs/" + ("BidPermit" if bid else "ValidationRecord")}, record
    )
    args = [int(domain["chainId"]), domain["verifyingContract"], wire]
    if bid:
        args.append(bytes.fromhex(vector["signature"][2:]))
    result = pureCall(pure, "bidHashes" if bid else "verdictHashes", args)
    assert ["0x" + v.hex() for v in result[:4]] == [
        vector[k] for k in ("typeHash", "domainSeparator", "structHash", "digest")
    ]
    assert result[4] is True


def runGeneratedSolo(rig, random, mode):
    taskId, terms = createRoot(rig, budget=random.randint(100, 300), denominator=100)
    ref = {"taskRef": rig.taskRef(taskId)}
    execution = {**ref, "awardId": 1}
    if mode == "retry":
        rig.command(
            "createTask", {"terms": terms}, caller="requester", value=0, expected="WRONG_VALUE"
        )
        rig.command("cancelTask", ref, caller="other", expected="UNAUTHORIZED")
        rig.command("cancelTask", ref, caller="requester")
        rig.command("cancelTask", ref, caller="requester", expected="ALREADY_SETTLED")
        retryId, retryTerms = createRoot(rig, retry=rig.taskRef(taskId))
        rig.advance(int(retryTerms["biddingClose"]))
        rig.command("allocateTask", {"taskRef": rig.taskRef(retryId)})
    else:
        rig.register(taskId)
        bid = rig.bid(taskId, taskId, amount=random.randint(0, 40), signed=True, nonce=taskId)
        if mode == "wallet":
            rig.transfer(taskId, "owner")
            rig.command("submitBid", bid, expected="WALLET_UNSET")
            rig.transfer(taskId, "owner", rig.actors["payout"])
        rig.command("submitBid", bid)
        if mode == "verdict":
            rig.command("submitBid", bid, expected="NONCE_USED")
        if random.randrange(2):
            rig.transfer(taskId)
        rig.advance(int(terms["biddingClose"]))
        rig.command("allocateTask", ref)
        if mode == "no-show":
            rig.command(
                "acceptAward",
                {"executionRef": execution, "ownWorkReserveAtoms": "0"},
                caller="other",
                expected="UNAUTHORIZED",
            )
            rig.command("expireTask", ref, expected="TOO_EARLY")
            rig.command("allocateTask", ref, expected="WRONG_STATE")
            rig.advance(int(terms["acceptBy"]))
            rig.command("expireTask", ref)
        else:
            rig.command(
                "acceptAward",
                {"executionRef": execution, "ownWorkReserveAtoms": "0"},
                caller="signer",
            )
            if mode == "execution-timeout":
                rig.command(
                    "submitResult",
                    {"executionRef": execution, "artifact": terms["input"]},
                    caller="other",
                    expected="UNAUTHORIZED",
                )
                rig.command("expireTask", ref, expected="TOO_EARLY")
                rig.advance(int(terms["resultBy"]))
                rig.command("expireTask", ref)
            else:
                rig.command(
                    "submitResult",
                    {"executionRef": execution, "artifact": terms["input"]},
                    caller="signer",
                )
                if mode == "validator-timeout":
                    verdict = rig.verdict(taskId, nonce=taskId)
                    verdict["record"]["signature"] = "0x" + "00" * 65
                    rig.command("settleVerdict", verdict, expected="INVALID_SIGNATURE")
                    rig.advance(int(terms["validationBy"]))
                    rig.command("expireTask", ref)
                else:
                    rig.command(
                        "settleVerdict",
                        rig.verdict(taskId, verdict=random.choice(["PASS", "FAIL"]), nonce=taskId),
                    )
    rig.command(
        "withdrawCredit",
        {"receiver": rig.actors["receiver"], "amountAtoms": "1"},
        caller="requester",
    )


def runGeneratedTree(rig, random):
    parentId, terms = createRoot(rig, budget=200, denominator=200, delegation=True)
    rig.register(parentId)
    rig.command("submitBid", rig.bid(parentId, parentId), caller="owner")
    rig.advance(int(terms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(parentId)})
    execution = {"taskRef": rig.taskRef(parentId), "awardId": 1}
    rig.command(
        "acceptAward", {"executionRef": execution, "ownWorkReserveAtoms": "20"}, caller="signer"
    )
    childTerms = rig.terms(budget=60, denominator=80)
    childTerms.update(
        refundAddress=rig.actors["signer"], delegation={"maxDepth": 1, "maxChildren": 0}
    )
    rig.command(
        "createChildTask",
        {"parentRef": rig.taskRef(parentId), "terms": childTerms},
        caller="signer",
    )
    childId = rig.state.taskCount
    rig.register(childId, "childOwner", "childPayout")
    rig.command(
        "submitBid",
        rig.bid(
            childId,
            childId,
            amount=random.randint(0, 30),
            owner="childOwner",
            signer="childSigner",
            payout="childPayout",
        ),
        caller="childOwner",
    )
    rig.command(
        "submitResult",
        {"executionRef": execution, "artifact": terms["input"]},
        caller="signer",
        expected="CHILDREN_ACTIVE",
    )
    rig.advance(int(childTerms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(childId)})
    childExecution = {"taskRef": rig.taskRef(childId), "awardId": 1}
    rig.command(
        "acceptAward",
        {"executionRef": childExecution, "ownWorkReserveAtoms": "0"},
        caller="childSigner",
    )
    rig.command(
        "submitResult",
        {"executionRef": childExecution, "artifact": childTerms["input"]},
        caller="childSigner",
    )
    rig.command("settleVerdict", rig.verdict(childId, nonce=childId))
    rig.command(
        "submitResult", {"executionRef": execution, "artifact": terms["input"]}, caller="signer"
    )
    rig.command(
        "settleVerdict",
        rig.verdict(parentId, verdict=random.choice(["PASS", "FAIL"]), nonce=parentId),
    )
    rig.command("expireTask", {"taskRef": rig.taskRef(parentId)}, expected="ALREADY_SETTLED")
    for owner in ("requester", "childPayout"):
        rig.command(
            "withdrawCredit", {"receiver": rig.actors["receiver"], "amountAtoms": "1"}, caller=owner
        )


@pytest.mark.parametrize("seed", range(32))
def testSeededDifferential(tmp_path, seed):
    random = Random(seed)
    with localNode(tmp_path) as rpc:
        rig = Layer3Rig(rpc)
        runGeneratedTree(rig, random)
        modes = ["wallet", "no-show", "execution-timeout", "validator-timeout", "retry", "verdict"]
        random.shuffle(modes)
        for mode in modes:
            start = len(rig.transactions)
            runGeneratedSolo(rig, random, mode)
            assert len(rig.transactions) - start == 8, mode
        assert len(rig.transactions) == 64
        assert sum(t["error"] is None for t in rig.transactions) >= 45
        assert {"createChildTask", "withdrawCredit"} <= {t["command"] for t in rig.transactions}
        rig.withdrawAll()
        reportDir = ROOT / ".scratch/layer3/differential"
        reportDir.mkdir(parents=True, exist_ok=True)
        (reportDir / f"seed-{seed}.json").write_text(
            json.dumps({"seed": seed, "actions": 64, **rig.report()}, indent=2) + "\n"
        )


def testEventReplayAndOrphanRollback(tmp_path):
    with localNode(tmp_path) as rpc:
        rig = Layer3Rig(rpc)
        snapshot = rpc.call("evm_snapshot")
        taskId, _ = createRoot(rig)
        rig.command("cancelTask", {"taskRef": rig.taskRef(taskId)}, caller="requester")
        orphanHash = rig.transactions[-1]["hash"]
        orphanBlock = rpc.call("eth_getTransactionReceipt", orphanHash)["blockHash"]
        assert rpc.call("evm_revert", snapshot)
        rig.state = type(rig.state)()
        rig.transactions.clear()
        rig.events.clear()
        rig.compare()
        assert rpc.call("eth_getTransactionReceipt", orphanHash) is None
        taskId, _ = createRoot(rig, budget=101)
        rig.command("cancelTask", {"taskRef": rig.taskRef(taskId)}, caller="requester")
        logs = rpc.call(
            "eth_getLogs", {"address": rig.market, "fromBlock": "0x0", "toBlock": "latest"}
        )
        assert all(log["blockHash"] != orphanBlock for log in logs)
        seen, credits, tasks = set(), {}, {}
        for log in logs + logs:
            key = (31337, rig.market, log["blockHash"], log["logIndex"])
            if key in seen:
                continue
            seen.add(key)
            event = rig.codec.event(log)
            if event["name"] == "TaskCreated":
                spec = event["payload"]["task"]
                tasks[taskKey(spec["taskRef"])] = spec
            elif event["name"] == "TaskSettled":
                receipt = event["payload"]["receipt"]
                credits[receipt["refundAddress"]] = credits.get(receipt["refundAddress"], 0) + int(
                    receipt["refundAtoms"]
                )
        assert credits == rig.state.credits
        assert tasks == {key: task.spec for key, task in rig.state.tasks.items()}


@pytest.mark.parametrize(
    "case",
    [
        c
        for c in readJson("specs/fixtures/reputation.json")["cases"]
        if c["action"]["operation"] != "probability"
    ],
    ids=lambda c: c["id"],
)
def testReputationStateGoldens(tmp_path, case):
    from modules.domain.records import agentKey
    from modules.market_core.state import Checkpoint
    from tests.integration.test_layer3_transactions import stageTask

    with localNode(tmp_path) as rpc:
        rig = Layer3Rig(rpc, marketName="MarketHarness")
        initial, action = case["initial"], case["action"]
        operation = action["operation"]

        def seedPoint(family, height, successes, failures):
            rig.external(
                rig.market,
                "seedCheckpoint",
                ["uint256", "string", "uint64", "uint64", "uint64"],
                [0, family, int(height), int(successes), int(failures)],
            )

        family = "structured-output-v1"
        if operation == "createTask":
            rig.external(rig.market, "seedTaskCount", ["uint64"], [int(initial["taskCount"])])
            rig.state.taskCount = int(initial["taskCount"])
            rig.command(
                "createTask", {"terms": rig.terms()}, caller="requester", expected=case["error"]
            )
            result = {"taskCount": str(rig.read("readAccounting")[0])}
        elif operation == "snapshot":
            if "families" in initial:
                for row in initial["families"]:
                    seedPoint(row["family"], 9, row["successes"], row["failures"])
                snapshot = 9
            else:
                for row in initial["checkpoints"]:
                    seedPoint(family, row["block"], row["successes"], row["failures"])
                snapshot = int(action["creationBlock"]) - 1
            s, f = rig.read(
                "readCounters", [rig.wireRef(rig.agentRef(0), "AgentRef"), family, snapshot]
            )
            probability = s + 1
            result = {
                "successes": str(s),
                "failures": str(f),
                "p": 1_000_000 * probability // (s + f + 2),
            }
            if "creationBlock" in action:
                result["snapshotBlock"] = str(snapshot)
        elif operation == "transfer":
            rig.register()
            seedPoint(family, 1, initial["successes"], initial["failures"])
            rig.transfer(0)
            s, f = rig.read(
                "readCounters", [rig.wireRef(rig.agentRef(0), "AgentRef"), family, 2**64 - 1]
            )
            assert rig.identities[0][0] == rig.actors["other"]
            result = {
                "successes": str(s),
                "failures": str(f),
                "owner": "NEW",
                "p": 1_000_000 * (s + 1) // (s + f + 2),
            }
        else:
            taskId, terms = createRoot(rig)
            stageTask(rig, taskId, terms)
            if initial["receiptSeen"]:
                rig.command("settleVerdict", rig.verdict(taskId))
            height = int(rpc.call("eth_blockNumber"), 16) + 1
            seedPoint(family, height, initial["successes"], initial["failures"])
            rig.external(rig.market, "seedTaskCount", ["uint64"], [5])
            rig.state.taskCount = 5
            rig.state.reputation.checkpoints[(agentKey(rig.agentRef(0)), family)] = (
                Checkpoint(height, int(initial["successes"]), int(initial["failures"])),
            )
            if action["reason"] == "VALIDATOR_TIMEOUT":
                rig.advance(int(terms["validationBy"]))
                rig.command("expireTask", {"taskRef": rig.taskRef(taskId)})
            else:
                rig.command(
                    "settleVerdict",
                    rig.verdict(
                        taskId, verdict="PASS" if action["reason"] == "SUCCESS" else "FAIL"
                    ),
                    expected="ALREADY_SETTLED" if initial["receiptSeen"] else None,
                )
            s, f = rig.read(
                "readCounters", [rig.wireRef(rig.agentRef(0), "AgentRef"), family, 2**64 - 1]
            )
            result = {
                "successes": str(s),
                "failures": str(f),
                "observationAdded": (s, f) != (int(initial["successes"]), int(initial["failures"])),
            }
        assert result == case["expected"]


@pytest.mark.parametrize(
    "case",
    [c for c in readJson("specs/fixtures/signatures.json")["negative"] if c["mutation"]],
    ids=lambda c: c["id"],
)
def testOriginalSigningMutations(pure, case):
    from eth_account.messages import encode_typed_data

    vectors = readJson("specs/fixtures/signatures.json")["positive"]
    vector = deepcopy(next(v for v in vectors if v["id"] == case["base"]))
    mutation = case["mutation"]
    typed = vector["typedData"]
    if mutation["section"] == "primaryType":
        typed["types"][mutation["value"]] = typed["types"].pop(typed["primaryType"])
        typed["primaryType"] = mutation["value"]
        encoded = encode_typed_data(full_message=typed)
        valid = pureCall(
            pure,
            "verifyDigest",
            [
                encoded.header,
                encoded.body,
                bytes.fromhex(vector["signature"][2:]),
                vector["recoveredSigner"],
            ],
        )[0]
    else:
        typed[mutation["section"]][mutation["field"]] = mutation["value"]
        record = vectorRecord(vector)
        bid = case["base"] == "BidPermit"
        wire = pure[0].codec.convert(
            {"$ref": "#/$defs/" + ("BidPermit" if bid else "ValidationRecord")}, record
        )
        args = [int(typed["domain"]["chainId"]), typed["domain"]["verifyingContract"], wire]
        if bid:
            args.append(bytes.fromhex(vector["signature"][2:]))
        valid = pureCall(pure, "bidHashes" if bid else "verdictHashes", args)[4]
    assert valid is False


@pytest.mark.parametrize(
    "entry",
    [
        v
        for v in readJson("specs/fixtures/objects.json")["objects"]
        if v["type"]
        in {
            "AgentRef",
            "TaskRef",
            "TaskSpec",
            "Bid",
            "Allocation",
            "ExecutionRef",
            "ResultCommitment",
            "ValidationRecord",
            "SettlementReceipt",
        }
    ],
    ids=lambda v: v["id"],
)
def testCanonicalWireRoundtrip(pure, entry):
    schema = {"$ref": "#/$defs/" + entry["type"]}
    wire = pure[0].codec.convert(schema, entry["value"])
    returned = pureCall(pure, "echo" + entry["type"], [wire])[0]
    assert pure[0].codec.convert(schema, returned, decoding=True) == entry["value"]


@pytest.mark.parametrize(
    "vector", readJson("specs/fixtures/signatures.json")["contentHashes"], ids=lambda v: v["id"]
)
def testOriginalContentHashes(pure, vector):
    hashed = pureCall(pure, "hashContent", [bytes.fromhex(vector["bytesHex"][2:])])[0]
    assert "0x" + hashed.hex() == vector["keccak256"]
