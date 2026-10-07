"""Offline L0–L3 acceptance gate; live deployment and exact-parity exceptions stay explicit."""

import json
import platform
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from layer3_deployment import buildEvidence
from layer3_tools import toolPath, verifyExecutionProfile, verifyToolchain

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / ".scratch/layer3"
sys.path.insert(0, str(ROOT))

from modules.domain.records import parseJson  # noqa: E402


def checkMappings(data, pytestReport, forgeReport):
    if set(data["requirements"]) != {f"L3-{number:02}" for number in range(1, 10)}:
        raise ValueError("Missing L3 requirement")
    pytestIds = []
    for case in pytestReport.iter("testcase"):
        name = case.attrib["classname"] + "." + case.attrib["name"]
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise ValueError(f"Unpassed required L3 pytest case: {name}")
        pytestIds.append(name)
    forgeIds = []
    for suiteName, suite in forgeReport.items():
        for name, result in suite["test_results"].items():
            fullName = suiteName.rsplit(":", 1)[-1] + "." + name
            if result["status"] != "Success":
                raise ValueError(f"Unpassed required L3 Forge case: {fullName}")
            forgeIds.append(fullName)
    if not pytestIds or not forgeIds:
        raise ValueError("Empty L3 acceptance report")
    for names in (pytestIds, forgeIds):
        duplicates = [name for name, count in Counter(names).items() if count != 1]
        if duplicates:
            raise ValueError(f"Duplicate executed acceptance cases: {duplicates}")
    mapped = {"pytest": set(), "forge": set()}
    for requirement, groups in data["requirements"].items():
        if set(groups) != {"pytest", "forge"} or not any(groups.values()):
            raise ValueError(f"Missing/invalid evidence mapping: {requirement}")
        for kind, ids in groups.items():
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate mapping within {requirement}/{kind}")
            passed = set(pytestIds if kind == "pytest" else forgeIds)
            if not set(ids) <= passed:
                raise ValueError(
                    f"Missing executed {requirement} {kind} cases: {set(ids) - passed}"
                )
            mapped[kind].update(ids)
    unmappedPytest = sorted(set(pytestIds) - mapped["pytest"])
    unmappedForge = sorted(set(forgeIds) - mapped["forge"])
    if unmappedPytest or unmappedForge:
        raise ValueError(
            f"Executed tests missing requirement mapping: {unmappedPytest + unmappedForge}"
        )
    return {
        "pytest": sorted(pytestIds),
        "forge": sorted(forgeIds),
        "unmappedPytest": unmappedPytest,
        "unmappedForge": unmappedForge,
    }


def checkFixtureMappings(data, tests):
    coverage = data["fixtureCoverage"]
    required = {"market.json", "reputation.json", "lifecycle.json", "delegation.json"}
    if not required <= set(coverage):
        raise ValueError(f"Missing golden fixture mappings: {required - set(coverage)}")
    totals = {}
    for name, cases in coverage.items():
        path = ROOT / "specs/fixtures" / name
        if Path(name).name != name or path.suffix != ".json" or not path.is_file():
            raise ValueError(f"Unknown golden fixture: {name}")
        fixture = parseJson(path.read_bytes())
        ids = [
            case["id"]
            for collection in (
                "cases",
                "rawCases",
                "positive",
                "negative",
                "contentHashes",
                "objects",
            )
            for case in fixture.get(collection, [])
        ]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"Empty/duplicate golden fixture IDs: {name}")
        if set(cases) != set(ids):
            raise ValueError(
                f"Golden fixture mapping differs for {name}: "
                f"missing={set(ids) - set(cases)}, extra={set(cases) - set(ids)}"
            )
        for caseId, links in cases.items():
            if not links or not set(links) <= {"pytest", "forge"} or not any(links.values()):
                raise ValueError(f"Missing/invalid fixture evidence: {name}/{caseId}")
            for kind, names in links.items():
                if not isinstance(names, list) or len(names) != len(set(names)):
                    raise ValueError(f"Invalid/duplicate fixture links: {name}/{caseId}/{kind}")
                missing = set(names) - set(tests[kind])
                if missing:
                    raise ValueError(f"Unexecuted fixture evidence {name}/{caseId}: {missing}")
        totals[name] = len(ids)
    return totals


def checkBytecode(artifact):
    runtime = artifact["deployedBytecode"]["object"].removeprefix("0x")
    creation = artifact["bytecode"]["object"].removeprefix("0x")
    if not runtime or not creation:
        raise ValueError("Empty market bytecode")
    sizes = {
        "runtimeBytes": len(bytes.fromhex(runtime)),
        "initcodeBytes": len(bytes.fromhex(creation)) + 64,
    }
    # Native Monad limits, independently checked against the actual execution profile at deployment.
    if sizes["runtimeBytes"] > 128 * 1024 or sizes["initcodeBytes"] > 256 * 1024:
        raise ValueError(f"Market exceeds qualified Monad code limits: {sizes}")
    return sizes | {"limitsSource": "https://docs.monad.xyz/developer-essentials/differences"}


