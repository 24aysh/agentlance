from copy import deepcopy
from dataclasses import replace

import pytest

from conftest import ACTORS, POLICY, Harness, address, cases, contextFor, digest, example
from modules.domain.records import taskKey
from modules.market_core.state import Applied, CoreState, Rejected
from modules.market_core.transitions import applyCommand, nonceKey


def invoke(state, name, data, context):
    before = deepcopy(state)
    result = applyCommand(
        state, {"schemaVersion": 1, "command": name, "input": data}, context, POLICY
    )
    assert state == before
    return result


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("foreign-chain", "IDENTITY_UNAVAILABLE"),
        ("foreign-registry", "IDENTITY_UNAVAILABLE"),
        ("unavailable", "IDENTITY_UNAVAILABLE"),
        ("owner-transfer", "OWNER_CHANGED"),
        ("wallet-unset", "WALLET_UNSET"),
        ("wallet-mismatch", "WALLET_MISMATCH"),
        ("signature-false", "INVALID_SIGNATURE"),
        ("signature-missing", "INVALID_SIGNATURE"),
        ("permit-missing", "INVALID_SIGNATURE"),
        ("permit-task", "UNKNOWN_TASK"),
        ("permit-agent", "INVALID_SIGNATURE"),
        ("permit-payout", "INVALID_SIGNATURE"),
        ("nonce-used", "NONCE_USED"),
        ("expiry", "SIGNATURE_EXPIRED"),
        ("owner-validator", "VALIDATOR_CONFLICT"),
        ("signer-validator", "VALIDATOR_CONFLICT"),
        ("payout-validator", "VALIDATOR_CONFLICT"),
        ("over-budget", "BID_OVER_BUDGET"),
        ("duplicate", "DUPLICATE_AGENT"),
        ("approved-operator", "UNAUTHORIZED"),
    ],
)
def testBidRejections(mutation, error):
    h = Harness().stage("BID" if mutation == "duplicate" else "OPEN")
    data = example("submitBid")["input"]
    observationChanges = {}
    if mutation.startswith("foreign-"):
        field = "chainId" if mutation == "foreign-chain" else "identityRegistry"
        data["offer"]["agentRef"][field] = "1" if field == "chainId" else address(1)
    if mutation in ("signature-missing", "permit-missing"):
        data[mutation.split("-")[0]] = None
    if mutation.startswith("permit-") and mutation != "permit-missing":
        field = {"permit-task": "taskRef", "permit-agent": "agentRef", "permit-payout": "payout"}[
            mutation
        ]
        if field == "payout":
            data["permit"][field] = address(1)
        else:
            data["permit"][field]["taskId" if field == "taskRef" else "agentId"] = "99"
    if mutation == "over-budget":
        data["offer"]["bidAtoms"] = data["permit"]["bidAtoms"] = "101"
    if mutation == "expiry":
        data["permit"]["expiry"] = "1100"
    if mutation in ("owner-validator", "signer-validator", "payout-validator"):
        field = {
            "owner-validator": "owner",
            "signer-validator": "executionSigner",
            "payout-validator": "payout",
        }[mutation]
        data["permit"][field] = POLICY.validator
        if field != "owner":
            data["offer"][field] = POLICY.validator
        else:
            observationChanges["owner"] = POLICY.validator
    if mutation == "approved-operator":
        data.update(permit=None, signature=None)
    if mutation == "owner-transfer":
        observationChanges["owner"] = address(900)
    if mutation == "unavailable":
        observationChanges["available"] = False
    if mutation.startswith("wallet-"):
        observationChanges["verifiedWallet"] = None if mutation == "wallet-unset" else address(900)
    context = contextFor("submitBid", data, 1100, ACTORS["OTHER"])
    context = replace(
        context, identityObservation=replace(context.identityObservation, **observationChanges)
    )
    if mutation == "signature-false":
        context = replace(
            context, signatureObservation=replace(context.signatureObservation, valid=False)
        )
    if mutation == "nonce-used":
        h.state.usedNonces.add(
            nonceKey(POLICY.market, data["permit"]["owner"], "BidPermit", data["permit"]["nonce"])
        )
    assert invoke(h.state, "submitBid", data, context) == Rejected(error)


