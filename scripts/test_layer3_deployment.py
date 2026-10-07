"""Deployment reports cannot fabricate missing L7 facts or bypass canonical wire constraints."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import layer3_deployment
from eth_utils import keccak
from jsonschema import ValidationError
from layer3_deployment import buildEvidence, validateDeploymentReport


class DeploymentReportTests(unittest.TestCase):
    def setUp(self):
        examples = json.loads(Path("specs/fixtures/objects.json").read_text())
        self.manifest = next(
            entry["value"] for entry in examples["objects"] if entry["type"] == "DeploymentManifest"
        )
        schema = json.loads(Path("specs/schemas/layer-3-verification.schema.json").read_text())
        facts = {
            key: self.manifest[key] for key in schema["properties"]["manifestFacts"]["properties"]
        }
        facts["rpcUrls"] = ["https://rpc.example.invalid"]
        self.manifest["rpcUrls"] = facts["rpcUrls"]
        digest = "0x" + "12" * 32
        content = {"uri": "ipfs://synthetic-evidence", "digest": digest}
        self.report = {
            "reportVersion": 1,
            "layer": "L3",
            "manifestFacts": facts,
            "genesisHash": digest,
            "deploymentTxHash": digest,
            "executionRevision": "monad:MonadTen",
            "build": {
                "sourceCommit": "a" * 40,
                "sourceTreeDigest": digest,
                "solcVersion": "0.8.30",
                "foundryVersion": "1.8.0",
                "openZeppelinRevision": "test-version",
                "forgeStdRevision": "test-version",
                "evmVersion": "paris",
                "optimizerRuns": 200,
                "viaIR": True,
                "creationCodeHash": digest,
                "storageLayoutDigest": digest,
            },
            "identityVerification": content,
            "rpcVerification": self.manifest["rpcVerification"],
            "validatorVerification": self.manifest["validatorVerification"],
            "scenarioEvidence": content,
            "pendingManifestFields": schema["properties"]["pendingManifestFields"]["enum"][0],
        }

    def testClosedReportPreservesCanonicalTypes(self):
        self.assertEqual(validateDeploymentReport(self.report), self.report)
        self.report["manifestFacts"]["chainId"] = 10143
        with self.assertRaises((ValueError, ValidationError)):
            validateDeploymentReport(self.report)

    def testRejectUnknownMissingAndUnqualifiedFields(self):
        for mutation in (
            "unknown",
            "emptyRpc",
            "pending",
            "badHash",
            "badContent",
            "floatVersion",
            "wrongBuild",
            "mainnet",
        ):
            with self.subTest(mutation=mutation):
                report = deepcopy(self.report)
                if mutation == "unknown":
                    report["privateKey"] = "forbidden-shape"
                elif mutation == "emptyRpc":
                    report["manifestFacts"]["rpcUrls"] = []
                elif mutation == "pending":
                    report["pendingManifestFields"].pop()
                elif mutation == "badHash":
                    report["genesisHash"] = "0x1234"
                elif mutation == "badContent":
                    report["identityVerification"]["uri"] = "file:///tmp/report"
                elif mutation == "floatVersion":
                    report["reportVersion"] = True
                elif mutation == "wrongBuild":
                    report["build"]["viaIR"] = False
                else:
                    report["manifestFacts"]["network"] = "monad-mainnet"
                with self.assertRaises((ValueError, ValidationError)):
                    validateDeploymentReport(report)

    def testClearedPendingFieldsRequireMatchingCompleteManifest(self):
        self.report["pendingManifestFields"] = []
        with self.assertRaisesRegex(ValueError, "complete canonical manifest"):
            validateDeploymentReport(self.report)
        validateDeploymentReport(self.report, self.manifest)
        changed = deepcopy(self.manifest)
        changed["market"] = "0x" + "34" * 20
        with self.assertRaisesRegex(ValueError, "differs"):
            validateDeploymentReport(self.report, changed)


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "contracts/src/AgentLanceMarket.sol"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("pragma solidity 0.8.30; contract AgentLanceMarket {}")
        lock = Path("contracts/toolchain.lock.json").read_text()
        (self.root / "contracts/toolchain.lock.json").write_text(lock)
        self.artifact = {
            "metadata": {
                "compiler": {"version": "0.8.30+commit.73712a01"},
                "settings": {
                    "optimizer": {"enabled": True, "runs": 200},
                    "evmVersion": "paris",
                    "viaIR": True,
                    "libraries": {},
                    "compilationTarget": {"contracts/src/AgentLanceMarket.sol": "AgentLanceMarket"},
                },
                "sources": {
                    "contracts/src/AgentLanceMarket.sol": {
                        "keccak256": "0x" + keccak(self.source.read_bytes()).hex(),
                    }
                },
            },
            "bytecode": {"object": "0x6000"},
            "storageLayout": {"storage": [], "types": {}},
        }

    def evidence(self, artifact):
        with (
            patch.object(layer3_deployment, "ROOT", self.root),
            patch.object(layer3_deployment.subprocess, "run") as revision,
        ):
            revision.return_value.stdout = "a" * 40
            return buildEvidence(artifact)

    def testBuildFactsRequireObservedMetadataAndCurrentSources(self):
        evidence = self.evidence(self.artifact)
        self.assertEqual(evidence["solcVersion"], "0.8.30+commit.73712a01")
        self.assertEqual(evidence["optimizerRuns"], 200)
        for mutation in (
            "compiler",
            "evm",
            "runs",
            "optimizerDisabled",
            "optimizerWrongType",
            "conflictingMetadata",
            "optimizerDetails",
            "viaIR",
            "linkedLibrary",
            "target",
            "sourceHash",
            "staleSource",
            "sourcePath",
            "omittedSource",
        ):
            with self.subTest(mutation=mutation):
                artifact = deepcopy(self.artifact)
                settings = artifact["metadata"]["settings"]
                if mutation == "compiler":
                    artifact["metadata"]["compiler"]["version"] = "0.8.31"
                elif mutation == "evm":
                    settings["evmVersion"] = "cancun"
                elif mutation == "runs":
                    settings["optimizer"]["runs"] = 1
                elif mutation == "optimizerDisabled":
                    settings["optimizer"]["enabled"] = False
                elif mutation == "optimizerWrongType":
                    settings["optimizer"]["enabled"] = 1
                elif mutation == "conflictingMetadata":
                    artifact["rawMetadata"] = "{}"
                elif mutation == "optimizerDetails":
                    settings["optimizer"]["details"] = {"peephole": False}
                elif mutation == "viaIR":
                    settings["viaIR"] = False
                elif mutation == "linkedLibrary":
                    settings["libraries"] = {"unqualified.sol": {"Library": "0x" + "11" * 20}}
                elif mutation == "target":
                    settings["compilationTarget"] = {
                        "contracts/test/MarketHarness.sol": "MarketHarness"
                    }
                elif mutation == "sourceHash":
                    artifact["metadata"]["sources"]["contracts/src/AgentLanceMarket.sol"][
                        "keccak256"
                    ] = "0x" + "00" * 32
                elif mutation == "staleSource":
                    self.source.write_text("pragma solidity 0.8.30; contract Changed {}")
                elif mutation == "sourcePath":
                    artifact["metadata"]["sources"]["../external.sol"] = {
                        "keccak256": "0x" + "00" * 32
                    }
                else:
                    artifact["metadata"]["sources"] = {}
                with self.assertRaises(ValueError):
                    self.evidence(artifact)
                self.source.write_text("pragma solidity 0.8.30; contract AgentLanceMarket {}")


if __name__ == "__main__":
    unittest.main()
