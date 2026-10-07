"""Reviewed arithmetic vectors and trust-boundary failures for private economics."""

import json
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from conftest import SCHEMA, example
from modules.agent_client.ports import AdapterError
from modules.economics.estimator import (
    allocateMass,
    estimateCost,
    normalizeProbabilities,
    summarizeDistribution,
    validateEstimate,
)
from modules.economics.policy import decideBid
from modules.economics.pricing import PricingCatalog
from modules.economics.records import MON, Q, recordDigest

FIXTURES = Path(__file__).resolve().parents[2] / "specs/fixtures/layer-5"


def economicsConfig():
    return json.loads((FIXTURES / "economics.json").read_text())


def usage(n):
    return [
        {
            "resource": "transform",
            "quantityUnit": "invocation",
            "quantity": {"numerator": str(n), "denominator": "1"},
        }
    ]


def history(*values):
    return {
        "samples": [{"reportId": str(i), "usage": usage(n)} for i, n in enumerate(values)],
        "adequate": True,
        "reportIds": [],
        "excludedCount": 0,
        "expiresAt": 2000,
    }


def estimate(config=None, observations=None, **options):
    config = config or economicsConfig()
    return estimateCost(
        example("TaskSpec"),
        config["runtime"],
        config["pricing"],
        history() if observations is None else observations,
        1000,
        SCHEMA,
        **options,
    )["estimate"]


def reputation():
    task = example("TaskSpec")
    return {
        "taskRef": task["taskRef"],
        "agentRef": example("AgentRef"),
        "taskFamily": task["terms"]["taskFamily"],
        "snapshotBlock": task["reputationSnapshotBlock"],
        "counters": {"successes": "0", "failures": "0"},
        "p": 500000,
        "stamp": {
            "source": "FIXTURE",
            "chainId": task["taskRef"]["chainId"],
            "blockNumber": "100",
            "blockHash": "0x" + "01" * 32,
            "blockTimestamp": "1000",
            "finality": "FINALIZED",
        },
    }


def testPricingAndPolicyGoldens():
    config = economicsConfig()
    vector = json.loads((FIXTURES / "vectors.json").read_text())["policy"]
    config["overhead"]["gasAtoms"] = vector["overheadAtoms"]
    config["policy"]["marginPpm"] = vector["marginPpm"]
    task = example("TaskSpec")
    result = decideBid(
        task, estimate(config), reputation(), config["overhead"], config["policy"], 1000, SCHEMA
    )
    assert result["bidAtoms"] == vector["bidAtoms"]
    task["terms"]["budgetAtoms"] = "26"
    assert decideBid(
        task, estimate(config), reputation(), config["overhead"], config["policy"], 1000, SCHEMA
    )["reasons"] == ["OVER_BUDGET"]


def testExplicitConversionAndOneCeil():
    config = economicsConfig()
    price = config["pricing"]
    usd = {"currency": "USD", "decimals": 6}
    price["rates"][0].update(rate={"numerator": "1", "denominator": "3"}, unit=usd)
    price["fixedFees"] = [{"id": "fee", "costAtoms": "1", "unit": usd}]
    price["conversions"] = [
        {
            "sourceUnit": usd,
            "targetUnit": MON,
            "rate": {"numerator": "1", "denominator": "2"},
            "observedAt": "900",
            "expiresAt": "1100",
            "source": "synthetic",
        }
    ]
    assert PricingCatalog(price, config["runtime"], SCHEMA, 1000).priceUsage(usage(1)) == 1
    price["conversions"][0]["expiresAt"] = "1000"
    assert estimate(config) is None
    price["conversions"] = []
    assert estimate(config) is None