@pytest.mark.parametrize("kind", ["BidPermit", "ValidationVerdict"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("chainId", 1),
        ("market", address(999)),
        ("primaryType", "Other"),
        ("signer", address(999)),
        ("record", {}),
        ("signature", "0x00"),
        ("valid", False),
    ],
)
def testVerifierBinding(kind, field, value):
    name = "submitBid" if kind == "BidPermit" else "settleVerdict"
    h = Harness().stage("OPEN" if kind == "BidPermit" else "SUBMITTED")
    data = example(name)["input"]
    context = contextFor(name, data, 1100 if kind == "BidPermit" else 2600)
    context = replace(
        context, signatureObservation=replace(context.signatureObservation, **{field: value})
    )
    assert invoke(h.state, name, data, context) == Rejected("INVALID_SIGNATURE")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("agentRef", {**example("AgentRef"), "agentId": "99"}, "RESULT_MISMATCH"),
        ("resultDigest", digest(900), "RESULT_MISMATCH"),
        ("validationPolicyDigest", digest(900), "POLICY_MISMATCH"),
        ("validator", address(900), "INVALID_SIGNATURE"),
        ("expiry", "2600", "SIGNATURE_EXPIRED"),
    ],
)
def testVerdictBindings(field, value, code):
    h = Harness().stage("SUBMITTED")
    data = example("settleVerdict")["input"]
    data["record"][field] = value
    assert invoke(
        h.state, "settleVerdict", data, contextFor("settleVerdict", data, 2600)
    ) == Rejected(code)


def testNonceAndTransferSemantics():
    h = Harness().stage("OPEN")
    data = example("submitBid")["input"]
    data["permit"]["nonce"] = "0"
    h.state.usedNonces.add(nonceKey(POLICY.market, ACTORS["OWNER"], "BidPermit", "100"))
    accepted = invoke(h.state, "submitBid", data, contextFor("submitBid", data, 1100))
    assert isinstance(accepted, Applied)
    assert nonceKey(POLICY.market, ACTORS["OWNER"], "BidPermit", "0") in accepted.state.usedNonces
    assert (
        nonceKey(POLICY.market, ACTORS["OWNER"], "ValidationVerdict", "0")
        not in accepted.state.usedNonces
    )
    # Transfer back has no ownership epoch; this still-unused permit is valid again.
    transferred = contextFor("submitBid", data, 1100)
    transferred = replace(
        transferred,
        identityObservation=replace(transferred.identityObservation, owner=address(999)),
    )
    assert invoke(h.state, "submitBid", data, transferred) == Rejected("OWNER_CHANGED")
    assert isinstance(
        invoke(h.state, "submitBid", data, contextFor("submitBid", data, 1100)), Applied
    )
    # Old admission is frozen, and no registry observation is needed to accept or settle.
    h.state = accepted.state
    h.call("allocateTask", 1200)
    h.call(
        "acceptAward",
        1400,
        identityObservation=replace(transferred.identityObservation, verifiedWallet=None),
    )
    h.call("submitResult", 2400)
    h.call("settleVerdict", 2600)
    assert h.task().receipt["payout"] == ACTORS["PAYOUT"]
    verdict = example("settleVerdict")["input"]
    assert invoke(
        h.state,
        "settleVerdict",
        verdict,
        contextFor("settleVerdict", verdict, 5000, ACTORS["OTHER"]),
    ) == Rejected("ALREADY_SETTLED")


