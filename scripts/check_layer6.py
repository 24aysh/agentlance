"""Full L6 gate, with explicit real Docker/SDK/local-EVM evidence and no skips."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer6"
REQUIREMENTS = {
    "L6-01": [
        "testManagedCrashRetainsClaimAndBudget",
        "testCompletedStepResumesWithoutRepeatingDocker",
    ],
    "L6-02": [
        "testPublicChildMarketsAndFallback",
        "testContainerPrivilegeFilesystemAndPidLimits",
        "testLiveSdkRequiresExplicitProviderAndKey",
        "testLiveSdkPinsEndpointAndDoesNotRetryProviderError",
    ],
    "L6-03": ["testAcceptedDockerAndActualSdk"],
    "L6-04": [
        "testCapacityIsEnforcedAtCommonSigningPath",
        "testContainerLimitsKillAndNoRedispatch",
        "testResourceExhaustionAndUsageDeliveryRetry",
        "testWallCancellationDoesNotCancelCoordinatorTick",
    ],
    "L6-05": [
        "testChildEnvelopeGoldenAndPlanBoundaries",
        "testEnvelopeConservation",
        "testConcurrentParentsReserveFreshDepositsAndGas",
    ],
    "L6-06": ["testPublicChildMarketsAndFallback"],
    "L6-07": ["testPublicChildMarketsAndFallback"],
    "L6-08": [
        "testSoloSdkEconomicExecutionAndEvaluation",
        "testNeverStartedAwardProducesExplicitZeroUsage",
    ],
    "L6-09": [
        "testManagedCrashRetainsClaimAndBudget",
        "testResourceExhaustionAndUsageDeliveryRetry",
    ],
}


def run(command, name):
    path = SCRATCH / name
    print(f"L6 check: {' '.join(command)} → {path.relative_to(ROOT)}", flush=True)
    with path.open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    report = {
        "layer": "L6",
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "liveQualification": "pending",
        "paidModelCalls": 0,
    }
    try:
        run([sys.executable, "scripts/layer6_image.py"], "image.log")
        run([sys.executable, "scripts/check_layer5.py"], "lower-layers.log")
        tests = [
            "modules/execution",
            *sorted(
                str(p.relative_to(ROOT))
                for p in (ROOT / "tests/integration").glob("test_layer6_*.py")
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
        cases = list(ET.parse(SCRATCH / "pytest.xml").getroot().iter("testcase"))
        if not cases or any(
            any(c.find(t) is not None for t in ("error", "failure", "skipped")) for c in cases
        ):
            raise ValueError("L6 requires all selected tests without skips")
        names = {case.attrib["name"].split("[")[0] for case in cases}
        for requirement, required in REQUIREMENTS.items():
            if not set(required) <= names:
                raise ValueError("Missing evidence: " + requirement)
        report.update(requirements=REQUIREMENTS, tests=len(cases))
        run([sys.executable, "scripts/demo_layer6.py"], "demo.log")
        run([sys.executable, "-m", "ruff", "check", "."], "lint.log")
        run([sys.executable, "-m", "ruff", "format", "--check", "."], "format.log")
        run(["git", "diff", "--check"], "diff.log")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (SCRATCH / "gate.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"L6 offline acceptance passed: {SCRATCH / 'gate.json'}")


if __name__ == "__main__":
    main()
