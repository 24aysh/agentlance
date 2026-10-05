import copy
import unittest

from check_layer1 import checkCoverage


class CoverageGateChecks(unittest.TestCase):
    def testExactCoverageGate(self):
        report = {
            "files": {"core.py": {"summary": {"missing_lines": 0, "missing_branches": 0}}},
            "totals": {"num_statements": 10000},
        }
        checkCoverage(report, {"core.py"})
        for field in ("missing_lines", "missing_branches"):
            incomplete = copy.deepcopy(report)
            incomplete["files"]["core.py"]["summary"][field] = 1
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                checkCoverage(incomplete, {"core.py"})
        with self.assertRaisesRegex(ValueError, "every"):
            checkCoverage(report, {"core.py", "unmeasured.py"})
        report["totals"]["num_statements"] = 0
        with self.assertRaisesRegex(ValueError, "Empty"):
            checkCoverage(report, {"core.py"})
