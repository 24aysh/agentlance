"""Deterministic conformance worker; no models, validation verdicts or tools."""

from modules.adapters.a2a.profile import strictJson
from modules.agent_client.ports import ensure


def checkFixtureInput(raw):
    ensure(len(raw) <= 1048576, "Input limit")
    value = strictJson(raw)
    ensure(isinstance(value, dict) and set(value) == {"value"}, "Expected only value")
    ensure(type(value["value"]) is int and -1000 <= value["value"] <= 1000, "Integer value range")


def executeFixture(raw):
    checkFixtureInput(raw)
    return raw
