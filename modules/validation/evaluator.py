"""The fixed structured-output-v1 predicate language. No I/O or signing authority."""

import json
import re
from pathlib import Path

from eth_hash.auto import keccak

from modules.domain.records import (
    ProtocolViolation,
    parseJson,
    rejectDuplicateKeys,
    rejectNonIntegerNumber,
    require,
    validateRecord,
)

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "specs/schemas/protocol.schema.json").read_bytes()
)
MISSING = object()


def digestBytes(raw):
    return "0x" + keccak(raw).hex()


def evidenceBytes(evidence):
    return json.dumps(evidence, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def parseArtifact(raw):
    try:
        return parseJson(raw)
    except ProtocolViolation:
        # Python's host digit cap is not a protocol integer bound. Parse long tokens
        # in small chunks; the isolated evaluator still has its CPU/memory budget.
        def exactInteger(token):
            negative = token.startswith("-")
            digits = token[1:] if negative else token
            value = 0
            for start in range(0, len(digits), 1000):
                chunk = digits[start : start + 1000]
                value = value * 10 ** len(chunk) + int(chunk)
            return -value if negative else value

        try:
            return json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=rejectDuplicateKeys,
                parse_constant=rejectNonIntegerNumber,
                parse_float=rejectNonIntegerNumber,
                parse_int=exactInteger,
            )
        except (ValueError, UnicodeError) as error:
            raise ProtocolViolation("INVALID_ENCODING") from error


def checkStructure(value, maxDepth=16, maxNodes=10000):
    # Root is depth one; object keys are not JSON value nodes.
    stack, count = [(value, 1)], 0
    while stack:
        child, depth = stack.pop()
        count += 1
        require(depth <= maxDepth and count <= maxNodes, "RESULT_LIMIT")
        if type(child) is dict:
            for key in child:
                key.encode("utf-8")
            stack.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            stack.extend((item, depth + 1) for item in child)
        elif type(child) is str:
            child.encode("utf-8")
        else:
            require(type(child) in (int, bool, type(None)), "INVALID_ENCODING")


def pointerTokens(pointer):
    require(pointer == "" or pointer.startswith("/"), "INVALID_POLICY")
    require(re.search(r"~(?![01])", pointer) is None, "INVALID_POLICY")
    return (
        []
        if pointer == ""
        else [p.replace("~1", "/").replace("~0", "~") for p in pointer[1:].split("/")]
    )


def resolvePointer(value, pointer):
    for token in pointerTokens(pointer):
        if type(value) is dict and token in value:
            value = value[token]
        elif (
            type(value) is list
            and re.fullmatch(r"0|[1-9][0-9]*", token)
            and len(token) < 10
            and int(token) < len(value)
        ):
            value = value[int(token)]
        else:
            return MISSING
    return value


