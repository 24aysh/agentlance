"""Adapter boundaries. Canonical records remain schema-owned mappings."""

from dataclasses import dataclass
from typing import Protocol

from modules.domain.records import ProtocolViolation, validateRecord


class AdapterError(Exception):
    def __init__(self, kind, detail):
        self.kind, self.detail = kind, detail
        super().__init__(f"{kind}: {detail}")


def ensure(condition, detail, kind="INVALID_DATA"):
    if not condition:
        raise AdapterError(kind, detail)


def closed(record, fields):
    ensure(isinstance(record, dict) and set(record) == set(fields.split()), "Unexpected fields")


def recordCheck(value, name, schema):
    try:
        validateRecord(value, name, schema)
    except ProtocolViolation as error:
        raise AdapterError("INVALID_DATA", f"{name}: {error.code}") from error


def checkStamp(stamp, schema):
    closed(stamp, "source chainId blockNumber blockHash blockTimestamp finality")
    ensure(stamp["source"] in {"FIXTURE", "MONAD"}, "Unknown observation source")
    ensure(stamp["finality"] in {"PENDING", "FINALIZED"}, "Unknown finality")
    for field, name in (
        ("chainId", "Uint256"),
        ("blockNumber", "Uint64"),
        ("blockTimestamp", "Uint64"),
        ("blockHash", "Digest"),
    ):
        recordCheck(stamp[field], name, schema)
    ensure(int(stamp["chainId"]) > 0, "Zero chain")


def checkIdentity(value, schema):
    closed(value, "agentRef owner verifiedWallet registrationUri ownerHasCode stamp")
    for field, name in (("agentRef", "AgentRef"), ("owner", "Address"), ("registrationUri", "Uri")):
        recordCheck(value[field], name, schema)
    if value["verifiedWallet"] is not None:
        recordCheck(value["verifiedWallet"], "Address", schema)
    ensure(type(value["ownerHasCode"]) is bool, "Invalid code observation")
    checkStamp(value["stamp"], schema)
    ensure(value["agentRef"]["chainId"] == value["stamp"]["chainId"], "Identity chain")


def checkReputation(value, schema):
    from modules.market_core.reputation import calculateProbability

    closed(value, "taskRef agentRef taskFamily snapshotBlock counters p stamp")
    for field, kind in (
        ("taskRef", "TaskRef"),
        ("agentRef", "AgentRef"),
        ("snapshotBlock", "Uint64"),
        ("counters", "Counters"),
    ):
        recordCheck(value[field], kind, schema)
    checkStamp(value["stamp"], schema)
    ensure(
        value["stamp"]["finality"] == "FINALIZED"
        and value["stamp"]["chainId"] == value["taskRef"]["chainId"] == value["agentRef"]["chainId"]
        and int(value["snapshotBlock"]) <= int(value["stamp"]["blockNumber"]),
        "Reputation observation",
    )
    ensure(
        type(value["p"]) is int
        and value["p"]
        == calculateProbability(
            int(value["counters"]["successes"]), int(value["counters"]["failures"])
        ),
        "Reputation probability",
    )


def checkTaskView(view, schema):
    closed(
        view,
        "task status allocation winningBid ownWorkReserveAtoms childrenCreated "
        "activeChildren reservedChildBudgets committedChildPayouts result receipt stamp",
    )
    recordCheck(view["task"], "TaskSpec", schema)
    checkStamp(view["stamp"], schema)
    ref = view["task"]["taskRef"]
    ensure(ref["chainId"] == view["stamp"]["chainId"], "Task chain")
    ensure(view["status"] in {"OPEN", "AWARDED", "RUNNING", "SUBMITTED", "SETTLED"}, "State")
    for field, name in (
        ("allocation", "Allocation"),
        ("winningBid", "Bid"),
        ("result", "ResultCommitment"),
        ("receipt", "SettlementReceipt"),
    ):
        if view[field] is not None:
            recordCheck(view[field], name, schema)
    for field in ("ownWorkReserveAtoms", "reservedChildBudgets", "committedChildPayouts"):
        recordCheck(view[field], "Uint96", schema)
    for field in ("childrenCreated", "activeChildren"):
        ensure(type(view[field]) is int and 0 <= view[field] <= 4, "Child count")
    ensure(view["activeChildren"] <= view["childrenCreated"], "Active child count")
    ensure((view["status"] == "SETTLED") == (view["receipt"] is not None), "Receipt state")
    allocation, bid = view["allocation"], view["winningBid"]
    if allocation is not None:
        ensure(allocation["taskRef"] == ref, "Allocation ref")
    awarded = allocation is not None and allocation["outcome"] == "AWARDED"
    ensure((bid is not None) == awarded, "Winner binding")
    if awarded:
        ensure(
            bid["offer"]["taskRef"] == ref and bid["offer"]["agentRef"] == allocation["winner"],
            "Bid binding",
        )
    if view["status"] in {"AWARDED", "RUNNING", "SUBMITTED"}:
        ensure(awarded, "Missing award")
    if view["result"] is not None:
        ensure(view["result"]["executionRef"] == {"taskRef": ref, "awardId": 1}, "Result ref")
        ensure(view["status"] in {"SUBMITTED", "SETTLED"}, "Premature result")
    if view["status"] == "SUBMITTED":
        ensure(view["result"] is not None, "Missing result")
    if view["receipt"] is not None:
        ensure(view["receipt"]["taskRef"] == ref, "Receipt ref")


@dataclass(frozen=True)
class ResolvedProfile:
    profile: dict
    registry: dict
    registrationBytes: bytes
    cardBytes: bytes
    selectedInterfaceIndex: int


class RegistryPort(Protocol):
    async def readIdentity(self, agentRef: dict) -> dict: ...
    async def checkContractSignature(
        self, owner: str, digest: str, signature: str, stamp: dict, gasLimit: int = 50000
    ) -> dict: ...


class MarketPort(Protocol):
    async def readReputation(self, taskRef: dict, agentRef: dict) -> dict: ...
    async def readTask(self, taskRef: dict) -> dict: ...
    async def readBid(self, taskRef: dict, agentRef: dict) -> dict: ...
    async def observeAward(self, executionRef: dict) -> dict: ...
    async def submitSignedBid(self, command: dict, operationId: str) -> dict: ...
    async def acceptAward(self, command: dict, operationId: str) -> dict: ...
    async def publishChild(self, command: dict, valueAtoms: str, operationId: str) -> dict: ...
    async def commitResult(self, command: dict, operationId: str) -> dict: ...
    async def readSettlement(self, taskRef: dict) -> dict: ...
    async def readOperation(self, operationId: str) -> dict: ...


class ContentPort(Protocol):
    async def fetchBytes(
        self, uri: str, maximumBytes: int, expectedDigest: str | None = None
    ) -> bytes: ...
    def publishResult(self, executionRef: dict, raw: bytes) -> dict: ...
