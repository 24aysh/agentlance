"""Layer 5 offline acceptance plus the unchanged lower-layer gates."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer5"
REQUIREMENTS = {
    "L5-01": ["testEconomicWorkerChainHistoryRestart"],
    "L5-02": ["testPricingAndPolicyGoldens", "testExplicitConversionAndOneCeil"],
    "L5-03": ["testFusionAndSummaryGoldens"],
    "L5-04": ["testFusionAndSummaryGoldens", "testHistoryCorrectionsCensoringAndEvaluation"],
    "L5-05": ["testFallbackAndCensoredHistory", "testJevWireAndBoundedFailure"],
    "L5-06": ["testTailBoundaries", "testMassAndQuantileProperties"],
    "L5-07": ["testForecastCrashRetainsChargeAndIntent", "testDurableBudgetCrashAndClock"],
    "L5-08": ["testPricingAndPolicyGoldens", "testFixtureReputationAndCandidateReplay"],
    "L5-09": ["testHistoryCorrectionsCensoringAndEvaluation"],
    "L5-10": [
        "testHistoryCorrectionsCensoringAndEvaluation",
        "testEconomicWorkerChainHistoryRestart",
    ],
}


def run(command, name):
    path = SCRATCH / name
    print(f"L5 check: {' '.join(command)} → {path.relative_to(ROOT)}", flush=True)
    with path.open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    report = {
        "layer": "L5",
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "liveGate": "provider billing/runtime/FX and Monad qualification remain explicit",
    }
    try:
        run([sys.executable, "scripts/check_layer4.py"], "lower-layers.log")
        tests = [
            "modules/economics",
            "modules/adapters/test_jev.py",
            "modules/adapters/storage/test_economics_storage.py",
        ]
        tests += sorted(
            str(p.relative_to(ROOT)) for p in (ROOT / "tests/integration").glob("test_layer5_*.py")
        )
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
            any(c.find(tag) is not None for tag in ("failure", "error", "skipped")) for c in cases
        ):
            raise ValueError("L5 acceptance requires all selected tests without skips")
        names = {case.attrib["name"].split("[")[0] for case in cases}
        for requirement, required in REQUIREMENTS.items():
            if not set(required) <= names:
                raise ValueError(f"Missing executed evidence for {requirement}")
        report.update(requirements=REQUIREMENTS, tests=len(cases))
        run([sys.executable, "scripts/demo_layer5.py"], "demo.log")
        run([sys.executable, "-m", "ruff", "check", "."], "lint.log")
        run([sys.executable, "-m", "ruff", "format", "--check", "."], "format.log")
        run(["git", "diff", "--check"], "diff.log")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (SCRATCH / "gate.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"L5 offline acceptance passed: {SCRATCH / 'gate.json'}")


if __name__ == "__main__":
    main()
