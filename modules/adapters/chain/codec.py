"""Canonical schema/ABI conversion shared by chain transport and conformance drivers."""

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from modules.agent_client.ports import AdapterError, ensure, recordCheck

ZERO = "0x" + "00" * 20


def schemaAbiField(name, schema, definitions):
    if "allOf" in schema and "type" not in schema:
        return schemaAbiField(name, schema["allOf"][0], definitions)
    if "$ref" in schema:
        typeName = schema["$ref"].split("/")[-1]
        scalars = {
            "Address": "address",
            "Digest": "bytes32",
            "Signature": "bytes",
            "Uri": "string",
            "Uint64": "uint64",
            "Uint96": "uint96",
            "Uint256": "uint256",
            "Int256": "int256",
        }
        if typeName in scalars:
            return {"name": name, "type": scalars[typeName]}
        return schemaAbiField(name, definitions[typeName], definitions)
    if "anyOf" in schema:
        return {
            "name": name,
            "type": "tuple",
            "components": [
                {"name": "present", "type": "bool"},
                schemaAbiField("value", schema["anyOf"][0], definitions),
            ],
        }
    if schema.get("type") == "object":
        return {
            "name": name,
            "type": "tuple",
            "components": [
                schemaAbiField(key, value, definitions)
                for key, value in schema["properties"].items()
            ],
        }
    if schema.get("type") == "array":
        item = schemaAbiField(name, schema["items"], definitions)
        item["type"] += "[]"
        return item
    if "enum" in schema:
        return {"name": name, "type": "uint8"}
    if "const" in schema:
        value = schema["const"]
        typeName = (
            "bool" if isinstance(value, bool) else "uint8" if isinstance(value, int) else "string"
        )
        return {"name": name, "type": typeName}
    return {"name": name, "type": "uint32" if schema.get("type") == "integer" else schema["type"]}


def abiType(field):
    if field["type"].startswith("tuple"):
        return "(" + ",".join(abiType(v) for v in field["components"]) + ")" + field["type"][5:]
    return field["type"]


def zeroAbi(field):
    kind = field["type"]
    if kind == "tuple":
        return tuple(zeroAbi(v) for v in field["components"])
    if kind.endswith("[]"):
        return []
    if kind == "address":
        return ZERO
    if kind == "string":
        return ""
    if kind.startswith("bytes"):
        return bytes(int(kind[5:])) if kind != "bytes" else b""
    return False if kind == "bool" else 0


class WireCodec:
    def __init__(self, schema, catalog, abi):
        self.schema = schema
        self.definitions = self.schema["$defs"]
        self.catalog = catalog
        self.abi = abi
        self.commands = {
            v["properties"]["command"]["const"]: v["properties"]["input"]
            for v in self.definitions["Command"]["oneOf"]
        }
        self.eventSchemas = {
            v["properties"]["name"]["const"]: {
                "type": "object",
                "properties": {
                    "schemaVersion": v["properties"]["schemaVersion"],
                    "policyVersion": v["properties"]["policyVersion"],
                    **v["properties"]["payload"]["properties"],
                },
            }
            for v in self.definitions["Event"]["oneOf"]
        }
        self.enums = {}
        for name in (
            "states",
            "terminalReasons",
            "verdicts",
            "counterEffects",
            "allocationOutcomes",
        ):
            values = self.catalog[name]
            self.enums[frozenset(values)] = values

    def resolve(self, schema):
        while "$ref" in schema or ("allOf" in schema and "type" not in schema):
            schema = (
                self.definitions[schema["$ref"].split("/")[-1]]
                if "$ref" in schema
                else schema["allOf"][0]
            )
        return schema

    def field(self, schema):
        return schemaAbiField("", schema, self.definitions)

    def convert(self, schema, value, *, decoding=False):
        original = schema
        schema = self.resolve(schema)
        if "anyOf" in schema:
            child = schema["anyOf"][0]
            if decoding:
                present, contents = value
                if not present:
                    ensure(contents == zeroAbi(self.field(child)), "Dirty absent ABI payload")
                    return None
                return self.convert(child, contents, decoding=True)
            return (
                (False, zeroAbi(self.field(child)))
                if value is None
                else (True, self.convert(child, value))
            )
        if schema.get("type") == "object":
            fields = schema["properties"]
            if decoding:
                return {
                    k: self.convert(s, v, decoding=True)
                    for (k, s), v in zip(fields.items(), value, strict=True)
                }
            return tuple(self.convert(s, value[k]) for k, s in fields.items())
        if "enum" in schema:
            mapping = self.enums[frozenset(schema["enum"])]
            return {v: k for k, v in mapping.items()}[value] if decoding else mapping[value]
        kind = self.field(original)["type"]
        if kind.startswith("bytes"):
            return "0x" + value.hex() if decoding else bytes.fromhex(value[2:])
        if kind == "address":
            return value.lower()
        if kind.startswith(("uint", "int")):
            return str(value) if decoding and schema.get("type") == "string" else int(value)
        return value

    def commandData(self, name, data):
        entry = next(v for v in self.abi if v.get("name") == name)
        signature = name + "(" + ",".join(abiType(v) for v in entry["inputs"]) + ")"
        args = self.convert(self.commands[name], data)
        return (
            "0x"
            + (
                keccak(text=signature)[:4] + encode([abiType(v) for v in entry["inputs"]], args)
            ).hex()
        )

    def event(self, log):
        ensure(
            isinstance(log, dict)
            and isinstance(log.get("topics"), list)
            and len(log["topics"]) == 1
            and isinstance(log["topics"][0], str),
            "Unexpected market event topics",
        )
        ensure(
            isinstance(log.get("data"), str)
            and log["data"].startswith("0x")
            and len(log["data"]) <= 2 * 1048576 + 2,
            "Market event byte limit/encoding",
        )
        for entry in self.abi:
            if entry["type"] != "event":
                continue
            signature = entry["name"] + "(" + abiType(entry["inputs"][0]) + ")"
            if log["topics"][0].lower() != "0x" + keccak(text=signature).hex():
                continue
            ensure(len(log["topics"]) == 1, "Unexpected indexed market fields")
            try:
                types = [abiType(entry["inputs"][0])]
                value = decode(types, bytes.fromhex(log["data"][2:]))
                ensure(
                    "0x" + encode(types, value).hex() == log["data"], "Noncanonical market event"
                )
                data = self.convert(self.eventSchemas[entry["name"]], value[0], decoding=True)
            except (ValueError, TypeError, KeyError, OverflowError, DecodingError) as error:
                raise AdapterError("INVALID_DATA", "Malformed market event") from error
            event = {
                "schemaVersion": data.pop("schemaVersion"),
                "policyVersion": data.pop("policyVersion"),
                "name": entry["name"],
                "payload": data,
            }
            recordCheck(event, "Event", self.schema)
            return event
        raise AdapterError("INVALID_DATA", "Unknown market event")


def callData(entry, values):
    types = [abiType(f) for f in entry["inputs"]]
    signature = entry["name"] + "(" + ",".join(types) + ")"
    return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()
