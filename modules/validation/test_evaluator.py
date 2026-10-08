import json
from copy import deepcopy
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modules.domain.records import ProtocolViolation
from modules.validation.evaluator import (
    checkStructure,
    deepEqual,
    digestBytes,
    evaluateArtifact,
    evidenceBytes,
    resolvePointer,
)

ROOT = Path(__file__).resolve().parents[2]
OBJECTS = {
    item["type"]: item["value"]
    for item in json.loads((ROOT / "specs/fixtures/objects.json").read_bytes())["objects"]
}


def evaluation(
    result=b'{"answer":42}', *, inputValue=None, shape=None, predicates=None, policyChange=None
):
    task, commitment = deepcopy(OBJECTS["TaskSpec"]), deepcopy(OBJECTS["ResultCommitment"])
    shape = deepcopy(shape or OBJECTS["OutputShape"])
    policy = deepcopy(OBJECTS["ValidationPolicy"])
    inputRaw = evidenceBytes(inputValue if inputValue is not None else {"expected": 42})
    shapeRaw = evidenceBytes(shape)
    policy["outputSchemaDigest"] = digestBytes(shapeRaw)
    if predicates is not None:
        policy["predicates"] = predicates
    policy.update(policyChange or {})
    policyRaw = evidenceBytes(policy)
    for name, raw in [
        ("input", inputRaw),
        ("outputSchema", shapeRaw),
        ("validationPolicy", policyRaw),
    ]:
        task["terms"][name]["digest"] = digestBytes(raw)
    commitment["artifact"]["digest"] = digestBytes(result)
    return task, commitment, inputRaw, shapeRaw, policyRaw, result


@pytest.mark.parametrize(
    "case",
    json.loads((ROOT / "specs/fixtures/validation.json").read_bytes())["cases"],
    ids=lambda c: c["id"],
)
def testGoldenValidation(case):
    initial = case["initial"]
    raw = initial.get("resultBytes", "").encode() or evidenceBytes(
        initial.get("result", {"answer": 42})
    )
    if initial.get("resultBytesLength"):
        raw = b" " * initial["resultBytesLength"]
    predicates = None
    if case["action"].get("outputPointer"):
        predicates = [
            {"op": "equalsInput", "outputPointer": "/missing", "inputPointer": "/expected"}
        ]
    args = list(evaluation(raw, inputValue=initial.get("input"), predicates=predicates))
    if case["expected"]["verdict"] is None:
        args[4 if initial.get("policyDigestMatches") is False else 5] = b""
        with pytest.raises(ProtocolViolation):
            evaluateArtifact(*args)
    else:
        evidence = evaluateArtifact(*args)
        assert {k: evidence[k] for k in ("verdict", "reason", "failedPredicate")} == case[
            "expected"
        ]
        assert evidenceBytes(evidence) == evidenceBytes(evaluateArtifact(*args))


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b'{"answer":NaN}',
        b'{"answer":1e0}',
        b'{"answer":"\\ud800"}',
        b'{"\\ud800":42}',
        b'{"answer":42,}',
        b"\xff",
    ],
)
def testStrictJson(raw):
    assert evaluateArtifact(*evaluation(raw))["reason"] == "RESULT_INVALID_JSON"


@pytest.mark.parametrize("pointer", ["x", "/~", "/~2"])
def testMalformedPolicyIsNotVerdict(pointer):
    with pytest.raises(ProtocolViolation):
        evaluateArtifact(
            *evaluation(
                predicates=[{"op": "equalsInput", "outputPointer": pointer, "inputPointer": ""}]
            )
        )


def testShapePrerequisitesAndOrderedFailures():
    for shape in [
        OBJECTS["OutputShape"] | {"required": []},
        {"type": "integer", "minimum": 2, "maximum": 1},
    ]:
        with pytest.raises(ProtocolViolation):
            evaluateArtifact(*evaluation(shape=shape))
    predicates = [
        {"op": "equalsLiteral", "outputPointer": "/answer", "literal": x} for x in [42, True, 40]
    ]
    evidence = evaluateArtifact(*evaluation(predicates=predicates))
    assert evidence["failedPredicate"] == 1
    assert (
        evaluateArtifact(*evaluation(b'{"answer":true}', predicates=predicates))["reason"]
        == "SCHEMA_MISMATCH"
    )


def testExactSemanticLimitsAndUnicode():
    assert (
        evaluateArtifact(*evaluation(b'{"answer":42}' + b" " * (1048576 - 13)))["verdict"] == "PASS"
    )
    assert evaluateArtifact(*evaluation(b" " * 1048577))["reason"] == "RESULT_LIMIT"
    with pytest.raises(ProtocolViolation):
        evaluateArtifact(*evaluation(b" " * 2097153))
    checkStructure([0] * 9999)
    with pytest.raises(ProtocolViolation):
        checkStructure([0] * 10000)
    nested = 0
    for _ in range(15):
        nested = [nested]
    checkStructure(nested)
    with pytest.raises(ProtocolViolation):
        checkStructure([nested])
    assert evaluateArtifact(*evaluation(evidenceBytes([nested])))["reason"] == "RESULT_LIMIT"
    assert (
        evaluateArtifact(
            *evaluation(
                evidenceBytes("😀"),
                shape={"type": "string", "maxLength": 1},
                predicates=[{"op": "equalsLiteral", "outputPointer": "", "literal": "😀"}],
            )
        )["verdict"]
        == "PASS"
    )


def testPointersAndTypedEquality():
    value = {"a/b": {"~": [True, 1]}}
    assert resolvePointer(value, "/a~1b/~0/0") is True
    assert resolvePointer(value, "") == value
    for pointer in ["/a~1b/~0/00", "/a~1b/~0/-", "/a~1b/~0/-1", "/missing"]:
        assert not deepEqual(resolvePointer(value, pointer), None)
    assert not deepEqual(value, {"a/b": {"~": [1, 1]}})
    assert deepEqual({"a": 1, "b": [False]}, {"b": [False], "a": 1})
    assert not deepEqual([1, 2], [2, 1])


@given(st.integers(-100, 100), st.booleans())
def testTypeSensitiveEquality(integer, boolean):
    assert not deepEqual({"x": [integer]}, {"x": [boolean]})


def testLargeExactIntegerIsShapeMismatchNotInvalidJson():
    assert (
        evaluateArtifact(*evaluation(b'{"answer":' + b"9" * 5000 + b"}"))["reason"]
        == "SCHEMA_MISMATCH"
    )
