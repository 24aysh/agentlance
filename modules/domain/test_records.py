import json

import pytest

from conftest import OBJECTS, ROOT, SCHEMA, address, example
from modules.domain.records import (
    ProtocolViolation,
    addressBytes,
    agentKey,
    decodeRecord,
    decodeUnsigned,
    isContentUri,
    isHttpsUrl,
    isInt256,
    isUnsigned,
    parseJson,
    taskKey,
    unsigned,
    validateRecord,
)


@pytest.mark.parametrize(
    "entry",
    json.loads((ROOT / "specs/fixtures/objects.json").read_text())["objects"],
    ids=lambda entry: entry["id"],
)
def testCanonicalExamples(entry):
    assert (
        decodeRecord(json.dumps(entry["value"]).encode(), entry["type"], SCHEMA) == entry["value"]
    )


@pytest.mark.parametrize(
    "raw", [b"\xef\xbb\xbf{}", b"\xff", b'{"x":1,"x":2}', b"NaN", b"Infinity", b"1.0", b"1e3", b"{"]
)
def testStrictJson(raw):
    with pytest.raises(ProtocolViolation, match="INVALID_ENCODING"):
        parseJson(raw)


@pytest.mark.parametrize("value", ["01", "+1", "1e3", " 1", "1\n", "-0", "-1", 1, True])
def testIntegerEncoding(value):
    with pytest.raises(ProtocolViolation, match="INVALID_ENCODING"):
        decodeUnsigned(value, 96)


@pytest.mark.parametrize("value", [True, -1, 2**96, 1.0])
def testIntegerRange(value):
    with pytest.raises(ProtocolViolation, match="INVALID_RANGE"):
        unsigned(value, 96)


def testWidthsAndIdentityKeys():
    for width in (64, 96, 256):
        assert decodeUnsigned(str(2**width - 1), width) == 2**width - 1
        with pytest.raises(ProtocolViolation, match="INVALID_RANGE"):
            decodeUnsigned(str(2**width), width)
    with pytest.raises(ProtocolViolation, match="INVALID_RANGE"):
        decodeUnsigned("1" * 79, 256)
    assert not isUnsigned("1" * 79, 256)
    assert isInt256(str(-(2**255))) and isInt256(str(2**255 - 1))
    assert not isInt256(str(2**255)) and not isInt256("-0")
    assert len(agentKey(OBJECTS["AgentRef"])) == 84
    assert agentKey({**OBJECTS["AgentRef"], "agentId": "2"}) < agentKey(
        {**OBJECTS["AgentRef"], "agentId": "10"}
    )
    assert taskKey(OBJECTS["TaskRef"])[2] == 1
    for fn, value in [
        (agentKey, {**OBJECTS["AgentRef"], "chainId": "0"}),
        (taskKey, {**OBJECTS["TaskRef"], "taskId": "0"}),
        (addressBytes, address(0)),
        (addressBytes, "0xABC"),
    ]:
        with pytest.raises(ProtocolViolation):
            fn(value)


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "file:///a",
        "https://u:p@x/a",
        "https://x/a#f",
        "https://x:0/a",
        "https://x:99999/a",
        "https://[x/a",
        "https://x/ a",
        "https://x/" + "é" * 1024,
        "https://x/\ud800",
    ],
)
def testInvalidUris(uri):
    assert not isContentUri(uri)


def testSchemaFailures():
    for value in [None, 1.0, {1: "x"}, {"x": (1,)}, {"x": "\ud800"}]:
        with pytest.raises(ProtocolViolation):
            validateRecord(value, "TaskRef", SCHEMA)
    for field, value in [
        ("unknown", 1),
        ("taskId", "01"),
        ("taskId", str(2**64)),
        ("market", address(1).upper()),
    ]:
        with pytest.raises(ProtocolViolation):
            validateRecord({**OBJECTS["TaskRef"], field: value}, "TaskRef", SCHEMA)
    for value in (True, 1.0, 2):
        with pytest.raises(ProtocolViolation):
            validateRecord({**OBJECTS["TaskSpec"], "schemaVersion": value}, "TaskSpec", SCHEMA)
    task = example("TaskSpec")
    del task["parentRef"]
    with pytest.raises(ProtocolViolation):
        validateRecord(task, "TaskSpec", SCHEMA)
    with pytest.raises(ProtocolViolation):
        validateRecord({}, "Unknown", SCHEMA)
    with pytest.raises(ValueError, match="local"):
        validateRecord({}, "Task", {"$defs": {"Task": {"$ref": "https://example.org/schema"}}})
    assert isHttpsUrl("https://example.org/a") and not isHttpsUrl("ipfs://abc")
    for amount in ("-1", str(2**96)):
        offer = example("submitBid")
        offer["input"]["offer"]["bidAtoms"] = amount
        with pytest.raises(ProtocolViolation, match="INVALID_RANGE"):
            validateRecord(offer, "Command", SCHEMA)
    with pytest.raises(ProtocolViolation, match="INVALID_RANGE"):
        validateRecord({**OBJECTS["TaskSpec"], "depth": 3}, "TaskSpec", SCHEMA)
