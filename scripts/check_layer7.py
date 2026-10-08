"""Full inherited gate plus L7 boundaries; required tests must execute without skips."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from layer3_tools import toolPath

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer7"
REQUIREMENTS = {
    "L7-01": [
        "testGoldenValidation",
        "testStrictJson",
        "testTypeSensitiveEquality",
        "testExactSemanticLimitsAndUnicode",
        "testEvaluatorOperationalLimitsProduceNoEvidence",
        "testEvaluatorLargerFrameAndIsolation",
    ],
    "L7-02": [
        "testProductionValidatorKuboSettlementAndExport",
        "testVerdictBuilderMatchesFrozenVectorAndLocatorSemantics",
        "testValidatorUnavailableExpiresWithoutInventedFailure",
        "testEvaluatorBindingCannotAuthorizeAnotherResult",
    ],
    "L7-03": [
        "testValidatorRestartAtDurableBoundaries",
        "testValidationMigrationClosedRecordsAndRetention",
        "testReceiptConsumerCheckpointIsAtomicAndConflictsHalt",
        "testReceiptUsageOrderingCorrectionsAndMissingCosts",
        "testLateChildSettlementRefreshesFailedParentReconciliation",
    ],
    "L7-04": [
        "testProductionValidatorKuboSettlementAndExport",
        "testLostBroadcastAndPublisherQueueRecovery",
    ],
    "L7-05": [
        "testGoldenValidation",
        "testKuboImmutablePublicationRestartAndReadback",
        "testProductionValidatorKuboSettlementAndExport",
    ],
    "L7-06": [
        "testLostBroadcastAndPublisherQueueRecovery",
        "testValidatorRestartAtDurableBoundaries",
    ],
    "L7-07": [
        "testIndependentValidatorAndWorkersWithReconciliation",
        "testReceiptUsageOrderingCorrectionsAndMissingCosts",
        "testLateChildSettlementRefreshesFailedParentReconciliation",
    ],
}


def run(command, logName):
    print("L7 check: " + " ".join(command) + " → " + logName, flush=True)
    with (SCRATCH / logName).open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    report = {
        "layer": "L7",
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "liveQualification": "pending",
        "paidModelCalls": 0,
    }
    try:
        run([sys.executable, "scripts/layer7_tools.py"], "setup.log")
        run([sys.executable, "scripts/check_layer6.py"], "lower-layers.log")
        forge = toolPath("forge")
        run(
            [
                forge,
                "test",
                "--offline",
                "--match-contract",
                "FeedbackPublisherTest",
                "--json-file",
                str(SCRATCH / "forge.json"),
            ],
            "forge.log",
        )
        suites = json.loads((SCRATCH / "forge.json").read_bytes())
        solidity = [
            name
            for suite in suites.values()
            for name, result in suite["test_results"].items()
            if result["status"] == "Success"
        ]
        if (
            len(solidity) != sum(len(s["test_results"]) for s in suites.values())
            or len(solidity) < 6
        ):
            raise ValueError("Publisher security/real-registry tests missing or failed")
        for source, target in [
            ("FeedbackPublisher", "feedback-publisher"),
            ("ReputationRegistryUpgradeable", "registry.reputation"),
        ]:
            artifact = json.loads(
                (ROOT / f".scratch/layer3/out/{source}.sol/{source}.json").read_bytes()
            )
            if artifact["abi"] != json.loads((ROOT / f"specs/{target}.abi.json").read_bytes()):
                raise ValueError("Frozen L7 ABI changed: " + source)
        tests = [
            "modules/validation",
            *sorted(
                str(p.relative_to(ROOT))
                for p in (ROOT / "tests/integration").glob("test_layer7_*.py")
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
            any(c.find(x) is not None for x in ("error", "failure", "skipped")) for c in cases
        ):
            raise ValueError("All L7 tests must execute without skips")
        names = {case.attrib["name"].split("[")[0] for case in cases}
        for key, required in REQUIREMENTS.items():
            if not set(required) <= names:
                raise ValueError("Missing requirement evidence: " + key)
        report.update(requirements=REQUIREMENTS, solidityTests=solidity, pythonTests=len(cases))
        run([sys.executable, "scripts/demo_layer7.py"], "demo.log")
        run([sys.executable, "-m", "ruff", "check", "."], "lint.log")
        run([sys.executable, "-m", "ruff", "format", "--check", "."], "format.log")
        run([forge, "fmt", "--check"], "solidity-format.log")
        run(["git", "diff", "--check"], "diff.log")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        (SCRATCH / "gate.json").write_text(json.dumps(report, indent=2) + "\n")
    print("L7 offline acceptance passed: " + str(SCRATCH / "gate.json"))


if __name__ == "__main__":
    main()
