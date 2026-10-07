"""Joint cost distributions; prediction never supplies a price or authority."""

from copy import deepcopy
from decimal import Decimal
from fractions import Fraction

from modules.agent_client.ports import AdapterError, ensure, recordCheck
from modules.economics.pricing import PricingCatalog
from modules.economics.records import MON, Q, atoms, checkRuntime, recordDigest


def normalizeProbabilities(probabilities, ids):
    ensure(
        isinstance(probabilities, dict) and set(probabilities) == set(ids), "Probability options"
    )
    values = {}
    for key, value in probabilities.items():
        ensure(type(value) in (int, Decimal, Fraction), "Exact probabilities required")
        if isinstance(value, Decimal):
            ensure(
                value.is_finite() and abs(value.as_tuple().exponent) <= 100, "Probability precision"
            )
        value = Fraction(value)
        ensure(0 <= value <= 1, "Probability range")
        values[key] = value
    total = sum(values.values())
    ensure(total > 0 and abs(total - 1) <= Fraction(1, Q), "Probability total")
    return {key: value / total for key, value in values.items()}


def allocateMass(scenarios):
    ensure(0 < len(scenarios) <= 64, "Scenario count")
    tail = sum((prob for _, cost, prob in scenarios if cost is None), Fraction(0))
    points = [(key, cost, prob) for key, cost, prob in scenarios if cost is not None and prob > 0]
    if tail:
        points.append(("tail", None, tail))
    ensure(len({key for key, _, _ in points}) == len(points), "Scenario identity")
    ensure(sum(prob for _, _, prob in points) == 1, "Distribution mass")
    scaled = [prob * Q for _, _, prob in points]
    masses = [value.numerator // value.denominator for value in scaled]
    order = sorted(range(len(points)), key=lambda i: (-(scaled[i] - masses[i]), points[i][0]))
    for i in order[: Q - sum(masses)]:
        masses[i] += 1
    if tail and masses[-1] == 0:
        donor = min(range(len(points) - 1), key=lambda i: (-masses[i], points[i][0]))
        masses[donor] -= 1
        masses[-1] = 1
    return [
        {"id": key, "costAtoms": None if cost is None else str(cost), "probabilityPpm": mass}
        for (key, cost, _), mass in zip(points, masses, strict=True)
        if mass
    ]


def summarizeDistribution(scenarios):
    if not scenarios:
        return dict.fromkeys(("meanAtoms", "p50Atoms", "p80Atoms", "p95Atoms"))
    ordered = sorted(
        scenarios, key=lambda row: (row["costAtoms"] is None, int(row["costAtoms"] or 0), row["id"])
    )
    mean = (
        None
        if any(row["costAtoms"] is None for row in ordered)
        else str(sum(int(row["costAtoms"]) * row["probabilityPpm"] for row in ordered) // Q)
    )
    result = {"meanAtoms": mean}
    for quantile in (50, 80, 95):
        mass = 0
        for row in ordered:
            mass += row["probabilityPpm"]
            if mass >= quantile * 10000:
                result[f"p{quantile}Atoms"] = row["costAtoms"]
                break
    return result


def validateEstimate(estimate, schema):
    recordCheck(estimate, "CostEstimate", schema)
    ensure(atoms(estimate["expiresAt"], 64) > atoms(estimate["createdAt"], 64), "Estimate expiry")
    points = estimate["scenarios"]
    ensure(len({row["id"] for row in points}) == len(points), "Duplicate scenario")
    if estimate["tail"] == "UNKNOWN" or estimate["fallback"] == "ABSTAIN":
        ensure(
            not points and estimate["tail"] == "UNKNOWN" and estimate["fallback"] == "ABSTAIN",
            "Unknown distribution",
        )
    else:
        ensure(points and sum(row["probabilityPpm"] for row in points) == Q, "Probability mass")
        ensure(
            (estimate["tail"] == "UNBOUNDED") == any(r["costAtoms"] is None for r in points),
            "Tail binding",
        )
    ensure(
        all(estimate[key] == value for key, value in summarizeDistribution(points).items()),
        "Incorrect distribution summaries",
    )


def estimateCost(
    task,
    runtime,
    pricing,
    history,
    now,
    schema,
    *,
    probabilities=None,
    context=None,
    leadTimeSeconds=0,
):
    checkRuntime(runtime, schema)
    context = deepcopy(context or {})
    reasons, points, n = [], [], 0
    try:
        catalog = PricingCatalog(pricing, runtime, schema, now)
        envelope = catalog.envelopeCost()
        if envelope is not None:
            for scenario in runtime["scenarios"]:
                if scenario["usage"] is not None:
                    ensure(
                        catalog.withinEnvelope(scenario["usage"]),
                        "Scenario exceeds envelope",
                        "UNAVAILABLE",
                    )
        expires = min(
            now + runtime["ttlSeconds"],
            catalog.expiresAt,
            int(task["terms"]["biddingClose"]) - leadTimeSeconds,
            history.get("expiresAt", 2**64 - 1),
        )
        ensure(expires > now, "Estimate expired", "UNAVAILABLE")
        samples = history["samples"] if history["adequate"] and envelope is not None else []
        ensure(len(samples) <= 32, "History sample limit")
        historyPoints = []
        for i, sample in enumerate(samples):
            ensure(
                catalog.withinEnvelope(sample["usage"]), "History exceeds envelope", "UNAVAILABLE"
            )
            historyPoints.append(
                (f"h:{i:02}", catalog.priceUsage(sample["usage"]), Fraction(1, len(samples)))
            )
        n = len(samples)
        jevPoints = []
        if probabilities is not None:
            try:
                distribution = normalizeProbabilities(
                    probabilities, [s["id"] for s in runtime["scenarios"]]
                )
                for scenario in runtime["scenarios"]:
                    usage = scenario["usage"]
                    cost = (
                        envelope
                        if scenario["id"] == "other"
                        else (None if usage is None else catalog.priceUsage(usage))
                    )
                    jevPoints.append(("j:" + scenario["id"], cost, distribution[scenario["id"]]))
                if envelope is None and not any(
                    cost is None and prob > 0 for _, cost, prob in jevPoints
                ):
                    jevPoints = []
                    reasons.append("UNKNOWN_TAIL")
            except AdapterError:
                jevPoints = []
                reasons.append("INVALID_PREDICTION")
        if historyPoints and jevPoints:
            weight = Fraction(n, n + 20)
            points = [(key, cost, prob * weight) for key, cost, prob in historyPoints]
            points += [(key, cost, prob * (1 - weight)) for key, cost, prob in jevPoints]
            fallback = "MIXTURE"
        elif historyPoints or jevPoints:
            points = historyPoints or jevPoints
            fallback = "HISTORY" if historyPoints else "JEV"
        elif envelope is not None:
            points, fallback = [("bound", envelope, Fraction(1))], "BOUNDS"
        else:
            fallback = "ABSTAIN"
        scenarios = allocateMass(points) if points else []
        tail = (
            "UNKNOWN"
            if not points
            else ("UNBOUNDED" if any(c is None and p for _, c, p in points) else "BOUNDED")
        )
        estimate = {
            "schemaVersion": 1,
            "taskRef": deepcopy(task["taskRef"]),
            "inputDigest": task["terms"]["input"]["digest"],
            "runtimeDigest": recordDigest(runtime),
            "pricingDigest": recordDigest(pricing),
            "estimatorVersion": "hybrid-v1",
            "createdAt": str(now),
            "expiresAt": str(expires),
            "unit": dict(MON),
            "scenarios": scenarios,
            **summarizeDistribution(scenarios),
            "sampleCount": str(n),
            "fallback": fallback,
            "tail": tail,
            "coverage": "Own solo EXECUTION expense only; joint usage; operator-declared envelope. "
            f"{tail}; {fallback}; uncalibrated. Gas, forecast and validation excluded.",
        }
        estimate["estimateId"] = recordDigest(
            ["layer5-estimate-v1", recordDigest(context), estimate]
        )
        validateEstimate(estimate, schema)
        return {
            "estimate": estimate,
            "provenance": context | {"reasons": reasons},
            "reason": fallback,
        }
    except AdapterError as error:
        if error.kind != "UNAVAILABLE":
            raise
        return {
            "estimate": None,
            "provenance": context | {"reasons": [error.detail]},
            "reason": "PRICING_UNAVAILABLE",
        }
