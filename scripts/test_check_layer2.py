"""Mapping gate must reject stale plans and omitted or skipped evidence."""

import json
import unittest
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

from check_layer2 import checkMappings


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path("specs/fixtures/layer-2/cases.json").read_text())
        self.report = ET.Element("testsuite")
        names = {name for group in self.data["groups"].values() for name in group}
        names.update(self.data["replayFixtures"].values())
        names.update(f"testReviewedCases[{case['id']}]" for case in self.data["cases"])
        for name in names:
            ET.SubElement(self.report, "testcase", name=name)

    def testValid(self):
        checkMappings(self.data, self.report)

    def testMissingEvidence(self):
        for mutation in ("requirement", "group", "case", "test", "replay", "skipped"):
            with self.subTest(mutation=mutation):
                data = deepcopy(self.data)
                report = deepcopy(self.report)
                if mutation == "requirement":
                    data["requirements"].pop("L2-01")
                if mutation == "group":
                    data["groups"].pop("E")
                if mutation == "case":
                    data["cases"][0]["id"] = "missing"
                if mutation == "test":
                    data["groups"]["E"].append("notRun")
                if mutation == "replay":
                    data["replayFixtures"]["nonexistent"] = "testTwoProcessDemo"
                if mutation == "skipped":
                    for case in report:
                        if case.attrib["name"] == "testTwoProcessDemo":
                            ET.SubElement(case, "skipped")
                with self.assertRaises(ValueError):
                    checkMappings(data, report)
