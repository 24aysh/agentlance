"""Real funded Monad Anvil transactions, checked against independent L1 transitions."""

from copy import deepcopy

import pytest
from eth_abi import decode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from modules.domain.records import taskKey
from tests.layer3_support import Layer3Rig, abiType, artifact, callData, localNode, readJson

pytestmark = pytest.mark.l3socket


@pytest.fixture
def chain(tmp_path):
    with localNode(tmp_path) as rpc:
        yield Layer3Rig(rpc)


def createRoot(rig, **options):
    terms = rig.terms(**options)
    rig.command("createTask", {"terms": terms}, caller="requester")
    return rig.state.taskCount, terms


def stageTask(
    rig,
    taskId,
    terms,
    stage="SUBMITTED",
    *,
    agentId=0,
    signer="signer",
    owner="owner",
    payout="payout",
    amount=20,
    signed=True,
    ownWorkReserve=0,
):
    rig.register(agentId, owner, payout)
    rig.command(
        "submitBid",
        rig.bid(
            taskId, agentId, amount=amount, signed=signed, signer=signer, owner=owner, payout=payout
        ),
        caller="relayer" if signed else owner,
    )
    rig.advance(int(terms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(taskId)})
    if stage == "AWARDED":
        return
    execution = {"taskRef": rig.taskRef(taskId), "awardId": 1}
    rig.command(
        "acceptAward",
        {"executionRef": execution, "ownWorkReserveAtoms": str(ownWorkReserve)},
        caller=signer,
    )
    if stage == "RUNNING":
        return
    rig.command(
        "submitResult",
        {
            "executionRef": execution,
            "artifact": {"uri": "ipfs://fixture-result", "digest": "0x" + "03" * 32},
        },
        caller=signer,
    )


def runTerminal(rig, case):
    taskId, terms = createRoot(rig)
    ref = {"taskRef": rig.taskRef(taskId)}
    reason = case["expected"]["reason"]
    if reason == "CANCELLED":
        rig.command("cancelTask", ref, caller="requester")
    elif reason == "UNALLOCATED":
        rig.advance(int(terms["biddingClose"]))
        rig.command("allocateTask", ref)
    elif reason == "ALLOCATION_EXPIRED":
        rig.advance(int(terms["allocationBy"]))
        rig.command("allocateTask", ref, expected="DEADLINE_PASSED")
        rig.command("expireTask", ref)
    else:
        stage = {"NO_SHOW": "AWARDED", "EXECUTION_TIMEOUT": "RUNNING"}.get(reason, "SUBMITTED")
        stageTask(rig, taskId, terms, stage, ownWorkReserve=50 if reason == "SUCCESS" else 0)
        if reason in ("SUCCESS", "VALIDATION_FAILED"):
            rig.command(
                "settleVerdict",
                rig.verdict(taskId, verdict="PASS" if reason == "SUCCESS" else "FAIL"),
            )
        else:
            cutoff = {
                "NO_SHOW": "acceptBy",
                "EXECUTION_TIMEOUT": "resultBy",
                "VALIDATOR_TIMEOUT": "validationBy",
            }[reason]
            rig.advance(int(terms[cutoff]))
            execution = {"taskRef": rig.taskRef(taskId), "awardId": 1}
            late = {
                "NO_SHOW": (
                    "acceptAward",
                    {"executionRef": execution, "ownWorkReserveAtoms": "0"},
                    "signer",
                ),
                "EXECUTION_TIMEOUT": (
                    "submitResult",
                    {"executionRef": execution, "artifact": terms["input"]},
                    "signer",
                ),
                "VALIDATOR_TIMEOUT": ("settleVerdict", None, "relayer"),
            }
            name, data, caller = late[reason]
            rig.command(
                name,
                data if data is not None else rig.verdict(taskId),
                caller=caller,
                expected="DEADLINE_PASSED",
            )
            rig.command("expireTask", ref)
    receipt = rig.state.tasks[taskKey(rig.taskRef(taskId))].receipt
    assert {key: receipt[key] for key in ("reason", "paidAtoms", "refundAtoms")} == {
        key: case["expected"][key] for key in ("reason", "paidAtoms", "refundAtoms")
    }
    rig.command("expireTask", ref, expected="ALREADY_SETTLED")
    rig.withdrawAll()
    assert rig.state.withdrawnAtoms == 100
    return receipt


