"""Atomic reference transitions over schema-validated commands and explicit observations."""

from copy import deepcopy

from modules.domain.records import (
    ProtocolViolation,
    addressBytes,
    agentKey,
    decodeUnsigned,
    isContentUri,
    require,
    taskKey,
    unsigned,
)
from modules.market_core.accounting import (
    settlementAmounts,
    validateChildEnvelope,
    validateLedger,
    withdrawAmount,
)
from modules.market_core.market import (
    calculateAllocation,
    calculateScore,
    updateTopTwo,
    validateAlpha,
)
from modules.market_core.reputation import (
    COUNTER_EFFECTS,
    calculateProbability,
    reduceReceipt,
    snapshotCounters,
)
from modules.market_core.state import (
    Applied,
    CommandContext,
    CorePolicy,
    CoreState,
    Rejected,
    TaskState,
)

STATES = {
    "submitBid": "OPEN",
    "allocateTask": "OPEN",
    "acceptAward": "AWARDED",
    "createChildTask": "RUNNING",
    "submitResult": "RUNNING",
    "settleVerdict": "SUBMITTED",
    "cancelTask": "OPEN",
}
CUTOFFS = {
    "OPEN": ("allocationBy", "ALLOCATION_EXPIRED"),
    "AWARDED": ("acceptBy", "NO_SHOW"),
    "RUNNING": ("resultBy", "EXECUTION_TIMEOUT"),
    "SUBMITTED": ("validationBy", "VALIDATOR_TIMEOUT"),
}


def event(name: str, **payload) -> dict:
    return {"schemaVersion": 1, "policyVersion": 1, "name": name, "payload": payload}


def nonceKey(market: str, signer: str, primaryType: str, nonce: str) -> tuple:
    return market, signer, primaryType, decodeUnsigned(nonce, 256)


def winnerBid(task: TaskState) -> dict:
    return task.bids[agentKey(task.allocation["winner"])]


def resolveTask(state: CoreState, ref: dict, policy: CorePolicy) -> TaskState:
    require(
        int(ref["chainId"]) == policy.chainId and ref["market"] == policy.market,
        "UNKNOWN_TASK",
    )
    key = taskKey(ref)
    require(key in state.tasks, "UNKNOWN_TASK")
    return state.tasks[key]


def checkSignature(
    context: CommandContext,
    policy: CorePolicy,
    kind: str,
    signer: str,
    record: dict,
    signature: str,
) -> None:
    observation = context.signatureObservation
    if observation is None:
        raise ValueError("Missing signature observation")
    require(
        observation.primaryType == kind
        and observation.chainId == policy.chainId
        and observation.market == policy.market
        and observation.signer == signer
        and observation.record == record
        and observation.signature == signature
        and observation.valid is True,
        "INVALID_SIGNATURE",
    )


def checkNonce(
    state: CoreState,
    context: CommandContext,
    policy: CorePolicy,
    kind: str,
    signer: str,
    record: dict,
) -> tuple:
    key = nonceKey(policy.market, signer, kind, record["nonce"])
    require(key not in state.usedNonces, "NONCE_USED")
    require(context.blockTimestamp < int(record["expiry"]), "SIGNATURE_EXPIRED")
    return key


def validateTerms(
    terms: dict, context: CommandContext, policy: CorePolicy, parent: TaskState | None
) -> None:
    require(
        terms["policyVersion"] == policy.policyVersion
        and terms["taskFamily"] == "structured-output-v1"
        and terms["validator"] == policy.validator,
        "UNSUPPORTED_POLICY",
    )
    require(terms["asset"] == policy.asset, "INVALID_ASSET")
    require(decodeUnsigned(terms["budgetAtoms"], 96) > 0, "INVALID_BUDGET")
    validateAlpha(decodeUnsigned(terms["alphaNum"], 96), decodeUnsigned(terms["alphaDen"], 96))
    deadlines = [
        decodeUnsigned(terms[key], 64)
        for key in ("biddingClose", "allocationBy", "acceptBy", "resultBy", "validationBy")
    ]
    require(
        all(
            left < right
            for left, right in zip([context.blockTimestamp, *deadlines], deadlines, strict=False)
        )
        and deadlines[2] - deadlines[1] >= policy.minimumAcceptanceWindow,
        "INVALID_DEADLINES",
    )
    require(context.blockNumber > 0, "INVALID_RANGE")
    for key in ("input", "outputSchema", "validationPolicy"):
        require(isContentUri(terms[key]["uri"]), "INVALID_TERMS")
    depth = parent.spec["depth"] + 1 if parent else 0
    limits = terms["delegation"]
    require(
        0 <= limits["maxDepth"] <= policy.maxDepth
        and 0 <= limits["maxChildren"] <= policy.maxChildren
        and (depth != limits["maxDepth"] or limits["maxChildren"] == 0),
        "INVALID_TERMS",
    )
    roles = [context.caller, terms["refundAddress"]]
    if parent:
        roles.extend(
            [winnerBid(parent)["offer"]["executionSigner"], winnerBid(parent)["offer"]["payout"]]
        )
    require(policy.validator not in roles, "VALIDATOR_CONFLICT")