def testCompleteResourceInventoryAndFixedFeeOnce():
    config = economicsConfig()
    resources = [
        ("uncached-input", "token", 80, "1", "2"),
        ("cached-input", "token", 20, "1", "10"),
        ("output", "token", 10, "2", "1"),
        ("tool", "call", 1, "3", "1"),
    ]
    runtime, price = config["runtime"], config["pricing"]
    runtime["resources"] = [
        {"resource": resource, "quantityUnit": unit} for resource, unit, *_ in resources
    ]
    quantities = [
        {
            "resource": resource,
            "quantityUnit": unit,
            "quantity": {"numerator": str(n), "denominator": "1"},
        }
        for resource, unit, n, *_ in resources
    ]
    runtime["envelope"]["usage"] = quantities
    runtime["scenarios"][0]["usage"] = quantities
    price["rates"] = [
        {
            "resource": resource,
            "quantityUnit": unit,
            "rate": {"numerator": n, "denominator": d},
            "unit": dict(MON),
        }
        for resource, unit, _, n, d in resources
    ]
    price["fixedFees"] = [{"id": "once", "costAtoms": "7", "unit": dict(MON)}]
    assert estimate(config)["meanAtoms"] == "72"  # 40 + 2 + 20 + 3 + 7.
    price["rates"].pop(1)
    assert estimate(config) is None


def testFusionAndSummaryGoldens():
    vector = json.loads((FIXTURES / "vectors.json").read_text())["distribution"]
    assert summarizeDistribution(vector["scenarios"]) == vector["summary"]
    config = economicsConfig()
    config["runtime"]["envelope"]["usage"] = usage(30)
    config["runtime"]["scenarios"][0]["usage"] = usage(30)
    result = estimate(config, history(10, 20), probabilities={"normal": 1, "other": 0})
    assert result["fallback"] == "MIXTURE" and result["sampleCount"] == "2"
    assert [s["probabilityPpm"] for s in result["scenarios"]] == [45455, 45454, 909091]
    assert result["meanAtoms"] == "286" and result["p80Atoms"] == "300"
    validateEstimate(result, SCHEMA)


@pytest.mark.parametrize(
    "probability,expected",
    [
        (Fraction(49999, Q), "10"),
        (Fraction(50000, Q), "10"),
        (Fraction(50001, Q), None),
        (Fraction(1, 10**9), "10"),
    ],
)
def testTailBoundaries(probability, expected):
    points = allocateMass([("finite", 10, 1 - probability), ("overflow", None, probability)])
    assert summarizeDistribution(points)["p95Atoms"] == expected
    assert summarizeDistribution(points)["meanAtoms"] is None
    assert points[-1]["probabilityPpm"] >= 1


@pytest.mark.parametrize("mutation", ["sum", "missing", "negative", "float", "nan", "bool"])
def testMalformedProviderProbabilities(mutation):
    values = {"normal": Decimal("0.8"), "other": Decimal("0.2")}
    if mutation == "sum":
        values["normal"] = Decimal("0.7")
    if mutation == "missing":
        del values["other"]
    if mutation == "negative":
        values["other"] = Decimal("-0.2")
    if mutation == "float":
        values["other"] = 0.2
    if mutation == "nan":
        values["other"] = Decimal("NaN")
    if mutation == "bool":
        values["other"] = True
    with pytest.raises(AdapterError):
        normalizeProbabilities(values, ("normal", "other"))
    assert estimate(probabilities=values)["fallback"] == "BOUNDS"


def testFallbackAndCensoredHistory():
    assert estimate()["fallback"] == "BOUNDS"
    assert estimate(observations=history(1))["fallback"] == "HISTORY"
    assert estimate(probabilities={"normal": 1, "other": 0})["fallback"] == "JEV"
    assert estimate(observations=history(1) | {"adequate": False})["sampleCount"] == "0"
    config = economicsConfig()
    config["runtime"]["envelope"] = None
    unknown = estimate(config)
    assert unknown["fallback"] == "ABSTAIN" and unknown["meanAtoms"] is None
    unbounded = estimate(
        config, probabilities={"normal": Fraction(9, 10), "other": Fraction(1, 10)}
    )
    assert unbounded["tail"] == "UNBOUNDED"
    assert decideBid(
        example("TaskSpec"),
        unbounded,
        reputation(),
        config["overhead"],
        config["policy"],
        1000,
        SCHEMA,
    )["reasons"] == ["UNBOUNDED_COST"]


