"""Validate L3 deployment report structure without asserting live qualification."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from eth_utils import keccak
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.domain.records import (  # noqa: E402
    RecordValidator,
    checkJson,
    formatChecker,
    parseJson,
    validateRecord,
)


def buildEvidence(artifact):
    """Hash the exact compiler source manifest and layout using canonical UTF-8 JSON.

    sourceTreeDigest covers every source in the market compilation, including imported
    dependencies, by path and compiler-recorded content hash. Tests/docs are not deployment code.
    Creation code excludes constructor arguments, which are pinned separately by manifestFacts.
    Callers verifyToolchain first; imported library bytes must also match this compiler manifest.
    """
    lock = parseJson((ROOT / "contracts/toolchain.lock.json").read_bytes())
    metadata = artifact.get("metadata") or json.loads(artifact["rawMetadata"])
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if "rawMetadata" in artifact:
        raw = json.loads(artifact["rawMetadata"])
        # Foundry normalizes ABI order/remapping syntax in metadata; build facts must agree.
        keys = ("evmVersion", "optimizer", "viaIR", "libraries", "compilationTarget")
        if any(raw.get(key) != metadata.get(key) for key in ("compiler", "sources")) or any(
            raw.get("settings", {}).get(key) != metadata.get("settings", {}).get(key)
            for key in keys
        ):
            raise ValueError("Artifact metadata representations disagree")
    settings = metadata["settings"]
    if metadata["compiler"]["version"] != lock["solc"]["version"]:
        raise ValueError("Artifact compiler version differs from the pinned compiler")
    if (
        settings.get("evmVersion") != lock["execution"]["evmVersion"]
        or settings.get("optimizer")
        != {"enabled": True, "runs": lock["execution"]["optimizerRuns"]}
        or settings["optimizer"]["enabled"] is not True
        or type(settings["optimizer"]["runs"]) is not int
        or settings.get("viaIR") is not True
        or settings.get("libraries") != {}
        or settings.get("compilationTarget")
        != {"contracts/src/AgentLanceMarket.sol": "AgentLanceMarket"}
    ):
        raise ValueError("Artifact compiler settings differ from the qualified market build")
    allowedRoots = [
        ROOT / "contracts/src",
        *(ROOT / ".scratch/layer3/toolchain/lib" / name for name in lock["libraries"]),
    ]
    sources = {name: entry["keccak256"] for name, entry in metadata["sources"].items()}
    if "contracts/src/AgentLanceMarket.sol" not in sources:
        raise ValueError("Artifact omits the market source")
    for name, digest in sources.items():
        source = (ROOT / name).resolve()
        if not any(source.is_relative_to(path.resolve()) for path in allowedRoots):
            raise ValueError(f"Artifact includes an unqualified source path: {name}")
        if not source.is_file() or "0x" + keccak(source.read_bytes()).hex() != digest:
            raise ValueError(f"Artifact source hash differs from the current pinned source: {name}")

    def canonicalDigest(value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        return "0x" + keccak(raw).hex()

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "sourceCommit": revision,
        "sourceTreeDigest": canonicalDigest(sources),
        "solcVersion": metadata["compiler"]["version"],
        "foundryVersion": lock["foundry"]["version"],
        "openZeppelinRevision": lock["libraries"]["openzeppelin-contracts"]["revision"],
        "forgeStdRevision": lock["libraries"]["forge-std"]["revision"],
        "evmVersion": settings["evmVersion"],
        "optimizerRuns": settings["optimizer"]["runs"],
        "viaIR": settings["viaIR"],
        "creationCodeHash": "0x"
        + keccak(bytes.fromhex(artifact["bytecode"]["object"].removeprefix("0x"))).hex(),
        "storageLayoutDigest": canonicalDigest(artifact["storageLayout"]),
    }


def validateDeploymentReport(report, completeManifest=None):
    """Check closed evidence shape; callers must independently verify every live fact."""
    protocol = parseJson((ROOT / "specs/schemas/protocol.schema.json").read_bytes())
    schema = parseJson((ROOT / "specs/schemas/layer-3-verification.schema.json").read_bytes())
    resources = Registry().with_resource(protocol["$id"], Resource.from_contents(protocol))
    checkJson(report)
    RecordValidator(schema, registry=resources, format_checker=formatChecker).validate(report)
    facts = report["manifestFacts"]
    if not facts["rpcUrls"]:
        raise ValueError("L3 qualification requires at least one tested RPC URL")
    if report["pendingManifestFields"] == []:
        if completeManifest is None:
            raise ValueError("A complete canonical manifest is required to clear pending L7 fields")
        validateRecord(completeManifest, "DeploymentManifest", protocol)
        if any(completeManifest[key] != value for key, value in facts.items()):
            raise ValueError("Full manifest differs from verified L3 facts")
        for name in ("rpcVerification", "validatorVerification"):
            if report[name] != completeManifest[name]:
                raise ValueError(f"Full manifest differs from L3 {name}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--complete-manifest", type=Path)
    args = parser.parse_args()
    manifest = parseJson(args.complete_manifest.read_bytes()) if args.complete_manifest else None
    validateDeploymentReport(parseJson(args.report.read_bytes()), manifest)
    print("L3 report structure valid; live facts and evidence require independent verification.")


if __name__ == "__main__":
    main()