@pytest.mark.parametrize(
    "case", readJson("specs/fixtures/layer-3.json")["cases"], ids=lambda c: c["id"]
)
def testTerminalDifferential(chain, case):
    runTerminal(chain, case)


def runTree(rig, ending="SUCCESS"):
    parentId, terms = createRoot(rig, budget=200, denominator=200, delegation=True)
    rig.register()
    rig.command("submitBid", rig.bid(parentId), caller="owner")
    rig.advance(int(terms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(parentId)})
    execution = {"taskRef": rig.taskRef(parentId), "awardId": 1}
    rig.command(
        "acceptAward", {"executionRef": execution, "ownWorkReserveAtoms": "20"}, caller="signer"
    )
    childTerms = rig.terms(budget=60, denominator=80)
    base = int(terms["biddingClose"]) - 30
    childTerms.update(
        {
            name: str(base + offset)
            for name, offset in zip(
                ("biddingClose", "allocationBy", "acceptBy", "resultBy", "validationBy"),
                (210, 240, 360, 600, 720),
                strict=True,
            )
        }
    )
    childTerms.update(
        refundAddress=rig.actors["signer"], delegation={"maxDepth": 1, "maxChildren": 0}
    )
    rig.command(
        "createChildTask",
        {"parentRef": rig.taskRef(parentId), "terms": childTerms},
        caller="signer",
    )
    childId = rig.state.taskCount
    rig.command(
        "submitResult",
        {"executionRef": execution, "artifact": terms["input"]},
        caller="signer",
        expected="CHILDREN_ACTIVE",
    )
    stageTask(
        rig,
        childId,
        childTerms,
        agentId=1,
        owner="childOwner",
        signer="childSigner",
        payout="childPayout",
        amount=10,
    )
    if ending != "ACTIVE_CHILD":
        rig.command("settleVerdict", rig.verdict(childId))
        childReceipt = deepcopy(rig.state.tasks[taskKey(rig.taskRef(childId))].receipt)
        assert childReceipt["paidAtoms"] == "40"
    if ending == "SUCCESS":
        rig.command(
            "submitResult", {"executionRef": execution, "artifact": terms["input"]}, caller="signer"
        )
        rig.command("settleVerdict", rig.verdict(parentId, nonce=1))
    else:
        rig.advance(int(terms["resultBy"]))
        rig.command("expireTask", {"taskRef": rig.taskRef(parentId)})
        if ending == "ACTIVE_CHILD":
            rig.command("settleVerdict", rig.verdict(childId), expected="DEADLINE_PASSED")
            rig.command("expireTask", {"taskRef": rig.taskRef(childId)})
        else:
            assert rig.state.tasks[taskKey(rig.taskRef(childId))].receipt == childReceipt
    rig.withdrawAll()
    assert rig.state.withdrawnAtoms == 260


@pytest.mark.parametrize("ending", ["SUCCESS", "PARENT_TIMEOUT", "ACTIVE_CHILD"])
def testFundedTreeDifferential(chain, ending):
    runTree(chain, ending)


def testTransferPreservesAdmittedObligation(chain):
    taskId, terms = createRoot(chain)
    stageTask(chain, taskId, terms, "AWARDED")
    chain.transfer(0)
    execution = {"taskRef": chain.taskRef(taskId), "awardId": 1}
    data = {"executionRef": execution, "ownWorkReserveAtoms": "50"}
    chain.command("acceptAward", data, caller="other", expected="UNAUTHORIZED")
    chain.command("acceptAward", data, caller="signer")
    chain.command(
        "submitResult", {"executionRef": execution, "artifact": terms["input"]}, caller="signer"
    )
    chain.command("settleVerdict", chain.verdict(taskId))
    assert chain.read("readCredit", [chain.actors["other"]])[0] == 0
    chain.withdrawAll()


