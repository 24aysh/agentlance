"""Consume independently verified frozen signature vectors, not self-generated expectations."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from modules.agent_client.signing import CURVE_ORDER, typedDigest, verifyContractReturn, verifyEoa

VECTORS = json.loads(Path("specs/fixtures/signatures.json").read_text())
BID = VECTORS["positive"][0]


@pytest.mark.parametrize("vector", VECTORS["positive"], ids=lambda v: v["id"])
def testFrozenSignatures(vector):
    assert typedDigest(vector["typedData"]) == vector["digest"]
    assert verifyEoa(vector["typedData"], vector["signature"], vector["recoveredSigner"])


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS["negative"] if v["mutation"] and v["id"] != "wrong-primary-type"],
    ids=lambda v: v["id"],
)
def testFrozenTampering(vector):
    base = next(v for v in VECTORS["positive"] if v["id"] == vector["base"])
    typed = deepcopy(base["typedData"])
    mutation = vector["mutation"]
    typed[mutation["section"]][mutation["field"]] = mutation["value"]
    assert not verifyEoa(typed, base["signature"], base["recoveredSigner"])


@pytest.mark.parametrize(
    "kind",
    ["empty", "short", "long", "v", "high-s", "zero-s", "zero-r", "invalid-r", "hex", "owner"],
)
def testSignatureBoundary(kind):
    signature = BID["signature"]
    owner = BID["recoveredSigner"]
    raw = bytearray.fromhex(signature[2:])
    if kind == "empty":
        signature = "0x"
    if kind == "short":
        signature = signature[:-2]
    if kind == "long":
        signature += "00"
    if kind == "hex":
        signature = "0xGG"
    if kind in {"zero-r", "invalid-r"}:
        raw[:32] = (0 if kind == "zero-r" else CURVE_ORDER - 1).to_bytes(32)
        signature = "0x" + raw.hex()
    if kind == "owner":
        owner = "0x" + "42" * 20
    if kind in {"v", "high-s", "zero-s"}:
        if kind == "v":
            raw[64] = 0
        else:
            raw[32:64] = (CURVE_ORDER - 1 if kind == "high-s" else 0).to_bytes(32)
        signature = "0x" + raw.hex()
    assert not verifyEoa(BID["typedData"], signature, owner)


@pytest.mark.parametrize(
    "outcome,data,expected",
    [
        ("RETURNED", "0x1626ba7e" + "00" * 28, True),
        ("RETURNED", "0x1626ba7e", False),
        ("RETURNED", "0x" + "00" * 32, False),
        ("REVERTED", None, False),
        ("OUT_OF_GAS", None, False),
        ("RETURNED", None, False),
        ("RETURNED", "0x1626ba7e" + "00" * 29, False),
    ],
)
def testContractAbi(outcome, data, expected):
    assert verifyContractReturn({"outcome": outcome, "returnData": data}) is expected
