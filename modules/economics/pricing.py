"""Exact resource pricing, explicit FX and conservative atomic rounding."""

from fractions import Fraction

from modules.agent_client.ports import closed, ensure, recordCheck
from modules.economics.records import MON, atoms, ceilAtoms, checkUsage, rational, textField


class PricingCatalog:
    def __init__(self, snapshot, runtime, schema, now, *, historical=False):
        closed(
            snapshot,
            "version provider model effectiveAt expiresAt source rates fixedFees conversions",
        )
        self.snapshot, self.runtime = snapshot, runtime
        for field in ("version", "provider", "model", "source"):
            textField(snapshot[field], 1024)
        ensure(all(snapshot[f] == runtime[f] for f in ("provider", "model")), "Pricing runtime")
        start, end = atoms(snapshot["effectiveAt"], 64), atoms(snapshot["expiresAt"], 64)
        ensure(start < end, "Pricing validity")
        ensure(historical or start <= now < end, "Stale pricing", "UNAVAILABLE")
        self.expiresAt, self.conversions = end, {}
        ensure(
            isinstance(snapshot["conversions"], list) and len(snapshot["conversions"]) <= 16,
            "Conversion limit",
        )
        for conversion in snapshot["conversions"]:
            closed(conversion, "sourceUnit targetUnit rate observedAt expiresAt source")
            for field in ("sourceUnit", "targetUnit"):
                recordCheck(conversion[field], "MoneyUnit", schema)
            ensure(conversion["targetUnit"] == MON, "Conversion target")
            rate = rational(conversion["rate"])
            ensure(rate > 0, "Positive conversion")
            observed, expires = (
                atoms(conversion["observedAt"], 64),
                atoms(conversion["expiresAt"], 64),
            )
            ensure(observed < expires, "Conversion validity")
            ensure(historical or observed <= now < expires, "Stale conversion", "UNAVAILABLE")
            textField(conversion["source"], 1024)
            key = self.unitKey(conversion["sourceUnit"])
            ensure(key not in self.conversions and conversion["sourceUnit"] != MON, "Duplicate FX")
            self.conversions[key] = rate
            self.expiresAt = min(self.expiresAt, expires)
        ensure(isinstance(snapshot["rates"], list) and len(snapshot["rates"]) <= 64, "Rate limit")
        self.rates = {}
        for rate in snapshot["rates"]:
            closed(rate, "resource quantityUnit rate unit")
            recordCheck(rate["unit"], "MoneyUnit", schema)
            key = textField(rate["resource"]), textField(rate["quantityUnit"], 64)
            ensure(key not in self.rates, "Duplicate price")
            self.rates[key] = (rational(rate["rate"]), rate["unit"])
        ensure(
            isinstance(snapshot["fixedFees"], list) and len(snapshot["fixedFees"]) <= 64,
            "Fixed fee limit",
        )
        seen = set()
        for fee in snapshot["fixedFees"]:
            closed(fee, "id costAtoms unit")
            textField(fee["id"])
            ensure(fee["id"] not in seen, "Duplicate fixed fee")
            seen.add(fee["id"])
            atoms(fee["costAtoms"])
            recordCheck(fee["unit"], "MoneyUnit", schema)

    @staticmethod
    def unitKey(unit):
        return unit["currency"], unit["decimals"]

    def convert(self, value, unit):
        if unit == MON:
            return Fraction(value)
        rate = self.conversions.get(self.unitKey(unit))
        ensure(rate is not None, "Missing conversion", "UNAVAILABLE")
        return value * rate

    def priceUsage(self, usage):
        quantities = checkUsage(usage, self.runtime)
        total = Fraction(0)
        for key, quantity in quantities.items():
            ensure(key in self.rates, "Missing resource price", "UNAVAILABLE")
            rate, unit = self.rates[key]
            total += self.convert(rate * quantity, unit)
        for fee in self.snapshot["fixedFees"]:
            total += self.convert(Fraction(atoms(fee["costAtoms"])), fee["unit"])
        return ceilAtoms(total)

    def envelopeCost(self):
        envelope = self.runtime["envelope"]
        return None if envelope is None else self.priceUsage(envelope["usage"])

    def withinEnvelope(self, usage):
        envelope = self.runtime["envelope"]
        if envelope is None:
            return False
        bounds = checkUsage(envelope["usage"], self.runtime)
        return all(value <= bounds[key] for key, value in checkUsage(usage, self.runtime).items())
