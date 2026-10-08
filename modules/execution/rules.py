"""Private execution configuration and pure checks; protocol rules stay in L1."""

import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import closed, ensure, recordCheck
from modules.domain.records import ProtocolViolation, agentKey
from modules.economics.pricing import PricingCatalog
from modules.economics.records import atoms, boundedInt, checkRuntime, recordDigest
from modules.market_core.accounting import validateChildEnvelope
from modules.market_core.state import CommandContext, CorePolicy, TaskState
from modules.market_core.transitions import validateTerms

TERMINAL = {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}


def executionDigest(profile):
    # Avoid a self-referential hash while binding every executable setting into L5.
    value = deepcopy(profile)
    value["runtime"].pop("configDigest")
    return recordDigest(value)


def checkProfile(profile, schema):
    closed(profile, "version runtime pricing executor capacity limits delegation operator")
    ensure(profile["version"] == "execution-v1", "Execution version")
    recordCheck(profile["operator"], "Address", schema)
    boundedInt(profile["capacity"], 1, 64)
    checkRuntime(profile["runtime"], schema)
    ensure(profile["runtime"]["configDigest"] == executionDigest(profile), "Execution digest")
    executor, limits, delegation = (profile[k] for k in ("executor", "limits", "delegation"))
    closed(executor, "kind image model promptVersion")
    ensure(executor["kind"] in {"DETERMINISTIC", "AGENTS"}, "Executor kind")
    ensure(re.fullmatch(r"sha256:[0-9a-f]{64}", executor["image"]) is not None, "Pinned image ID")
    ensure(executor["promptVersion"] == "structured-copy-v1", "Unsupported prompt", "UNSUPPORTED")
    ensure(executor["model"] == profile["runtime"]["model"], "Model binding")
    closed(
        limits,
        "wallSeconds requestSeconds cpuMillis memoryBytes pids workspaceBytes "
        "outputBytes logBytes requestBytes modelTurns modelCalls toolCalls maxSteps "
        "maxChargeAtoms callChargeAtoms chargeSource validUntil maxRows maxBytes "
        "readAttempts deliveryAttempts commitSeconds pollSeconds",
    )
    for field in (
        "wallSeconds",
        "cpuMillis",
        "memoryBytes",
        "pids",
        "workspaceBytes",
        "outputBytes",
        "logBytes",
        "requestBytes",
        "modelTurns",
        "maxSteps",
        "maxRows",
        "maxBytes",
        "readAttempts",
        "deliveryAttempts",
        "commitSeconds",
    ):
        boundedInt(limits[field], 1)
    boundedInt(limits["requestSeconds"], 1, 10)
    boundedInt(limits["pollSeconds"], 2, 60)
    for field in ("modelCalls", "toolCalls"):
        boundedInt(limits[field], 0, 64)
    ensure(
        limits["maxSteps"] <= 64
        and limits["outputBytes"] <= 1048576
        and limits["requestBytes"] <= 1048576
        and limits["modelTurns"] <= 64,
        "Execution record bounds",
    )
    for field in ("maxChargeAtoms", "callChargeAtoms", "validUntil"):
        atoms(limits[field])
    ensure(
        isinstance(limits["chargeSource"], str) and 0 < len(limits["chargeSource"]) <= 1024,
        "Explicit charge bound source",
    )
    runtime = profile["runtime"]
    expected = [{"resource": "transform", "quantityUnit": "invocation"}]
    if executor["kind"] == "AGENTS":
        expected += [
            {"resource": "modelInput", "quantityUnit": "token"},
            {"resource": "modelOutput", "quantityUnit": "token"},
        ]
        ensure(limits["modelCalls"] > 0, "Model call allowance")
    ensure(
        runtime["resources"] == expected and runtime["envelope"] is not None,
        "Executor inventory must be enforceable",
        "UNSUPPORTED",
    )
    PricingCatalog(profile["pricing"], runtime, schema, 0, historical=True)
    ensure(
        not profile["pricing"]["fixedFees"],
        "Execution pricing requires per-resource rates",
        "UNSUPPORTED",
    )
    closed(
        delegation,
        "enabled ownWorkReserveAtoms maxChildren depositAtoms gasAtoms "
        "publishSeconds synthesisSeconds fallback templates childBudgetAtoms",
    )
    ensure(
        type(delegation["enabled"]) is bool and type(delegation["fallback"]) is bool,
        "Delegation flags",
    )
    boundedInt(delegation["maxChildren"], 0, 4)
    boundedInt(delegation["publishSeconds"], 1)
    boundedInt(delegation["synthesisSeconds"], 120)
    for field in ("ownWorkReserveAtoms", "depositAtoms", "gasAtoms", "childBudgetAtoms"):
        atoms(delegation[field], 96 if field == "ownWorkReserveAtoms" else 256)
    ensure(
        delegation["enabled"]
        or (delegation["ownWorkReserveAtoms"] == "0" and delegation["maxChildren"] == 0),
        "Solo reserve",
    )
    ensure(
        isinstance(delegation["templates"], list) and len(delegation["templates"]) <= 4,
        "Child templates",
    )
    for pair in delegation["templates"]:
        ensure(isinstance(pair, list) and len(pair) == 2, "Template pair")
        for digest in pair:
            recordCheck(digest, "Digest", schema)