def readCoverage(path):
    records = {}
    current = None
    for line in path.read_text().splitlines():
        if line.startswith("SF:"):
            source = line[3:]
            if source.startswith(str(ROOT) + "/"):
                source = source[len(str(ROOT)) + 1 :]
            current = {"uncoveredLines": [], "uncoveredBranches": []}
            records[source] = current
        elif current is not None:
            key, _, value = line.partition(":")
            if key in {"LF", "LH", "BRF", "BRH", "FNF", "FNH"}:
                current[key] = int(value)
            elif key == "DA" and int(value.split(",")[1]) == 0:
                current["uncoveredLines"].append(int(value.split(",")[0]))
            elif key == "BRDA" and value.split(",")[-1] in {"0", "-"}:
                current["uncoveredBranches"].append(value)
    expected = {
        str(file.relative_to(ROOT))
        for file in (ROOT / "contracts/src").rglob("*.sol")
        if file.name
        not in {
            "ProtocolTypes.sol",
            "IAgentLanceMarket.sol",
            "IAgentLanceViews.sol",
            "IIdentityRegistry.sol",
        }
    }
    production = {name: record for name, record in records.items() if name in expected}
    if set(production) != expected or not any(
        record.get("LF", 0) for record in production.values()
    ):
        raise ValueError(f"Incomplete production coverage sources: {expected - set(production)}")
    return production


def run(command, logName=None):
    print("Running: " + " ".join(map(str, command)), flush=True)
    if logName:
        with (SCRATCH / logName).open("w") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            print((SCRATCH / logName).read_text()[-20000:])
            result.check_returncode()
    else:
        subprocess.run(command, cwd=ROOT, check=True)


def main():
    SCRATCH.mkdir(parents=True, exist_ok=True)
    reportPath = SCRATCH / "gate.json"
    report = {
        "status": "running",
        "startedAt": datetime.now(UTC).isoformat(),
        "pythonVersion": platform.python_version(),
        "unicodeVersion": unicodedata.unidata_version,
        "liveGate": "pending; no deployment readiness implied",
    }
    reportPath.write_text(json.dumps(report, indent=2) + "\n")
    try:
        # Fail before lengthy checks, while replacing any stale previous success report.
        report["toolchain"] = verifyToolchain()
        report["executionProfile"] = verifyExecutionProfile()
        run([sys.executable, "scripts/check_layer2.py"], "lower-layers.log")
        run([sys.executable, "scripts/generate_contract_types.py", "--check"])
        forge = toolPath("forge")
        run([forge, "fmt", "--check"])
        run([forge, "build", "--locked", "--offline"], "build.log")
        artifactPath = SCRATCH / "out/AgentLanceMarket.sol/AgentLanceMarket.json"
        artifact = json.loads(artifactPath.read_text())
        report["bytecode"] = checkBytecode(artifact)
        report["build"] = buildEvidence(artifact)
        run([sys.executable, "scripts/check_abi.py", "--artifact", str(artifactPath)])
        run(
            [
                forge,
                "test",
                "--offline",
                "--gas-report",
                "-vv",
                "--json-file",
                str(SCRATCH / "forge-tests.json"),
            ],
            "forge-tests.log",
        )
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/conformance/test_layer3.py",
                "tests/conformance/test_layer3_blocks.py",
                "tests/conformance/test_layer3_uri.py",
                "tests/conformance/test_layer3_replay.py",
                "tests/integration/test_layer3_transactions.py",
                "tests/integration/test_layer3_live_driver.py",
                "tests/test_layer3_testnet.py",
                "tests/test_layer3_uri_fixtures.py",
                "-q",
                "--junitxml=" + str(SCRATCH / "pytest.xml"),
            ],
            "pytest.log",
        )
        mapping = parseJson((ROOT / "specs/acceptance-layer-3.json").read_bytes())
        report["tests"] = checkMappings(
            mapping,
            ET.parse(SCRATCH / "pytest.xml").getroot(),
            json.loads((SCRATCH / "forge-tests.json").read_text()),
        )
        report["fixtureCoverage"] = checkFixtureMappings(mapping, report["tests"])
        report["compatibilityExceptions"] = mapping["compatibilityExceptions"]
        report["liveGate"] = mapping["liveGate"]
        report["coverageProfile"] = {
            "compilerMode": "ir-minimum instrumentation",
            "excludedTests": ["MarketBoundsTest.testMaximumWireTransactionGasLimits()"],
            "reason": (
                "Instrumentation changes gas costs; the optimized test suite enforces native "
                "transaction gas bounds. The same maximum-record scenario remains in coverage "
                "through testMaximumWireRecordsAndContractSignature."
            ),
        }
        run(
            [
                forge,
                "coverage",
                "--offline",
                "--ir-minimum",
                "--no-match-test",
                r"^testMaximumWireTransactionGasLimits\(\)$",
                "--report",
                "lcov",
                "--report-file",
                str(SCRATCH / "coverage.lcov"),
            ],
            "coverage.log",
        )
        report["solidityCoverage"] = readCoverage(SCRATCH / "coverage.lcov")
        report["unreachableDefensiveBranches"] = mapping.get("unreachableDefensiveBranches", [])
        for command in (
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "ruff", "format", "--check", "."],
            ["git", "diff", "--check"],
        ):
            run(command)
        # Coverage has its own instrumentation profile; restore the qualified deployable artifacts.
        run([forge, "build", "--locked", "--offline"], "build-restored.log")
        report["status"] = "passed"
        report["strictErrorParity"] = "pending explicit nonpayable-boundary acceptance"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        reportPath.write_text(json.dumps(report, indent=2) + "\n")
    print(f"L3 offline checks passed; exceptions/live gate remain explicit in {reportPath}")


if __name__ == "__main__":
    main()