def validateRetry(state: CoreState, terms: dict, caller: str, parent: TaskState | None) -> None:
    ref = terms["retryOf"]
    if ref is None:
        return
    prior = state.tasks.get(taskKey(ref))
    require(prior is not None, "INVALID_RETRY")
    require(
        prior.status == "SETTLED"
        and prior.receipt["reason"] != "SUCCESS"
        and prior.spec["requester"] == caller,
        "INVALID_RETRY",
    )
    expectedParent = parent.spec["taskRef"] if parent else None
    require(prior.spec["parentRef"] == expectedParent, "INVALID_RETRY")
    if parent:
        require(prior.spec["rootRef"] == parent.spec["rootRef"], "INVALID_RETRY")


def createTask(
    state: CoreState,
    terms: dict,
    context: CommandContext,
    policy: CorePolicy,
    parent: TaskState | None,
) -> dict:
    validateTerms(terms, context, policy, parent)
    if parent:
        validateChildEnvelope(parent, terms, policy.synthesisSlack)
    validateRetry(state, terms, context.caller, parent)
    budget = int(terms["budgetAtoms"])
    require(context.valueAtoms == budget, "WRONG_VALUE")
    require(state.taskCount < 2**64 - 1, "TASK_LIMIT")
    state.taskCount += 1
    ref = {"chainId": str(policy.chainId), "market": policy.market, "taskId": str(state.taskCount)}
    spec = {
        "schemaVersion": 1,
        "taskRef": ref,
        "requester": context.caller,
        "terms": terms,
        "parentRef": parent.spec["taskRef"] if parent else None,
        "rootRef": parent.spec["rootRef"] if parent else ref,
        "depth": parent.spec["depth"] + 1 if parent else 0,
        "createdAt": str(context.blockTimestamp),
        "createdBlock": str(context.blockNumber),
        "reputationSnapshotBlock": str(context.blockNumber - 1),
    }
    state.tasks[taskKey(ref)] = TaskState(spec, escrowAtoms=budget)
    state.depositedAtoms += budget
    if parent:
        parent.reservedChildBudgets += budget
        parent.activeChildren += 1
        parent.childrenCreated += 1
    return event("TaskCreated", task=spec)


def admitBid(
    state: CoreState, task: TaskState, data: dict, context: CommandContext, policy: CorePolicy
) -> dict:
    offer, permit, signature = data["offer"], data["permit"], data["signature"]
    ref = offer["agentRef"]
    require(
        int(ref["chainId"]) == policy.chainId
        and ref["identityRegistry"] == policy.identityRegistry,
        "IDENTITY_UNAVAILABLE",
    )
    observation = context.identityObservation
    if observation is None or observation.agentRef != ref:
        raise ValueError("Missing or mismatched identity observation")
    require(observation.available, "IDENTITY_UNAVAILABLE")
    if observation.owner is None:
        raise ValueError("Available identity must have an owner")
    addressBytes(observation.owner)
    require((permit is None) == (signature is None), "INVALID_SIGNATURE")
    if permit is None:
        require(context.caller == observation.owner, "UNAUTHORIZED")
    else:
        require(permit["owner"] == observation.owner, "OWNER_CHANGED")
    require(observation.verifiedWallet is not None, "WALLET_UNSET")
    require(observation.verifiedWallet == offer["payout"], "WALLET_MISMATCH")
    key = None
    if permit is not None:
        require(all(permit[field] == value for field, value in offer.items()), "INVALID_SIGNATURE")
        checkSignature(context, policy, "BidPermit", observation.owner, permit, signature)
        key = checkNonce(state, context, policy, "BidPermit", observation.owner, permit)
    require(
        policy.validator not in (observation.owner, offer["executionSigner"], offer["payout"]),
        "VALIDATOR_CONFLICT",
    )
    require(int(offer["bidAtoms"]) <= int(task.spec["terms"]["budgetAtoms"]), "BID_OVER_BUDGET")
    identity = agentKey(ref)
    require(identity not in task.bids, "DUPLICATE_AGENT")
    successes, failures = snapshotCounters(
        state.reputation,
        ref,
        task.spec["terms"]["taskFamily"],
        int(task.spec["reputationSnapshotBlock"]),
    )
    p = calculateProbability(successes, failures)
    score = calculateScore(
        p,
        int(task.spec["terms"]["alphaNum"]),
        int(task.spec["terms"]["alphaDen"]),
        int(offer["bidAtoms"]),
    )
    bid = {
        "schemaVersion": 1,
        "offer": offer,
        "ownerAtBid": observation.owner,
        "snapshotBlock": task.spec["reputationSnapshotBlock"],
        "counters": {"successes": str(successes), "failures": str(failures)},
        "p": p,
        "score": str(score),
    }
    task.bids[identity] = bid
    task.topTwo = updateTopTwo(task.topTwo, bid)
    if key is not None:
        state.usedNonces.add(key)
    return event("BidAccepted", bid=bid, permitNonce=permit["nonce"] if permit else None)


