"""Check the frozen ABI against canonical schema fields, without contract implementation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.adapters.chain.codec import schemaAbiField  # noqa: E402


def verifyAbi(abi, schema):
    definitions = schema["$defs"]
    expected = []
    for variant in definitions["Command"]["oneOf"]:
        properties = variant["properties"]
        name = properties["command"]["const"]
        creation = name in {"createTask", "createChildTask"}
        expected.append(
            {
                "type": "function",
                "name": name,
                "stateMutability": "payable" if creation else "nonpayable",
                "inputs": [
                    schemaAbiField(k, v, definitions)
                    for k, v in properties["input"]["properties"].items()
                ],
                "outputs": [schemaAbiField("taskRef", {"$ref": "#/$defs/TaskRef"}, definitions)]
                if creation
                else [],
            }
        )
    for variant in definitions["Event"]["oneOf"]:
        properties = variant["properties"]
        payload = {
            "type": "object",
            "properties": {
                "schemaVersion": properties["schemaVersion"],
                "policyVersion": properties["policyVersion"],
                **properties["payload"]["properties"],
            },
        }
        field = schemaAbiField("data", payload, definitions)
        field["indexed"] = False
        expected.append(
            {
                "type": "event",
                "name": properties["name"]["const"],
                "anonymous": False,
                "inputs": [field],
            }
        )
    expected.append(
        {"type": "error", "name": "ProtocolError", "inputs": [{"name": "code", "type": "uint16"}]}
    )
    if abi != expected:
        raise ValueError("Frozen ABI differs from canonical schema fields")


def stripInternalTypes(value):
    """Ignore compiler-only Solidity type names, preserving every public ABI detail."""
    if isinstance(value, list):
        return [stripInternalTypes(item) for item in value]
    if isinstance(value, dict):
        return {
            key: stripInternalTypes(item) for key, item in value.items() if key != "internalType"
        }
    return value


def verifyCompiledAbi(compiled, protocol, views):
    expected = {(entry["type"], entry.get("name", "")): entry for entry in [*protocol, *views]}
    actual = {}
    constructors = 0
    for entry in stripInternalTypes(compiled):
        if entry["type"] == "constructor":
            constructors += 1
            if entry != {
                "type": "constructor",
                "inputs": [
                    {"name": "identityRegistry_", "type": "address"},
                    {"name": "validator_", "type": "address"},
                ],
                "stateMutability": "nonpayable",
            }:
                raise ValueError("Compiled constructor differs from qualified deployment interface")
            continue
        key = (entry["type"], entry.get("name", ""))
        if key in actual:
            raise ValueError(f"Duplicate/overloaded compiled ABI entry: {key}")
        actual[key] = entry
    if constructors != 1:
        raise ValueError("Compiled market must declare exactly one qualified constructor")
    if actual != expected:
        differences = sorted(
            key for key in actual.keys() | expected.keys() if actual.get(key) != expected.get(key)
        )
        raise ValueError(
            f"Compiled ABI differs from frozen commands/events or reviewed views: {differences}"
        )


def main():
    import argparse
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    args = parser.parse_args()
    protocol = json.loads((root / "specs/protocol.abi.json").read_text())
    verifyAbi(protocol, json.loads((root / "specs/schemas/protocol.schema.json").read_text()))
    if args.artifact:
        artifact = json.loads(args.artifact.read_text())
        views = json.loads((root / "specs/contracts.read.abi.json").read_text())
        verifyCompiledAbi(artifact["abi"], protocol, views)
    print("Canonical ABI verified" + (" against compiled market" if args.artifact else ""))


if __name__ == "__main__":
    main()