def quantities(profile, **values):
    return [
        r | {"quantity": {"numerator": str(values.get(r["resource"], 0)), "denominator": "1"}}
        for r in profile["runtime"]["resources"]
    ]


def checkOutput(raw, shape, maximum=1048576):
    ensure(isinstance(raw, bytes) and len(raw) <= maximum, "Output byte limit", "LIMIT")
    value = strictJson(raw)
    nodes = 0

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        ensure(depth <= 16 and nodes <= 10000, "Output structure limit", "LIMIT")
        if isinstance(item, dict):
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)

    visit(value, 0)
    ensure(not list(Draft202012Validator(shape).iter_errors(value)), "Output shape mismatch")
    return value


def parentState(view):
    return TaskState(
        spec=view["task"],
        status=view["status"],
        allocation=view["allocation"],
        bids={agentKey(view["winningBid"]["offer"]["agentRef"]): view["winningBid"]},
        ownWorkReserveAtoms=int(view["ownWorkReserveAtoms"]),
        reservedChildBudgets=int(view["reservedChildBudgets"]),
        committedChildPayouts=int(view["committedChildPayouts"]),
        childrenCreated=view["childrenCreated"],
        activeChildren=view["activeChildren"],
    )


def validateChildPlan(plan, view, profile, schema, signer, pending=()):
    closed(plan, "version executionRef profileDigest mode slots")
    ensure(
        plan["version"] == 1
        and plan["executionRef"]
        == {"taskRef": view["task"]["taskRef"], "awardId": view["allocation"]["awardId"]}
        and plan["profileDigest"] == recordDigest(profile),
        "Plan binding",
    )
    ensure(plan["mode"] in {"SOLO", "DELEGATE"}, "Plan mode")
    slots, policy = plan["slots"], profile["delegation"]
    ensure(isinstance(slots, list) and len(slots) <= policy["maxChildren"], "Child count")
    ensure(
        (plan["mode"] == "SOLO" and not slots)
        or (plan["mode"] == "DELEGATE" and policy["enabled"] and slots),
        "Delegation disabled",
    )
    parent = parentState(view)
    parent.reservedChildBudgets += sum(int(item["budgetAtoms"]) for item in pending)
    parent.childrenCreated += len(pending)
    ref = view["task"]["taskRef"]
    core = CorePolicy(
        int(ref["chainId"]),
        ref["market"],
        view["winningBid"]["offer"]["agentRef"]["identityRegistry"],
        view["task"]["terms"]["validator"],
    )
    context = CommandContext(
        signer, int(view["stamp"]["blockNumber"]), int(view["stamp"]["blockTimestamp"])
    )
    ids = set()
    for slot in slots:
        closed(slot, "id terms input fallbackInput")
        ensure(
            isinstance(slot["id"], str)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", slot["id"])
            and slot["id"] not in ids,
            "Unique slot IDs",
        )
        ids.add(slot["id"])
        terms = slot["terms"]
        recordCheck(terms, "TaskTerms", schema)
        ensure(
            [terms["outputSchema"]["digest"], terms["validationPolicy"]["digest"]]
            in policy["templates"],
            "Child template",
            "UNSUPPORTED",
        )
        from modules.agent_client.signing import contentDigest

        ensure(
            terms["input"]["digest"] == contentDigest(jsonBytes(slot["input"]))
            and slot["fallbackInput"] == slot["input"],
            "Child input binding",
        )
        ensure(
            terms["refundAddress"] == signer
            and terms["retryOf"] is None
            and terms["delegation"]["maxChildren"] == 0,
            "Child authority",
        )
        try:
            validateTerms(terms, context, core, parent)
            validateChildEnvelope(parent, terms, policy["synthesisSeconds"])
        except ProtocolViolation as error:
            ensure(False, "Child plan: " + error.code, "INVALID_PLAN")
        parent.reservedChildBudgets += int(terms["budgetAtoms"])
        parent.childrenCreated += 1
    ensure(
        sum(int(s["terms"]["budgetAtoms"]) for s in slots) <= int(policy["depositAtoms"]),
        "Gross deposit allowance",
        "LIMIT",
    )
    return deepcopy(plan)
