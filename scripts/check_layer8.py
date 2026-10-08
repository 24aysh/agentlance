"""Full inherited acceptance plus requester, disclosure and independent-agent boundaries."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer8"
REQUIREMENTS = {
    "L8-01": [
        "testRequesterRootCancelWithdrawRecovery",
        "testRequesterValidationAndAuctionPagination",
        "testRequesterLostSendAndWithdrawalBoundaries",
    ],
    "L8-02": [
        "testRequesterValidationAndAuctionPagination",
        "testIndependentAgentCanonicalSettlementAndWithdrawal",
    ],
    "L8-03": [
        "testRequesterValidationAndAuctionPagination",
        "testPublicMetadataFailureRetainsFrozenBid",
    ],
    "L8-04": ["testDisclosureCorrectionMissingZeroAndPrivacy"],
    "L8-05": [
        "testRequesterLostSendAndWithdrawalBoundaries",
        "testRequesterRootCancelWithdrawRecovery",
        "testRequesterValidationAndAuctionPagination",
    ],
    "L8-06": [
        "testIndependentExternalProfileConformance",
        "testIndependentAgentDiscoversExecutesAndRestarts",
    ],
    "L8-07": ["testIndependentAgentCanonicalSettlementAndWithdrawal"],
}


def run(command, name):
    print("L8 check: " + " ".join(command) + " → " + name, flush=True)
    with (SCRATCH / name).open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    report = {
        "layer": "L8",
        "scope": "non-frontend offline",
        "startedAt": datetime.now(UTC).isoformat(),
        "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "liveQualification": "pending",
        "frontend": "deferred",
        "paidModelCalls": 0,
    }
    try:
        run([sys.executable, "scripts/check_layer7.py"], "lower-layers.log")
        tests = [
            "modules/validation/test_disclosure.py",
            *sorted(
                str(p.relative_to(ROOT))
                for p in (ROOT / "tests/integration").glob("test_layer8_*.py")
            ),
        ]
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                *tests,
                "-q",
                "--junitxml=" + str(SCRATCH / "pytest.xml"),
            ],
            "pytest.log",
        )
        cases = ET.parse(SCRATCH / "pytest.xml").findall(".//testcase")
        if not cases or any(
            any(c.find(k) is not None for k in ("failure", "error", "skipped")) for c in cases
        ):
            raise ValueError("All required L8 tests must execute without skips")
        names = {case.attrib["name"].split("[")[0] for case in cases}
        for requirement, required in REQUIREMENTS.items():
            if not set(required) <= names:
                raise ValueError("Missing executed evidence for " + requirement)
        report.update(requirements=REQUIREMENTS, pythonTests=len(cases), externalHttpAssertions=24)
        for source in ("wire.mjs", "main.mjs", "test_wire.mjs"):
            run(["node", "--check", "examples/external-agent/" + source], source + ".log")
        run(["node", "--test", "examples/external-agent/test_wire.mjs"], "node-tests.log")
        run([sys.executable, "scripts/demo_layer8.py"], "demo.log")
        run([sys.executable, "-m", "ruff", "check", "."], "lint.log")
        run([sys.executable, "-m", "ruff", "format", "--check", "."], "format.log")
        run(["git", "diff", "--check"], "diff.log")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (SCRATCH / "gate.json").write_text(json.dumps(report, indent=2) + "\n")
    print("L8 non-frontend offline acceptance passed: " + str(SCRATCH / "gate.json"))


if __name__ == "__main__":
    main()
