"""Digest-bound validator key control and exact compiled runtime qualification."""

import time
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import closed, ensure
from modules.agent_client.signing import contentDigest

ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_SOURCES = (
    "modules/validation/evaluator.py",
    "modules/domain/records.py",
    "apps/validator/worker.py",
    "apps/validator/Dockerfile",
    "specs/schemas/protocol.schema.json",
    "uv.lock",
)


def evaluatorSourceDigest():
    return contentDigest(
        jsonBytes({name: contentDigest((ROOT / name).read_bytes()) for name in EVALUATOR_SOURCES})
    )


def buildValidatorEvidence(account, facts, image, validUntil, now=None):
    now = int(time.time()) if now is None else now
    payload = {
        "version": 1,
        "purpose": "AgentLance validator key control; no transaction authority",
        "chainId": facts["chainId"],
        "market": facts["market"],
        "validator": facts["validator"],
        "image": image,
        "sourceDigest": evaluatorSourceDigest(),
        "createdAt": str(now),
        "validUntil": str(validUntil),
    }
    signature = (
        "0x" + account.sign_message(encode_defunct(primitive=jsonBytes(payload))).signature.hex()
    )
    return {"payload": payload, "signature": signature}


def verifyValidatorEvidence(evidence, facts, image, now=None):
    now = int(time.time()) if now is None else now
    closed(evidence, "payload signature")
    payload = evidence["payload"]
    closed(
        payload, "version purpose chainId market validator image sourceDigest createdAt validUntil"
    )
    ensure(
        payload["version"] == 1
        and payload["purpose"] == "AgentLance validator key control; no transaction authority"
        and all(payload[k] == facts[k] for k in ("chainId", "market", "validator"))
        and payload["image"] == image
        and payload["sourceDigest"] == evaluatorSourceDigest()
        and int(payload["createdAt"]) <= now < int(payload["validUntil"]),
        "Validator qualification binding/expiry",
    )
    ensure(
        Account.recover_message(
            encode_defunct(primitive=jsonBytes(payload)), signature=evidence["signature"]
        ).lower()
        == facts["validator"],
        "Validator key-control proof",
    )
    return payload


def verifyCompiledRuntime(rawCode, artifact):
    """Immutables are qualified by on-chain getters; every other byte must match."""
    metadata = artifact["metadata"]
    if isinstance(metadata, str):
        metadata = strictJson(metadata.encode())
    for name, data in metadata["sources"].items():
        path = (ROOT / name).resolve()
        ensure(
            path.is_relative_to(ROOT)
            and path.is_file()
            and contentDigest(path.read_bytes()) == data["keccak256"],
            "Compiled source mismatch",
        )
    expected = bytearray.fromhex(artifact["deployedBytecode"]["object"].removeprefix("0x"))
    actual = bytearray.fromhex(rawCode.removeprefix("0x"))
    ensure(len(actual) == len(expected) and len(actual) > 0, "Runtime code length")
    for offsets in artifact["deployedBytecode"].get("immutableReferences", {}).values():
        for offset in offsets:
            start, length = offset["start"], offset["length"]
            actual[start : start + length] = expected[start : start + length] = bytes(length)
    ensure(actual == expected, "Runtime differs from pinned compiled source")
