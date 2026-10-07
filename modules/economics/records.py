"""Private, closed economics records; canonical protocol objects stay schema-owned."""

from fractions import Fraction
from math import gcd

from modules.adapters.a2a.profile import jsonBytes
from modules.agent_client.ports import closed, ensure, recordCheck
from modules.agent_client.signing import contentDigest
from modules.domain.records import decodeUnsigned

MON = {"currency": "MON", "decimals": 18}
Q = 1_000_000


def sortedRecord(value):
    if isinstance(value, dict):
        return {key: sortedRecord(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sortedRecord(item) for item in value]
    return value


def recordDigest(value):
    return contentDigest(jsonBytes(sortedRecord(value)))


def boundedInt(value, minimum=0, maximum=2**32 - 1):
    ensure(type(value) is int and minimum <= value <= maximum, "Integer limit")
    return value


def textField(value, limit=128):
    ensure(isinstance(value, str) and 0 < len(value) <= limit, "Text limit")
    return value


def atoms(value, bits=256):
    from modules.domain.records import ProtocolViolation

    try:
        return decodeUnsigned(value, bits)
    except ProtocolViolation as error:
        ensure(False, "Invalid atomic quantity: " + error.code)


def rational(value):
    closed(value, "numerator denominator")
    numerator, denominator = atoms(value["numerator"]), atoms(value["denominator"])
    ensure(denominator > 0 and gcd(numerator, denominator) == 1, "Reduced rational required")
    return Fraction(numerator, denominator)


def fractionRecord(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def ceilAtoms(value):
    amount = (value.numerator + value.denominator - 1) // value.denominator
    ensure(0 <= amount < 2**256, "Cost overflow", "UNAVAILABLE")
    return amount


def checkRuntime(runtime, schema):
    closed(
        runtime,
        "version provider model configDigest taskFamily outputSchemaDigest "
        "validationPolicyDigest resources scenarios envelope maxHistoryAgeSeconds "
        "ttlSeconds scenarioVersion",
    )
    for field in ("version", "provider", "model", "taskFamily", "scenarioVersion"):
        textField(runtime[field], 64)
    for field in ("configDigest", "outputSchemaDigest", "validationPolicyDigest"):
        recordCheck(runtime[field], "Digest", schema)
    for field in ("maxHistoryAgeSeconds", "ttlSeconds"):
        boundedInt(runtime[field], 1)
    ensure(
        isinstance(runtime["resources"], list) and 0 < len(runtime["resources"]) <= 64,
        "Resource inventory",
    )
    keys = []
    for resource in runtime["resources"]:
        closed(resource, "resource quantityUnit")
        keys.append((textField(resource["resource"]), textField(resource["quantityUnit"], 64)))
    ensure(len(set(keys)) == len(keys), "Duplicate resource")
    ensure(
        isinstance(runtime["scenarios"], list) and 1 <= len(runtime["scenarios"]) <= 32,
        "Scenario limit",
    )
    ids = []
    for scenario in runtime["scenarios"]:
        closed(scenario, "id description usage")
        ids.append(textField(scenario["id"], 60))
        textField(scenario["description"], 1024)
        if scenario["usage"] is not None:
            checkUsage(scenario["usage"], runtime)
    ensure(len(set(ids)) == len(ids) and "other" in ids, "Unique scenarios including other")
    envelope = runtime["envelope"]
    if envelope is not None:
        closed(envelope, "usage justification")
        textField(envelope["justification"], 1024)
        checkUsage(envelope["usage"], runtime)


def checkUsage(usage, runtime):
    ensure(isinstance(usage, list) and len(usage) <= 64, "Usage size")
    quantities = {}
    for item in usage:
        closed(item, "resource quantityUnit quantity")
        key = item["resource"], item["quantityUnit"]
        ensure(key not in quantities, "Duplicate usage")
        quantities[key] = rational(item["quantity"])
    expected = {(r["resource"], r["quantityUnit"]) for r in runtime["resources"]}
    ensure(quantities.keys() == expected, "Complete resource inventory required", "UNAVAILABLE")
    return quantities


def checkPolicy(policy):
    closed(policy, "version quantile marginPpm successPpmCap leadTimeSeconds")
    textField(policy["version"], 64)
    ensure(type(policy["quantile"]) is int and policy["quantile"] in (50, 80, 95), "Quantile")
    boundedInt(policy["marginPpm"])
    boundedInt(policy["successPpmCap"], 1, Q)
    boundedInt(policy["leadTimeSeconds"])


def checkForecast(config, schema):
    closed(
        config,
        "enabled model requestVersion feeUnit maxChargeAtoms dailyBudgetAtoms "
        "taskBudgetAtoms dailyCalls timeoutSeconds chargeBoundSource validUntil",
    )
    ensure(type(config["enabled"]) is bool, "Forecast enabled flag")
    textField(config["model"], 64)
    textField(config["requestVersion"], 64)
    recordCheck(config["feeUnit"], "MoneyUnit", schema)
    for field in ("dailyBudgetAtoms", "taskBudgetAtoms"):
        atoms(config[field])
    if config["maxChargeAtoms"] is not None:
        atoms(config["maxChargeAtoms"])
    boundedInt(config["dailyCalls"])
    boundedInt(config["timeoutSeconds"], 1, 10)
    atoms(config["validUntil"], 64)
    if config["chargeBoundSource"] is not None:
        textField(config["chargeBoundSource"], 1024)
