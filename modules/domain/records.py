"""In-memory wire validation; the supplied JSON Schema remains the public model."""

import json
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker, ValidationError, validators


class ProtocolViolation(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProtocolViolation(code)


def unsigned(value: int, bits: int) -> int:
    require(type(value) is int and 0 <= value < 2**bits, "INVALID_RANGE")
    return value


def isUnsigned(value, bits):
    return (
        isinstance(value, str)
        and len(value) <= 78
        and re.fullmatch(r"0|[1-9][0-9]*", value) is not None
        and int(value) < 2**bits
    )


def decodeUnsigned(value: str, bits: int) -> int:
    require(
        isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value) is not None,
        "INVALID_ENCODING",
    )
    require(len(value) <= 78, "INVALID_RANGE")
    return unsigned(int(value), bits)


formatChecker = FormatChecker()


@formatChecker.checks("uint64")
def isUint64(value):
    return isUnsigned(value, 64)


@formatChecker.checks("uint96")
def isUint96(value):
    return isUnsigned(value, 96)


@formatChecker.checks("uint256")
def isUint256(value):
    return isUnsigned(value, 256)


@formatChecker.checks("int256")
def isInt256(value):
    return (
        isinstance(value, str)
        and len(value) <= 78
        and re.fullmatch(r"0|-?[1-9][0-9]*", value) is not None
        and -(2**255) <= int(value) < 2**255
    )


@formatChecker.checks("content-uri")
def isContentUri(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return (
            len(value.encode("utf-8")) <= 2048
            and parts.scheme in {"https", "ipfs"}
            and bool(parts.hostname)
            and (parts.port is None or 0 < parts.port <= 65535)
            and not parts.username
            and not parts.password
            and not parts.fragment
            and not any(char.isspace() for char in value)
        )
    except (ValueError, UnicodeError):
        return False


@formatChecker.checks("https-url")
def isHttpsUrl(value):
    return isContentUri(value) and value.startswith("https://")


def rejectDuplicateKeys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def rejectNonIntegerNumber(value):
    raise ValueError(f"Invalid JSON number: {value}")


def parseJson(rawBytes: bytes):
    try:
        return json.loads(
            rawBytes.decode("utf-8"),
            object_pairs_hook=rejectDuplicateKeys,
            parse_constant=rejectNonIntegerNumber,
            parse_float=rejectNonIntegerNumber,
        )
    except (ValueError, UnicodeError) as error:
        raise ProtocolViolation("INVALID_ENCODING") from error


def checkJson(value, *, schema=False):
    if type(value) is dict:
        for key, child in value.items():
            require(type(key) is str, "INVALID_ENCODING")
            if schema and key == "$ref" and not child.startswith("#/"):
                raise ValueError("Only local schema references are supported")
            checkJson(child, schema=schema)
    elif type(value) is list:
        for child in value:
            checkJson(child, schema=schema)
    else:
        require(type(value) in (str, int, bool, type(None)), "INVALID_ENCODING")
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as error:
                raise ProtocolViolation("INVALID_ENCODING") from error


def strictConst(validator, expected, value, schema):
    if type(expected) is int and type(value) is not int:
        yield ValidationError("Integer constant requires an integer token")
    else:
        yield from Draft202012Validator.VALIDATORS["const"](validator, expected, value, schema)


RecordValidator = validators.extend(
    Draft202012Validator,
    {"const": strictConst},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)


def rangeError(error) -> bool:
    if error.context:
        return any(rangeError(child) for child in error.context)
    if error.validator in ("minimum", "maximum"):
        return True
    return (
        error.validator == "format"
        and error.validator_value in ("uint64", "uint96", "uint256", "int256")
        and isinstance(error.instance, str)
        and re.fullmatch(r"0|-?[1-9][0-9]*", error.instance) is not None
    )


def validateRecord(record: dict, typeName: str, schema: dict) -> None:
    require(typeName in schema["$defs"], "INVALID_ENCODING")
    checkJson(schema, schema=True)
    checkJson(record)
    selected = {"$defs": schema["$defs"], "$ref": f"#/$defs/{typeName}"}
    errors = list(RecordValidator(selected, format_checker=formatChecker).iter_errors(record))
    if errors:
        code = "INVALID_RANGE" if any(rangeError(error) for error in errors) else "INVALID_ENCODING"
        raise ProtocolViolation(code)


def decodeRecord(rawBytes: bytes, typeName: str, schema: dict) -> dict:
    record = parseJson(rawBytes)
    validateRecord(record, typeName, schema)
    return record


def addressBytes(address: str) -> bytes:
    require(
        isinstance(address, str) and re.fullmatch(r"0x[0-9a-f]{40}", address) is not None,
        "INVALID_ENCODING",
    )
    require(int(address[2:], 16) != 0, "INVALID_RANGE")
    return bytes.fromhex(address[2:])


def agentKey(agentRef: dict) -> bytes:
    chain = decodeUnsigned(agentRef["chainId"], 256)
    require(chain > 0, "INVALID_RANGE")
    return (
        chain.to_bytes(32)
        + addressBytes(agentRef["identityRegistry"])
        + decodeUnsigned(agentRef["agentId"], 256).to_bytes(32)
    )


def taskKey(taskRef: dict) -> tuple:
    chain = decodeUnsigned(taskRef["chainId"], 256)
    taskId = decodeUnsigned(taskRef["taskId"], 64)
    require(chain > 0 and taskId > 0, "INVALID_RANGE")
    return chain, addressBytes(taskRef["market"]), taskId