def settleTask(
    state: CoreState,
    task: TaskState,
    reason: str,
    context: CommandContext,
    validation: dict | None = None,
) -> tuple[dict, tuple]:
    terms = task.spec["terms"]
    awarded = task.allocation is not None and task.allocation["winner"] is not None
    reserve = int(task.allocation["reservedAtoms"]) if awarded else 0
    paid, refund = settlementAmounts(int(terms["budgetAtoms"]), reserve, reason)
    receipt = {
        "schemaVersion": 1,
        "taskRef": task.spec["taskRef"],
        "reason": reason,
        "winner": task.allocation["winner"] if awarded else None,
        "payout": winnerBid(task)["offer"]["payout"] if awarded else None,
        "refundAddress": terms["refundAddress"],
        "budgetAtoms": terms["budgetAtoms"],
        "reservedAtoms": str(reserve),
        "paidAtoms": str(paid),
        "refundAtoms": str(refund),
        "resultDigest": task.result["artifact"]["digest"] if task.result else None,
        "validation": validation,
        "counterEffect": COUNTER_EFFECTS[reason],
        "settledAt": str(context.blockTimestamp),
    }
    task.status, task.escrowAtoms, task.receipt = "SETTLED", 0, receipt
    for address, amount in ((receipt["payout"], paid), (receipt["refundAddress"], refund)):
        if amount:
            state.credits[address] = state.credits.get(address, 0) + amount
    state.reputation, evidence = reduceReceipt(
        state.reputation,
        task.spec,
        receipt,
        context.blockNumber,
        state.taskCount,
    )
    if task.spec["parentRef"] is not None:
        parent = state.tasks[taskKey(task.spec["parentRef"])]
        parent.reservedChildBudgets -= int(terms["budgetAtoms"])
        parent.committedChildPayouts += paid
        parent.activeChildren -= 1
    return event("TaskSettled", receipt=receipt), (evidence,) if evidence else ()


def targetRef(name: str, data: dict) -> dict:
    if name == "submitBid":
        return data["offer"]["taskRef"]
    if name == "settleVerdict":
        return data["record"]["executionRef"]["taskRef"]
    if name in ("acceptAward", "submitResult"):
        return data["executionRef"]["taskRef"]
    if name == "createChildTask":
        return data["parentRef"]
    return data["taskRef"]


def checkTaskCommand(task: TaskState, name: str, data: dict, context: CommandContext) -> None:
    require(task.status != "SETTLED", "ALREADY_SETTLED")
    if name != "expireTask":
        require(task.status == STATES[name], "WRONG_STATE")
    if name in ("acceptAward", "submitResult", "settleVerdict"):
        execution = (
            data["record"]["executionRef"] if name == "settleVerdict" else data["executionRef"]
        )
        require(execution["awardId"] == 1, "WRONG_STATE")
    if name in ("acceptAward", "submitResult", "createChildTask"):
        require(context.caller == winnerBid(task)["offer"]["executionSigner"], "UNAUTHORIZED")
    if name == "cancelTask":
        require(context.caller == task.spec["requester"], "UNAUTHORIZED")
    terms, now = task.spec["terms"], context.blockTimestamp
    if name == "expireTask":
        require(now >= int(terms[CUTOFFS[task.status][0]]), "TOO_EARLY")
    else:
        cutoff = {
            "submitBid": "biddingClose",
            "cancelTask": "biddingClose",
            "allocateTask": "allocationBy",
            "acceptAward": "acceptBy",
            "createChildTask": "resultBy",
            "submitResult": "resultBy",
            "settleVerdict": "validationBy",
        }[name]
        if name == "allocateTask":
            require(now >= int(terms["biddingClose"]), "TOO_EARLY")
        require(now < int(terms[cutoff]), "DEADLINE_PASSED")


