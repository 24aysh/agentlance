"""Check the frozen ABI against canonical schema fields, without contract implementation."""


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