@pytest.mark.parametrize(
    "mutation", ["stale", "missing", "overflow", "envelope", "scenario-envelope"]
)
def testUnknownPriceDoesNotBecomeZero(mutation):
    config = economicsConfig()
    if mutation == "stale":
        config["pricing"]["expiresAt"] = "1000"
    if mutation == "missing":
        config["pricing"]["rates"] = []
    if mutation == "overflow":
        config["pricing"]["rates"][0]["rate"]["numerator"] = str(2**256 - 1)
        config["runtime"]["envelope"]["usage"] = usage(2)
    if mutation == "scenario-envelope":
        config["runtime"]["scenarios"][0]["usage"] = usage(2)
    observations = history(2) if mutation == "envelope" else history()
    assert estimate(config, observations) is None


@given(st.lists(st.integers(1, 10000), min_size=1, max_size=32))
def testMassAndQuantileProperties(weights):
    before = deepcopy(weights)
    points = [(str(i), i * 5, Fraction(w, sum(weights))) for i, w in enumerate(weights)]
    result = allocateMass(points)
    assert sum(row["probabilityPpm"] for row in result) == Q
    summary = summarizeDistribution(result)
    assert int(summary["p50Atoms"]) <= int(summary["p80Atoms"]) <= int(summary["p95Atoms"])
    assert weights == before
    assert recordDigest({"b": 1, "a": 2}) == recordDigest({"a": 2, "b": 1})


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("zero", "BID_READY"),
        ("q", "ZERO_SUCCESS_PROXY"),
        ("score", "NONPOSITIVE_SCORE"),
        ("stale", "EXPIRED"),
        ("unknown", "COST_UNKNOWN"),
        ("precision", "COST_UNKNOWN"),
        ("uint96", "OVER_BUDGET"),
        ("cap", "BID_READY"),
    ],
)
def testBidPolicyBoundaries(mutation, reason):
    config, rep, task = economicsConfig(), reputation(), example("TaskSpec")
    if mutation == "zero":
        config["pricing"]["rates"][0]["rate"]["numerator"] = "0"
    if mutation == "q":
        rep.update(p=0, counters={"successes": "0", "failures": str(2**64 - 1)})
    if mutation == "score":
        task["terms"]["alphaNum"] = "99"
    if mutation == "stale":
        config["overhead"]["expiresAt"] = "1000"
    if mutation == "unknown":
        config["overhead"]["gasAtoms"] = None
    if mutation == "uint96":
        config["pricing"]["rates"][0]["rate"]["numerator"] = str(2**95)
        task["terms"]["budgetAtoms"] = str(2**96 - 1)
    if mutation == "cap":
        config["policy"]["successPpmCap"] = 250000
    predicted = estimate(config)
    if mutation == "precision":
        predicted["unit"]["decimals"] = 6
    result = decideBid(task, predicted, rep, config["overhead"], config["policy"], 1000, SCHEMA)
    assert result["reasons"] == [reason]
    if mutation == "zero":
        assert result["bidAtoms"] == "0"
    if mutation == "cap":
        assert result["bidAtoms"] == "40"


def testEstimateSemanticValidationAndDigestInvalidation():
    predicted = estimate()
    for mutation in ("summary", "probability", "expiry", "tail"):
        value = deepcopy(predicted)
        if mutation == "summary":
            value["p80Atoms"] = "0"
        if mutation == "probability":
            value["scenarios"][0]["probabilityPpm"] = 999999
        if mutation == "expiry":
            value["expiresAt"] = value["createdAt"]
        if mutation == "tail":
            value["tail"] = "UNBOUNDED"
        with pytest.raises(AdapterError):
            validateEstimate(value, SCHEMA)
    config = economicsConfig()
    first = estimate(config)
    config["pricing"]["version"] = "new-prices"
    assert estimate(config)["estimateId"] != first["estimateId"]
    config = economicsConfig()
    config["runtime"]["configDigest"] = "0x" + "ab" * 32
    assert estimate(config)["runtimeDigest"] != first["runtimeDigest"]
    assert normalizeProbabilities(
        {"a": Decimal("0.5000005"), "b": Decimal("0.5000005")}, ["a", "b"]
    ) == {"a": Fraction(1, 2), "b": Fraction(1, 2)}
