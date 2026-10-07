"""Full offline Layer 4 gate, including real Envio/GraphQL and all lower-layer checks."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer4"
REQUIREMENTS = {
    "L4-01": ["testRegistryStaticSignatureTransferAndOutage", "testDirectDiscoveredWorkerRestart"],
    "L4-02": ["testDirectReadSubmitReplayRestart", "testFundedChildAndLateTerminalParentRefresh"],
    "L4-03": ["testCheapEligibility", "testInvalidLocalPolicy"],
    "L4-04": [
        "testSubmissionCrashReconcilesSameIntent",
        "testObservationCrashReplaysWithoutDuplicateBid",
        "testFinalityFaultsDoNotBecomeLatest",
        "testOrphanedUnfinalizedTransactionResendsIdenticalBytes",
        "testLayer2JournalMigrationKeepsClaims",
        "testConcurrentIntentUnknownRevertAndFailedReceipt",
    ],
    "L4-05": [
        "testEnvioGraphqlReplayPaginationOutage",
        "testWatcherDuplicatesAdaptiveRangesAndBackpressure",
    ],
    "L4-06": ["testBoundedContentPreservesExistingBytes", "testDirectDiscoveredWorkerRestart"],
    "L4-07": [
        "testNotificationIsOptionalAndIdempotent",
        "testLostConcurrentHintsKeepMessageIdentity",
        "testNotificationMissingPinnedCardDoesNotRedirect",
        "testOwnWatcherAdmissionAndHintsShareClaim",
    ],
}


def run(command, name):
    path = SCRATCH / name
    print(f"L4 check: {' '.join(command)} → {path.relative_to(ROOT)}", flush=True)
    with path.open("w") as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    reportPath = SCRATCH / "gate.json"
    report = {
        "layer": "L4",
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "liveGate": "pending actual deployment/registry qualification and bootstrap decision",
    }
    reportPath.write_text(json.dumps(report, indent=2) + "\n")
    try:
        run(["docker", "info", "--format", "{{.ServerVersion}}"], "docker.log")
        run([sys.executable, "scripts/check_layer3.py"], "lower-layers.log")
        run(["pnpm", "--dir", "apps/service", "check"], "envio.log")
        tests = sorted(
            str(path.relative_to(ROOT))
            for path in (ROOT / "tests/integration").glob("test_layer4_*.py")
        )
        tests += [
            "modules/agent_client/test_discovery.py",
            "modules/adapters/a2a/test_notifications.py",
            "modules/adapters/storage/test_journal_migration.py",
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
            any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for case in cases
        ):
            raise ValueError("L4 acceptance requires every selected test to pass without skips")
        names = {case.attrib["name"].split("[")[0] for case in cases}
        for requirement, required in REQUIREMENTS.items():
            if not set(required) <= names:
                raise ValueError(f"Missing executed acceptance evidence for {requirement}")
        report.update(requirements=REQUIREMENTS, tests=len(cases))
        run([sys.executable, "scripts/demo_layer4.py"], "demo.log")
        run([sys.executable, "-m", "ruff", "check", "."], "lint.log")
        run([sys.executable, "-m", "ruff", "format", "--check", "."], "format.log")
        run(["git", "diff", "--check"], "diff.log")
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        reportPath.write_text(json.dumps(report, indent=2) + "\n")
    print(f"L4 offline gate passed; live qualification remains separate: {reportPath}")


if __name__ == "__main__":
    main()
