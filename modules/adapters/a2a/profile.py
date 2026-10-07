"""Strict AgentLance metadata at the protobuf/JSON boundary."""

import json
from copy import deepcopy

from google.protobuf.json_format import MessageToDict, ParseDict

from modules.agent_client.ports import AdapterError, ensure, recordCheck
from modules.domain.records import ProtocolViolation, parseJson

URN = "urn:agentlance:a2a:1"
DIAGNOSTIC = URN + ":diagnostic"
HEADERS = {"A2A-Version": "1.0", "A2A-Extensions": URN}
TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_CANCELED",
}


class ProfileError(AdapterError):
    def __init__(self, reason, taskStatus=None, receipt=None):
        self.diagnostic = {"reason": reason, "taskStatus": taskStatus, "receipt": receipt}
        super().__init__("CONFLICT", reason)


def jsonBytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def strictJson(raw):
    try:
        return parseJson(raw)
    except (ProtocolViolation, RecursionError) as error:
        raise AdapterError("INVALID_DATA", getattr(error, "code", "JSON_DEPTH")) from error


def restoreIntegers(value):
    """SDK Struct represents JSON integers as doubles; only use on SDK-owned output."""
    if isinstance(value, dict):
        return {key: restoreIntegers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [restoreIntegers(item) for item in value]
    if isinstance(value, float) and value.is_integer() and abs(value) <= 2**53 - 1:
        return int(value)
    return value


def wireDict(message):
    return restoreIntegers(MessageToDict(message))


def toProto(value, model):
    return ParseDict(value, model())


def readExtension(message, schema):
    ensure(isinstance(message, dict), "Message object required")
    ensure(isinstance(message.get("extensions"), list), "Extensions array required")
    ensure(isinstance(message.get("metadata"), dict), "Metadata object required")
    if URN not in message.get("extensions", []):
        raise ProfileError("PROFILE_VERSION")
    extension = message.get("metadata", {}).get(URN)
    ensure(len(jsonBytes(extension)) <= 16384, "Extension limit")
    recordCheck(extension, "A2AExtension", schema)
    return deepcopy(extension)


def checkHint(message, schema):
    extension = readExtension(message, schema)
    ensure(message.get("role") == "ROLE_USER", "Award hints require user role")
    ensure(
        isinstance(message.get("messageId"), str) and 0 < len(message["messageId"]) <= 128,
        "Message ID",
    )
    ensure(bool(message.get("parts")), "Message parts required")
    if extension["result"] is not None:
        raise ProfileError("PROFILE_CONFLICT")
    if message.get("contextId") and not message.get("taskId"):
        raise ProfileError("PROFILE_CONFLICT")
    return extension


def awardHint(view, messageId):
    """Build a transport hint from an already observed award, never from task text."""
    task, bid = view["task"], view["winningBid"]
    ensure(bid is not None and view["allocation"] is not None, "No observed winner")
    return {
        "messageId": messageId,
        "role": "ROLE_USER",
        "parts": [{"text": "Observe the canonical AgentLance award."}],
        "extensions": [URN],
        "metadata": {
            URN: {
                "schemaVersion": 1,
                "profileVersion": 1,
                "executionRef": {
                    "taskRef": task["taskRef"],
                    "awardId": view["allocation"]["awardId"],
                },
                "agentRef": bid["offer"]["agentRef"],
                "inputDigest": task["terms"]["input"]["digest"],
                "validationPolicyDigest": task["terms"]["validationPolicy"]["digest"],
                "result": None,
            }
        },
    }
