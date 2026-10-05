"""Synthetic canonical inputs shared across L1 conformance tests."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import settings

from modules.domain.records import taskKey, validateRecord
from modules.market_core.state import (
    Applied,
    CommandContext,
    CorePolicy,
    CoreState,
    IdentityObservation,
    SignatureObservation,
)
from modules.market_core.transitions import applyCommand

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / "specs/schemas/protocol.schema.json").read_text())
OBJECTS = {
    entry["id"]: entry["value"]
    for entry in json.loads((ROOT / "specs/fixtures/objects.json").read_text())["objects"]
}
settings.register_profile("l1", max_examples=100, derandomize=True, deadline=None)
settings.load_profile("l1")


def cases(domain):
    return json.loads((ROOT / f"specs/fixtures/{domain}.json").read_text())["cases"]


def address(value):
    return "0x" + f"{value:040x}"


def digest(value):
    return "0x" + f"{value:064x}"


def example(name):
    return deepcopy(OBJECTS[name])


POLICY = CorePolicy(
    31337,
    OBJECTS["TaskRef"]["market"],
    OBJECTS["AgentRef"]["identityRegistry"],
    OBJECTS["TaskSpec"]["terms"]["validator"],
)
ACTORS = {
    "REQUESTER": OBJECTS["TaskSpec"]["requester"],
    "OWNER": OBJECTS["Bid"]["ownerAtBid"],
    "SIGNER": OBJECTS["Bid"]["offer"]["executionSigner"],
    "PAYOUT": OBJECTS["Bid"]["offer"]["payout"],
    "ANY": address(500),
    "OTHER": address(501),
}


def contextFor(name, data, now, caller=None, **overrides):
    role = {
        "createTask": "REQUESTER",
        "submitBid": "OWNER",
        "acceptAward": "SIGNER",
        "submitResult": "SIGNER",
        "createChildTask": "SIGNER",
        "cancelTask": "REQUESTER",
        "withdrawCredit": "PAYOUT",
    }.get(name, "ANY")
    facts = dict(caller=caller or ACTORS[role], blockNumber=max(10, now // 100), blockTimestamp=now)
    if name in ("createTask", "createChildTask"):
        facts["valueAtoms"] = int(data["terms"]["budgetAtoms"])
    if name == "submitBid":
        offer, permit = data["offer"], data["permit"]
        facts["identityObservation"] = IdentityObservation(
            deepcopy(offer["agentRef"]),
            True,
            ACTORS["OWNER"],
            offer["payout"],
        )
        if permit is not None:
            facts["signatureObservation"] = SignatureObservation(
                "BidPermit",
                POLICY.chainId,
                POLICY.market,
                permit["owner"],
                deepcopy(permit),
                data["signature"],
                True,
            )
    if name == "settleVerdict":
        record = data["record"]
        facts["signatureObservation"] = SignatureObservation(
            "ValidationVerdict",
            POLICY.chainId,
            POLICY.market,
            POLICY.validator,
            deepcopy(record),
            record["signature"],
            True,
        )
    if name == "withdrawCredit":
        facts["transferSucceeded"] = True
    return CommandContext(**(facts | overrides))


class Harness:
    def __init__(self):
        self.state = CoreState()
        self.events = []

    def call(self, name, now, data=None, caller=None, **facts):
        directBid = data is None and name == "submitBid"
        data = deepcopy(data) if data is not None else example(name)["input"]
        if directBid:
            data.update(permit=None, signature=None)
        if name == "settleVerdict":
            data["record"]["expiry"] = str(now + 1000)
        command = {"schemaVersion": 1, "command": name, "input": data}
        validateRecord(command, "Command", SCHEMA)
        before = deepcopy(self.state)
        result = applyCommand(
            self.state, command, contextFor(name, data, now, caller, **facts), POLICY
        )
        assert self.state == before
        assert isinstance(result, Applied), result
        for emitted in result.events:
            validateRecord(emitted, "Event", SCHEMA)
        for evidence in result.evidence:
            validateRecord(evidence, "ReputationEvidence", SCHEMA)
        self.state = result.state
        self.events.extend(deepcopy(result.events))
        return result

    def stage(self, status):
        for name, now, target in [
            ("createTask", 1000, "OPEN"),
            ("submitBid", 1100, "BID"),
            ("allocateTask", 1200, "AWARDED"),
            ("acceptAward", 1400, "RUNNING"),
            ("submitResult", 2400, "SUBMITTED"),
            ("settleVerdict", 2600, "SETTLED"),
        ]:
            self.call(name, now)
            if status == target:
                return self
        raise ValueError(status)

    def task(self, taskId=1):
        return self.state.tasks[taskKey({**OBJECTS["TaskRef"], "taskId": str(taskId)})]


@pytest.fixture
def harness():
    return Harness()