def runMalicious(rig):
    """Actual adversarial transactions, with prepared mock facts kept separate from outcomes."""
    taskId, terms = createRoot(rig)
    rig.register()
    data = rig.bid(taskId, signed=True)
    old = deepcopy(data)
    for field, value in (("chainId", 31338), ("verifyingContract", rig.actors["other"])):
        bad = deepcopy(old)
        typed = rig.typed("BidPermit", bad["permit"])
        typed["domain"][field] = value
        bad["signature"] = (
            "0x"
            + Account.sign_message(
                encode_typed_data(full_message=typed), rig.keys["owner"]
            ).signature.hex()
        )
        rig.command("submitBid", bad, expected="INVALID_SIGNATURE")
    data["signature"] = "0x" + "00" * 65
    rig.command("submitBid", data, expected="INVALID_SIGNATURE")
    rig.transfer(0)
    rig.command("submitBid", old, expected="OWNER_CHANGED")
    rig.transfer(0, "owner", rig.actors["payout"])
    rig.command("submitBid", old)
    rig.command("submitBid", old, expected="NONCE_USED")
    rig.advance(int(terms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(taskId)})
    execution = {"taskRef": rig.taskRef(taskId), "awardId": 1}
    rig.command(
        "acceptAward",
        {"executionRef": execution, "ownWorkReserveAtoms": "0"},
        caller="other",
        expected="UNAUTHORIZED",
    )
    rig.command(
        "acceptAward", {"executionRef": execution, "ownWorkReserveAtoms": "0"}, caller="signer"
    )
    rig.command(
        "submitResult", {"executionRef": execution, "artifact": terms["input"]}, caller="signer"
    )
    good = rig.verdict(taskId)
    bad = deepcopy(good)
    bad["record"]["resultDigest"] = "0x" + "ff" * 32
    rig.command("settleVerdict", bad, expected="RESULT_MISMATCH")
    bad = deepcopy(good)
    bad["record"]["validationPolicyDigest"] = "0x" + "ff" * 32
    rig.command("settleVerdict", bad, expected="POLICY_MISMATCH")
    bad = deepcopy(good)
    signature = bytearray.fromhex(bad["record"]["signature"][2:])
    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    signature[32:64] = (order - int.from_bytes(signature[32:64], "big")).to_bytes(32, "big")
    signature[64] = 55 - signature[64]
    bad["record"]["signature"] = "0x" + signature.hex()
    rig.command("settleVerdict", bad, expected="INVALID_SIGNATURE")
    bad = deepcopy(good)
    bad["record"]["signature"] = "0x" + "00" * 65
    bad["record"]["expiry"] = str(rig.now())
    rig.command("settleVerdict", bad, expected="INVALID_SIGNATURE")
    rig.command("settleVerdict", good)
    rig.command("settleVerdict", good, expected="ALREADY_SETTLED")
    runContractOwner(rig)
    runHostileWithdrawals(rig, good)
    rig.withdrawAll()
    assert rig.state.withdrawnAtoms == 400


def hostileEntry(contract, name):
    return next(
        entry for entry in artifact(contract, "HostileActors")["abi"] if entry.get("name") == name
    )


def runContractOwner(rig):
    """The fixture owner's configured decision supplies L1's external signature observation."""
    owner = rig.deploy("SignatureOwner", sourceName="HostileActors")
    taskId, terms = createRoot(rig)
    rig.register(1, owner, "payout")
    bid = rig.bid(taskId, agentId=1, signed=True)
    bid["permit"]["owner"] = owner
    bid["signature"] = "0x01"
    message = encode_typed_data(full_message=rig.typed("BidPermit", bid["permit"]))
    digest = keccak(b"\x19" + message.version + message.header + message.body)
    rig.external(owner, "configure", ["uint256", "bytes32"], [1, digest])
    rig.command("submitBid", bid, signatureValid=False, expected="INVALID_SIGNATURE")
    rig.external(owner, "configure", ["uint256", "bytes32"], [0, digest])
    rig.command("submitBid", bid, signatureValid=True)
    rig.register(2, "childOwner", "childPayout")
    rig.command(
        "submitBid",
        rig.bid(
            taskId,
            agentId=2,
            amount=30,
            owner="childOwner",
            signer="childSigner",
            payout="childPayout",
        ),
        caller="childOwner",
    )
    rig.advance(int(terms["biddingClose"]))
    rig.command("allocateTask", {"taskRef": rig.taskRef(taskId)})
    rig.advance(int(terms["acceptBy"]))
    rig.command(
        "acceptAward",
        {
            "executionRef": {"taskRef": rig.taskRef(taskId), "awardId": 1},
            "ownWorkReserveAtoms": "0",
        },
        caller="signer",
        expected="DEADLINE_PASSED",
    )
    rig.command("expireTask", {"taskRef": rig.taskRef(taskId)})