def deepEqual(left, right):
    if left is MISSING or right is MISSING or type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(deepEqual(left[k], right[k]) for k in left)
    if type(left) is list:
        return len(left) == len(right) and all(
            deepEqual(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def checkShape(shape, depth=1):
    require(depth <= 16, "INVALID_SCHEMA")
    if shape["type"] == "integer":
        require(shape["minimum"] <= shape["maximum"], "INVALID_SCHEMA")
    elif shape["type"] == "object":
        require(set(shape["required"]) == set(shape["properties"]), "INVALID_SCHEMA")
        for child in shape["properties"].values():
            checkShape(child, depth + 1)
    elif shape["type"] == "array":
        checkShape(shape["items"], depth + 1)


def matchesShape(value, shape):
    # The vocabulary is already validated. Avoid constructing error messages from
    # attacker-controlled integers (JSON Schema's messages stringify those values).
    kind = shape["type"]
    expected = {"object": dict, "array": list, "integer": int, "boolean": bool, "string": str}
    if type(value) is not expected[kind]:
        return False
    if kind == "object":
        return value.keys() == shape["properties"].keys() and all(
            matchesShape(value[key], child) for key, child in shape["properties"].items()
        )
    if kind == "array":
        return len(value) <= shape["maxItems"] and all(
            matchesShape(child, shape["items"]) for child in value
        )
    if kind == "integer":
        return shape["minimum"] <= value <= shape["maximum"]
    if kind == "string":
        return len(value) <= shape["maxLength"]
    return True


def evidenceBinding(task, result):
    ref = task["taskRef"]
    return {
        "schemaVersion": 1,
        "executionRef": {
            "taskRef": {k: ref[k] for k in ("chainId", "market", "taskId")},
            "awardId": 1,
        },
        "inputDigest": task["terms"]["input"]["digest"],
        "resultDigest": result["artifact"]["digest"],
        "validationPolicyDigest": task["terms"]["validationPolicy"]["digest"],
        "outputSchemaDigest": task["terms"]["outputSchema"]["digest"],
        "evaluatorVersion": 1,
    }


def validatePrerequisites(terms, inputBytes, shapeBytes, policyBytes):
    for name, raw, maximum in zip(
        ("input", "outputSchema", "validationPolicy"),
        (inputBytes, shapeBytes, policyBytes),
        (1048576, 65536, 65536),
        strict=True,
    ):
        require(type(raw) is bytes and len(raw) <= maximum, "TRANSPORT_LIMIT")
        require(digestBytes(raw) == terms[name]["digest"], "CONTENT_DIGEST")
    inputValue, shape, policy = map(parseArtifact, (inputBytes, shapeBytes, policyBytes))
    checkStructure(inputValue)
    checkStructure(shape, 64)
    checkStructure(policy)
    validateRecord(shape, "OutputShape", SCHEMA)
    validateRecord(policy, "ValidationPolicy", SCHEMA)
    checkShape(shape)
    require(policy["outputSchemaDigest"] == terms["outputSchema"]["digest"], "POLICY_BINDING")
    for predicate in policy["predicates"]:
        pointerTokens(predicate["outputPointer"])
        if predicate["op"] == "equalsInput":
            pointerTokens(predicate["inputPointer"])

    return inputValue, shape, policy


def evaluateArtifact(task, result, inputBytes, shapeBytes, policyBytes, resultBytes):
    """Return canonical evidence; invalid prerequisites raise, never return FAIL."""
    validateRecord(task, "TaskSpec", SCHEMA)
    validateRecord(result, "ResultCommitment", SCHEMA)
    binding = evidenceBinding(task, result)
    require(result["executionRef"] == binding["executionRef"], "RESULT_BINDING")
    require(
        task["terms"]["taskFamily"] == "structured-output-v1"
        and task["terms"]["policyVersion"] == 1,
        "UNSUPPORTED",
    )
    for raw, field in (
        (inputBytes, "inputDigest"),
        (shapeBytes, "outputSchemaDigest"),
        (policyBytes, "validationPolicyDigest"),
        (resultBytes, "resultDigest"),
    ):
        require(type(raw) is bytes and digestBytes(raw) == binding[field], "CONTENT_DIGEST")
    require(
        len(inputBytes) <= 1048576
        and len(shapeBytes) <= 65536
        and len(policyBytes) <= 65536
        and len(resultBytes) <= 2097152,
        "TRANSPORT_LIMIT",
    )
    inputValue, shape, policy = validatePrerequisites(
        task["terms"], inputBytes, shapeBytes, policyBytes
    )

    def finish(reason, index=None):
        return binding | {
            "verdict": "PASS" if reason == "PASS" else "FAIL",
            "reason": reason,
            "failedPredicate": index,
        }

    if len(resultBytes) > 1048576:
        return finish("RESULT_LIMIT")
    try:
        output = parseArtifact(resultBytes)
        # Validate Unicode before limits, including object keys.
        checkStructure(output, 1000, 1048576)
    except (ProtocolViolation, UnicodeError):
        return finish("RESULT_INVALID_JSON")
    try:
        checkStructure(output)
    except ProtocolViolation:
        return finish("RESULT_LIMIT")
    if not matchesShape(output, shape):
        return finish("SCHEMA_MISMATCH")
    for index, predicate in enumerate(policy["predicates"]):
        expected = (
            resolvePointer(inputValue, predicate["inputPointer"])
            if predicate["op"] == "equalsInput"
            else predicate["literal"]
        )
        if not deepEqual(resolvePointer(output, predicate["outputPointer"]), expected):
            return finish("PREDICATE_FAILED", index)
    return finish("PASS")