def runCommand(
    state: CoreState, name: str, data: dict, context: CommandContext, policy: CorePolicy
) -> tuple[dict, tuple]:
    if name == "createTask":
        return createTask(state, data["terms"], context, policy, None), ()
    if name == "withdrawCredit":
        amount = decodeUnsigned(data["amountAtoms"], 256)
        balance = withdrawAmount(
            state.credits.get(context.caller, 0), amount, data["receiver"], True
        )
        if type(context.transferSucceeded) is not bool:
            raise ValueError("Missing transfer outcome")
        require(context.transferSucceeded, "TRANSFER_FAILED")
        state.credits[context.caller] = balance
        state.withdrawnAtoms += amount
        return event(
            "CreditWithdrawn",
            owner=context.caller,
            receiver=data["receiver"],
            amountAtoms=str(amount),
        ), ()
    task = resolveTask(state, targetRef(name, data), policy)
    if name == "submitBid" and data["permit"] is not None:
        require(data["permit"]["taskRef"] == task.spec["taskRef"], "UNKNOWN_TASK")
    checkTaskCommand(task, name, data, context)
    if name == "submitBid":
        return admitBid(state, task, data, context, policy), ()
    if name == "createChildTask":
        return createTask(state, data["terms"], context, policy, task), ()
    if name == "allocateTask":
        task.allocation = calculateAllocation(task.spec, task.topTwo, context.blockTimestamp)
        if task.allocation["outcome"] == "UNALLOCATED":
            return settleTask(state, task, "UNALLOCATED", context)
        task.status = "AWARDED"
        return event("TaskAwarded", allocation=task.allocation), ()
    if name == "acceptAward":
        reserve = decodeUnsigned(data["ownWorkReserveAtoms"], 96)
        require(reserve <= int(task.allocation["reservedAtoms"]), "INVALID_RESERVE")
        task.status, task.ownWorkReserveAtoms = "RUNNING", reserve
        return event(
            "AwardAccepted",
            executionRef=data["executionRef"],
            ownWorkReserveAtoms=str(reserve),
            acceptedAt=str(context.blockTimestamp),
        ), ()
    if name == "submitResult":
        require(task.activeChildren == 0, "CHILDREN_ACTIVE")
        require(isContentUri(data["artifact"]["uri"]), "INVALID_TERMS")
        task.status = "SUBMITTED"
        task.result = {"schemaVersion": 1, **data, "submittedAt": str(context.blockTimestamp)}
        return event("ResultSubmitted", result=task.result), ()
    if name == "settleVerdict":
        record = data["record"]
        require(
            record["agentRef"] == task.allocation["winner"]
            and record["resultDigest"] == task.result["artifact"]["digest"],
            "RESULT_MISMATCH",
        )
        require(
            record["validationPolicyDigest"] == task.spec["terms"]["validationPolicy"]["digest"],
            "POLICY_MISMATCH",
        )
        require(record["validator"] == policy.validator, "INVALID_SIGNATURE")
        checkSignature(
            context, policy, "ValidationVerdict", policy.validator, record, record["signature"]
        )
        key = checkNonce(state, context, policy, "ValidationVerdict", policy.validator, record)
        state.usedNonces.add(key)
        reason = "SUCCESS" if record["verdict"] == "PASS" else "VALIDATION_FAILED"
        return settleTask(state, task, reason, context, record)
    if name == "cancelTask":
        require(not task.bids, "INVALID_TERMS")
        return settleTask(state, task, "CANCELLED", context)
    return settleTask(state, task, CUTOFFS[task.status][1], context)


def applyCommand(
    state: CoreState, command: dict, context: CommandContext, policy: CorePolicy
) -> Applied | Rejected:
    validateLedger(state)
    try:
        unsigned(context.blockNumber, 64)
        unsigned(context.blockTimestamp, 64)
        unsigned(context.valueAtoms, 256)
        addressBytes(context.caller)
        name = command["command"]
        require(name in {*STATES, "createTask", "expireTask", "withdrawCredit"}, "INVALID_ENCODING")
        if name not in ("createTask", "createChildTask"):
            require(context.valueAtoms == 0, "WRONG_VALUE")
        require(not context.reentrant, "REENTRANCY")
        working = deepcopy(state)
        emitted, evidence = runCommand(working, name, deepcopy(command["input"]), context, policy)
        validateLedger(working)
        return deepcopy(Applied(working, (emitted,), evidence))
    except ProtocolViolation as error:
        return Rejected(error.code)