def runHostileWithdrawals(rig, verdict):
    """Both wrappers forward real calls; reverts, balances and L1 state are compared unchanged."""
    reverting = rig.deploy("RevertingReceiver", sourceName="HostileActors")
    terms = rig.terms()
    terms["refundAddress"] = reverting
    rig.command("createTask", {"terms": terms}, caller="requester")
    rig.command("cancelTask", {"taskRef": rig.taskRef(rig.state.taskCount)}, caller="requester")
    entry = hostileEntry("RevertingReceiver", "withdraw")
    rig.command(
        "withdrawCredit",
        {"receiver": reverting, "amountAtoms": "100"},
        caller=reverting,
        forwardedData=callData(entry, [rig.market, reverting, 100]),
        transferSucceeded=False,
        expected="TRANSFER_FAILED",
    )
    receiver = rig.actors["receiver"]
    rig.command(
        "withdrawCredit",
        {"receiver": receiver, "amountAtoms": "100"},
        caller=reverting,
        forwardedData=callData(entry, [rig.market, receiver, 100]),
    )
    ref = rig.taskRef(1)
    execution = {"taskRef": ref, "awardId": 1}
    callbacks = {
        "createTask": {"terms": terms},
        "submitBid": rig.bid(1),
        "allocateTask": {"taskRef": ref},
        "acceptAward": {"executionRef": execution, "ownWorkReserveAtoms": "0"},
        "createChildTask": {"parentRef": ref, "terms": terms},
        "submitResult": {"executionRef": execution, "artifact": terms["input"]},
        "settleVerdict": verdict,
        "expireTask": {"taskRef": ref},
        "cancelTask": {"taskRef": ref},
        "withdrawCredit": {"receiver": receiver, "amountAtoms": "1"},
    }
    assert set(callbacks) == set(rig.codec.commands)
    payloads = [
        bytes.fromhex(rig.codec.commandData(name, data)[2:]) for name, data in callbacks.items()
    ]
    reentrant = rig.deploy(
        "ReentrantReceiver",
        ["address", "bytes[]"],
        [rig.market, payloads],
        sourceName="HostileActors",
    )
    terms["refundAddress"] = reentrant
    rig.command("createTask", {"terms": terms}, caller="requester")
    rig.command("cancelTask", {"taskRef": rig.taskRef(rig.state.taskCount)}, caller="requester")
    rig.command(
        "withdrawCredit",
        {"receiver": reentrant, "amountAtoms": "100"},
        caller=reentrant,
        forwardedData=callData(hostileEntry("ReentrantReceiver", "withdraw"), [reentrant, 100]),
    )
    entry = hostileEntry("ReentrantReceiver", "rejected")
    raw = rig.rpc.call("eth_call", {"to": reentrant, "data": callData(entry, [])}, "latest")
    assert decode([abiType(field) for field in entry["outputs"]], bytes.fromhex(raw[2:])) == (10,)
    rig.transactions[-1]["rejectedCallbacks"] = list(callbacks)


def testMaliciousAndSignaturePrecedence(chain):
    runMalicious(chain)


def testNonpayableEncodingException(chain):
    taskId, _ = createRoot(chain)
    chain.command(
        "cancelTask",
        {"taskRef": chain.taskRef(taskId)},
        caller="requester",
        value=1,
        expected="WRONG_VALUE",
    )


def testRetryIndependentFunding(chain):
    taskId, _ = createRoot(chain)
    chain.command("cancelTask", {"taskRef": chain.taskRef(taskId)}, caller="requester")
    nextId, _ = createRoot(chain, retry=chain.taskRef(taskId))
    assert nextId == taskId + 1
    assert chain.state.depositedAtoms == 200
    assert chain.state.tasks[taskKey(chain.taskRef(nextId))].spec["rootRef"] == chain.taskRef(
        nextId
    )
