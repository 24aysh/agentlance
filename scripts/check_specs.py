"""Validate L0 fixture syntax and ranges; auction behavior belongs to L1."""

import subprocess
import sys
from pathlib import Path

from check_abi import verifyAbi
from check_signatures import verifySignatures
from jsonschema import Draft202012Validator

repoRoot = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repoRoot))

from modules.domain.records import formatChecker, parseJson  # noqa: E402


def readJson(path):
    return parseJson(path.read_bytes())


def validateMarketVectors(document, schema):
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=formatChecker).validate(document)
    seen = set()
    for case in document["cases"]:
        if case["id"] in seen:
            raise ValueError(f"Duplicate fixture ID: {case['id']}")
        seen.add(case["id"])
    return len(seen)


def validateObject(document, typeName, schema):
    if typeName not in schema["$defs"]:
        raise ValueError(f"Unknown protocol type: {typeName}")
    selected = {"$defs": schema["$defs"], "$ref": f"#/$defs/{typeName}"}
    Draft202012Validator(selected, format_checker=formatChecker).validate(document)


def checkUniqueIds(records, label):
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate ID in {label}")
    return set(ids)


def validateArtifacts():
    schema = readJson(repoRoot / "specs/schemas/protocol.schema.json")
    Draft202012Validator.check_schema(schema)
    verifyAbi(readJson(repoRoot / "specs/protocol.abi.json"), schema)
    objects = readJson(repoRoot / "specs/fixtures/objects.json")
    if (
        set(objects) != {"schemaVersion", "synthetic", "objects"}
        or objects["synthetic"] is not True
    ):
        raise ValueError("Object examples must be an explicitly synthetic bundle")
    idsByFile = {"objects.json": checkUniqueIds(objects["objects"], "objects")}
    for record in objects["objects"]:
        if set(record) != {"id", "type", "value"}:
            raise ValueError("Unexpected object-example fields")
        validateObject(record["value"], record["type"], schema)
    requiredTypes = {
        "AgentRef",
        "TaskRef",
        "AgentProfile",
        "TaskSpec",
        "Bid",
        "Allocation",
        "ExecutionRef",
        "ResultCommitment",
        "ValidationRecord",
        "SettlementReceipt",
        "ReputationEvidence",
        "CostEstimate",
        "CostReport",
        "DeploymentManifest",
        "ValidationPolicy",
        "A2AExtension",
    }
    if not requiredTypes <= {r["type"] for r in objects["objects"]}:
        raise ValueError("Missing canonical object example")
    catalog = readJson(repoRoot / "specs/catalog.json")
    for kind, field, key in [("Command", "command", "commands"), ("Event", "name", "events")]:
        examples = {r["value"][field] for r in objects["objects"] if r["type"] == kind}
        if examples != set(catalog[key]):
            raise ValueError(f"Missing {kind} example")
    scenarioSchema = readJson(repoRoot / "specs/schemas/scenarios.schema.json")
    Draft202012Validator.check_schema(scenarioSchema)
    caseCount = 0
    for domain in ["lifecycle", "reputation", "delegation", "replay", "validation", "layer-1"]:
        filename = f"{domain}.json"
        data = readJson(repoRoot / "specs/fixtures" / filename)
        Draft202012Validator(scenarioSchema).validate(data)
        idsByFile[filename] = checkUniqueIds(data["cases"], domain)
        caseCount += len(data["cases"])
        for case in data["cases"]:
            if case["error"] is not None and case["events"]:
                raise ValueError(f"{case['id']}: rejected action emits events")
    market = readJson(repoRoot / "specs/fixtures/market.json")
    idsByFile["market.json"] = checkUniqueIds(market["cases"], "market")
    vectors = readJson(repoRoot / "specs/fixtures/signatures.json")
    if vectors["schemaVersion"] != 1 or vectors["synthetic"] is not True:
        raise ValueError("Signing vectors must be explicitly synthetic v1")
    idsByFile["signatures.json"] = checkUniqueIds(
        vectors["positive"] + vectors["negative"] + vectors["contentHashes"], "signatures"
    )
    verifySignatures(vectors, readJson(repoRoot / "specs/signing/types.json"))
    acceptance = readJson(repoRoot / "specs/acceptance.json")["requirements"]
    if set(acceptance) != {f"L0-0{i}" for i in range(1, 7)}:
        raise ValueError("Incomplete L0 requirement map")
    for references in acceptance.values():
        for reference in references:
            filename, caseId = reference.split("#")
            if caseId not in idsByFile.get(filename, set()):
                raise ValueError(f"Unresolved acceptance case: {reference}")
    a2a = readJson(repoRoot / "specs/fixtures/a2a.json")
    extension = "urn:agentlance:a2a:1"
    validateObject(a2a["sendMessage"]["message"]["metadata"][extension], "A2AExtension", schema)
    for artifact in a2a["taskResponse"]["task"]["artifacts"]:
        validateObject(artifact["metadata"][extension], "A2AExtension", schema)
    subprocess.run(["node", str(repoRoot / "scripts/check_signatures.mjs")], check=True)
    print(f"Validated {len(objects['objects'])} protocol examples and {caseCount} scenario cases.")


def main():
    schema = readJson(repoRoot / "specs/schemas/market-vectors.schema.json")
    document = readJson(repoRoot / "specs/fixtures/market.json")
    count = validateMarketVectors(document, schema)
    validateArtifacts()
    print(f"Validated {count} market vectors; L0 checks do not execute protocol transitions.")


if __name__ == "__main__":
    main()
