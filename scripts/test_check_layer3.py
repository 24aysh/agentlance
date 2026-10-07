"""L3 gates reject omitted, skipped, duplicated and altered public-contract evidence."""

import json
import unittest
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from check_abi import verifyCompiledAbi
from check_layer3 import checkBytecode, checkFixtureMappings, checkMappings, parseJson, readCoverage


class Layer3GateTests(unittest.TestCase):
    def setUp(self):
        self.mapping = {
            "requirements": {
                f"L3-{number:02}": {
                    "pytest": ["tests.case.testCase[boundary]"],
                    "forge": ["Market.testBoundary()"],
                }
                for number in range(1, 10)
            }
        }
        self.pytestReport = ET.fromstring(
            '<testsuite><testcase classname="tests.case" name="testCase[boundary]"/></testsuite>'
        )
        self.forgeReport = {
            "test/Market.t.sol:Market": {"test_results": {"testBoundary()": {"status": "Success"}}}
        }

    def testExactExecutedParameterIds(self):
        result = checkMappings(self.mapping, self.pytestReport, self.forgeReport)
        self.assertEqual(result["pytest"], ["tests.case.testCase[boundary]"])
        self.assertFalse(result["unmappedForge"])

    def testRejectOmittedFailedAndSkippedEvidence(self):
        for mutation in (
            "requirement",
            "parameter",
            "skipped",
            "duplicate",
            "forgeFailure",
            "missingForge",
            "duplicateMapping",
            "unmappedCollected",
        ):
            with self.subTest(mutation=mutation):
                mapping, pytest, forge = deepcopy(
                    (self.mapping, self.pytestReport, self.forgeReport)
                )
                if mutation == "requirement":
                    mapping["requirements"].pop("L3-01")
                elif mutation == "parameter":
                    pytest[0].attrib["name"] = "testCase[other]"
                elif mutation == "skipped":
                    ET.SubElement(pytest[0], "skipped")
                elif mutation == "duplicate":
                    pytest.append(deepcopy(pytest[0]))
                elif mutation == "forgeFailure":
                    forge["test/Market.t.sol:Market"]["test_results"]["testBoundary()"][
                        "status"
                    ] = "Failure"
                elif mutation == "missingForge":
                    forge.clear()
                elif mutation == "duplicateMapping":
                    mapping["requirements"]["L3-01"]["forge"] *= 2
                elif mutation == "unmappedCollected":
                    ET.SubElement(pytest, "testcase", classname="tests.case", name="testUnmapped")
                with self.assertRaises(ValueError):
                    checkMappings(mapping, pytest, forge)

    def testEveryGoldenCaseRequiresExactExecutedEvidence(self):
        data = parseJson(Path("specs/acceptance-layer-3.json").read_bytes())
        tests = {
            kind: sorted(
                {
                    name
                    for cases in data["fixtureCoverage"].values()
                    for links in cases.values()
                    for name in links.get(kind, [])
                }
            )
            for kind in ("pytest", "forge")
        }
        totals = checkFixtureMappings(data, tests)
        self.assertEqual(totals["market.json"], 18)
        self.assertEqual(totals["lifecycle.json"], 34)
        for mutation in (
            "omittedCase",
            "extraCase",
            "wrongParameter",
            "duplicateLink",
            "emptyLinks",
        ):
            changed = deepcopy(data)
            cases = changed["fixtureCoverage"]["market.json"]
            first = next(iter(cases))
            if mutation == "omittedCase":
                cases.pop(first)
            elif mutation == "extraCase":
                cases["invented-golden"] = deepcopy(cases[first])
            elif mutation == "wrongParameter":
                cases[first]["pytest"] = [
                    "tests.conformance.test_layer3.testMarketGoldens[missing]"
                ]
            elif mutation == "duplicateLink":
                cases[first]["pytest"] *= 2
            else:
                cases[first] = {"pytest": [], "forge": []}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                checkFixtureMappings(changed, tests)

    def testDuplicateAcceptanceCaseKeysAreRejectedBeforeMapping(self):
        with self.assertRaises(ValueError):
            parseJson(b'{"fixtureCoverage":{"market.json":{"normal":{},"normal":{}}}}')

    def testCompiledAbiPreservesNamesWidthsAndMutability(self):
        protocol = json.loads(Path("specs/protocol.abi.json").read_text())
        views = json.loads(Path("specs/contracts.read.abi.json").read_text())
        constructor = {
            "type": "constructor",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "identityRegistry_", "type": "address"},
                {"name": "validator_", "type": "address"},
            ],
        }
        compiled = deepcopy([*protocol, *views, constructor])
        verifyCompiledAbi(list(reversed(compiled)), protocol, views)
        for mutation in (
            "mutability",
            "width",
            "name",
            "duplicate",
            "extraWrite",
            "missingConstructor",
        ):
            changed = deepcopy(compiled)
            if mutation == "mutability":
                changed[0]["stateMutability"] = "nonpayable"
            elif mutation == "width":
                changed[0]["inputs"][0]["components"][0]["type"] = "uint256"
            elif mutation == "name":
                changed[0]["inputs"][0]["name"] = "wrong"
            elif mutation == "duplicate":
                changed.append(deepcopy(changed[0]))
            elif mutation == "missingConstructor":
                changed.pop()
            else:
                changed.append(
                    {
                        "type": "function",
                        "name": "setOwner",
                        "stateMutability": "nonpayable",
                        "inputs": [],
                        "outputs": [],
                    }
                )
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                verifyCompiledAbi(changed, protocol, views)

    def testToolFailureReplacesStaleSuccessfulGateReport(self):
        import tempfile

        import check_layer3

        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            report = scratch / "gate.json"
            report.write_text('{"status":"passed"}')
            with (
                patch.object(check_layer3, "SCRATCH", scratch),
                patch.object(
                    check_layer3, "verifyToolchain", side_effect=RuntimeError("missing pinned tool")
                ),
                self.assertRaisesRegex(RuntimeError, "missing pinned tool"),
            ):
                check_layer3.main()
            result = json.loads(report.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], "missing pinned tool")
            self.assertIn("startedAt", result)

    def testQualifiedCodeLimits(self):
        artifact = {"bytecode": {"object": "0x6000"}, "deployedBytecode": {"object": "0x6000"}}
        self.assertEqual(checkBytecode(artifact)["initcodeBytes"], 66)
        artifact["deployedBytecode"]["object"] = "00" * (128 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "code limits"):
            checkBytecode(artifact)

    def testCoverageCannotOmitProduction(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage.lcov"
            path.write_text("SF:contracts/test/Market.t.sol\nLF:10\nLH:10\nend_of_record\n")
            with self.assertRaisesRegex(ValueError, "production coverage"):
                readCoverage(path)


if __name__ == "__main__":
    unittest.main()