@pytest.mark.parametrize(
    "missing", ["identity", "owner", "identity-binding", "signature", "transfer"]
)
def testMissingAdapterFacts(missing):
    name = "withdrawCredit" if missing == "transfer" else "submitBid"
    h = Harness().stage("SETTLED" if missing == "transfer" else "OPEN")
    data = example(name)["input"]
    context = contextFor(name, data, 1100)
    if missing == "identity":
        context = replace(context, identityObservation=None)
    elif missing == "owner":
        context = replace(
            context, identityObservation=replace(context.identityObservation, owner=None)
        )
    elif missing == "identity-binding":
        context = replace(
            context, identityObservation=replace(context.identityObservation, agentRef={})
        )
    elif missing == "signature":
        context = replace(context, signatureObservation=None)
    else:
        context = replace(context, transferSucceeded=None)
    with pytest.raises(ValueError):
        invoke(h.state, name, data, context)


@pytest.mark.parametrize("stateName", ["OPEN", "BID", "AWARDED", "RUNNING", "SUBMITTED", "SETTLED"])
def testInvalidStateMatrix(stateName):
    h = Harness().stage(stateName)
    allowed = {
        "submitBid": "OPEN",
        "allocateTask": "OPEN",
        "acceptAward": "AWARDED",
        "submitResult": "RUNNING",
        "settleVerdict": "SUBMITTED",
        "createChildTask": "RUNNING",
        "cancelTask": "OPEN",
    }
    status = "OPEN" if stateName == "BID" else stateName
    for name, expectedState in allowed.items():
        if status == expectedState:
            continue
        data = example(name)["input"]
        code = "ALREADY_SETTLED" if status == "SETTLED" else "WRONG_STATE"
        assert invoke(h.state, name, data, contextFor(name, data, 1100)) == Rejected(code)


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("policyVersion", 2, "UNSUPPORTED_POLICY"),
        ("taskFamily", "other", "UNSUPPORTED_POLICY"),
        ("validator", address(999), "UNSUPPORTED_POLICY"),
        ("asset", {}, "INVALID_ASSET"),
        ("budgetAtoms", "0", "INVALID_BUDGET"),
        ("alphaNum", "0", "INVALID_ALPHA"),
        ("alphaDen", "0", "INVALID_ALPHA"),
        ("biddingClose", "1000", "INVALID_DEADLINES"),
        ("acceptBy", "1400", "INVALID_DEADLINES"),
        ("input", {"uri": "file:///bad", "digest": digest(1)}, "INVALID_TERMS"),
        ("delegation", {"maxDepth": 0, "maxChildren": 1}, "INVALID_TERMS"),
        ("refundAddress", POLICY.validator, "VALIDATOR_CONFLICT"),
    ],
)
def testTermSemanticGuards(field, value, error):
    data = example("createTask")["input"]
    data["terms"][field] = value
    assert invoke(
        CoreState(), "createTask", data, contextFor("createTask", data, 1000)
    ) == Rejected(error)


def testCommandBoundaries(harness):
    harness.stage("AWARDED")
    data = example("acceptAward")["input"]
    for reserve, code in [("51", "INVALID_RESERVE"), ("0", None)]:
        data["ownWorkReserveAtoms"] = reserve
        result = invoke(harness.state, "acceptAward", data, contextFor("acceptAward", data, 1499))
        assert isinstance(result, Applied) if code is None else result == Rejected(code)
    data["executionRef"]["awardId"] = 0
    assert invoke(
        harness.state, "acceptAward", data, contextFor("acceptAward", data, 1400)
    ) == Rejected("WRONG_STATE")
    command = example("allocateTask")
    for field, value in [("taskId", "99"), ("chainId", "1"), ("market", address(999))]:
        data = deepcopy(command["input"])
        data["taskRef"][field] = value
        assert invoke(
            harness.state, "allocateTask", data, contextFor("allocateTask", data, 1200)
        ) == Rejected("UNKNOWN_TASK")
    for value, reentrant, code in [(1, True, "WRONG_VALUE"), (0, True, "REENTRANCY")]:
        assert invoke(
            harness.state,
            "allocateTask",
            command["input"],
            contextFor(
                "allocateTask", command["input"], 1200, valueAtoms=value, reentrant=reentrant
            ),
        ) == Rejected(code)
    data = example("createTask")["input"]
    assert invoke(
        CoreState(), "createTask", data, contextFor("createTask", data, 1000, blockNumber=0)
    ) == Rejected("INVALID_RANGE")
    assert invoke(CoreState(), "bogus", {}, contextFor("bogus", {}, 1000)) == Rejected(
        "INVALID_ENCODING"
    )
    harness = Harness().stage("SETTLED")
    for amount, receiver, code in [
        ("0", address(1), "INVALID_RANGE"),
        ("51", address(1), "INSUFFICIENT_CREDIT"),
        ("1", address(0), "INVALID_RANGE"),
    ]:
        data = {"amountAtoms": amount, "receiver": receiver}
        assert invoke(
            harness.state, "withdrawCredit", data, contextFor("withdrawCredit", data, 2700)
        ) == Rejected(code)
    assert taskKey(example("TaskRef")) in harness.state.tasks


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in cases("replay")
        if case["id"]
        in {
            "transfer-after-bid",
            "transferred-new-bid-unset-wallet",
            "validator-role-conflict",
            "approved-operator-cannot-bid",
            "restored-wallet-new-owner-bid",
        }
    ],
    ids=lambda case: case["id"],
)
def testReplayAuthorityGoldens(case):
    initial, action = case["initial"], case["action"]
    if action["operation"] == "transfer":
        h = Harness().stage("BID")
        bid = deepcopy(next(iter(h.task().bids.values())))
        observation = contextFor(
            "submitBid", example("submitBid")["input"], 1100
        ).identityObservation
        newObservation = replace(observation, owner=address(999), verifiedWallet=None)
        h.call("allocateTask", 1200)
        h.call("acceptAward", 1400, identityObservation=newObservation)
        assert next(iter(h.task().bids.values())) == bid
        result = {**initial, "owner": action["owner"], "wallet": newObservation.verifiedWallet}
    else:
        h = Harness().stage("OPEN")
        data = example("submitBid")["input"]
        data.update(permit=None, signature=None)
        caller = ACTORS["OWNER"]
        if initial.get("owner") == "NEW":
            caller = address(999)
            data["offer"]["payout"] = address(998)
        if action.get("executionSignerEqualsValidator"):
            data["offer"]["executionSigner"] = POLICY.validator
        context = contextFor("submitBid", data, 1100, caller)
        owner = caller
        if action.get("callerIsApprovedButNotOwner"):
            owner = ACTORS["OTHER"]
        wallet = None if initial.get("wallet", "set") is None else data["offer"]["payout"]
        context = replace(
            context,
            identityObservation=replace(
                context.identityObservation, owner=owner, verifiedWallet=wallet
            ),
        )
        applied = invoke(h.state, "submitBid", data, context)
        if case["error"]:
            assert applied == Rejected(case["error"])
            result = deepcopy(initial)
        else:
            assert isinstance(applied, Applied)
            result = {
                **initial,
                "bidCount": len(applied.state.tasks[taskKey(example("TaskRef"))].bids),
            }
    assert result == case["expected"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("chainId", 0),
        ("maxDepth", 3),
        ("maxChildren", 5),
        ("synthesisSlack", 0),
        ("minimumAcceptanceWindow", 0),
        ("policyVersion", 2),
        ("asset", {}),
    ],
)
def testFrozenPolicy(field, value):
    with pytest.raises(ValueError, match="Unsupported"):
        replace(POLICY, **{field: value})


def testConsumedVerdictAndOldOwnerAfterTransfer():
    h = Harness().stage("SUBMITTED")
    data = example("settleVerdict")["input"]
    h.state.usedNonces.add(
        nonceKey(POLICY.market, POLICY.validator, "ValidationVerdict", data["record"]["nonce"])
    )
    assert invoke(
        h.state, "settleVerdict", data, contextFor("settleVerdict", data, 2600)
    ) == Rejected("NONCE_USED")
    h = Harness().stage("AWARDED")
    data = example("acceptAward")["input"]
    assert invoke(
        h.state, "acceptAward", data, contextFor("acceptAward", data, 1400, address(999))
    ) == Rejected("UNAUTHORIZED")
