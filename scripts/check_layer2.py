"""Full L0/L1/L2 acceptance, real process coverage and executable fixture mappings."""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ["modules/agent_client", "modules/adapters", "apps"]
TESTS = [
    *SOURCES,
    "tests/conformance",
    "tests/integration/test_layer2.py",
    "tests/test_layer2_fixtures.py",
]


def checkMappings(data, reports):
    if set(data["requirements"]) != {f"L2-{number:02}" for number in range(1, 8)}:
        raise ValueError("Missing L2 requirement")
    groups = data["groups"]
    if set(groups) != set("RHSIPWCJE"):
        raise ValueError("Missing acceptance group")
    for requirement, ids in data["requirements"].items():
        if not ids or not set(ids) <= set(groups):
            raise ValueError(f"Unknown/empty group in {requirement}")
    passed = {
        case.attrib["name"].split("[")[0]
        for case in reports.iter("testcase")
        if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
    }
    required = {test for tests in groups.values() for test in tests}
    required.update(data["replayFixtures"].values())
    if not required <= passed:
        raise ValueError(f"Missing executed acceptance tests: {required - passed}")
    replay = json.loads((ROOT / "specs/fixtures/replay.json").read_text())
    if not set(data["replayFixtures"]) <= {case["id"] for case in replay["cases"]}:
        raise ValueError("Unknown frozen replay case")
    ids = [case["id"] for case in data["cases"]]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Missing/duplicate reviewed cases")
    names = {
        case.attrib["name"]
        for case in reports.iter("testcase")
        if not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
    }
    for case in data["cases"]:
        if set(case) != {"id", "initial", "action", "expected"} or not case["expected"]:
            raise ValueError("Invalid fixture grammar")
        if f"testReviewedCases[{case['id']}]" not in names:
            raise ValueError("Unused reviewed fixture")


def main():
    scratch = ROOT / ".scratch"
    scratch.mkdir(exist_ok=True)
    subprocess.run([sys.executable, "scripts/check_layer1.py"], cwd=ROOT, check=True)
    config = scratch / "l2.coveragerc"
    config.write_text(
        "[run]\nbranch = True\nparallel = True\nsigterm = True\n"
        f"data_file = {scratch / 'l2.coverage'}\n"
        "source = modules/agent_client, modules/adapters, apps\n"
        "omit = */test_*.py\n[report]\nshow_missing = True\nfail_under = 0\n"
    )
    env = os.environ | {"AGENTLANCE_COVERAGE_CONFIG": str(config)}
    # Only remove this gate's generated coverage shards, never an execution journal.
    for path in scratch.glob("l2.coverage.*"):
        path.unlink()
    commands = [
        ["coverage", "erase", "--rcfile=" + str(config)],
        [
            "coverage",
            "run",
            "--rcfile=" + str(config),
            "-m",
            "pytest",
            *TESTS,
            "-m",
            "not l3socket",
            "-q",
            "--junitxml=" + str(scratch / "l2-tests.xml"),
        ],
        ["coverage", "combine", "--rcfile=" + str(config)],
        ["coverage", "json", "--rcfile=" + str(config), "-o", str(scratch / "l2-coverage.json")],
        ["coverage", "report", "--rcfile=" + str(config)],
    ]
    for command in commands:
        subprocess.run([sys.executable, "-m", *command], cwd=ROOT, env=env, check=True)
    data = json.loads((ROOT / "specs/fixtures/layer-2/cases.json").read_text())
    checkMappings(data, ET.parse(scratch / "l2-tests.xml").getroot())
    report = json.loads((scratch / "l2-coverage.json").read_text())
    expected = {
        str(path.relative_to(ROOT))
        for folder in SOURCES
        for path in (ROOT / folder).rglob("*.py")
        if not path.name.startswith("test_")
    }
    if set(report["files"]) != expected:
        raise ValueError(f"Coverage source mismatch: {expected - set(report['files'])}")
    for command in [["ruff", "check", "."], ["ruff", "format", "--check", "."]]:
        subprocess.run([sys.executable, "-m", *command], cwd=ROOT, check=True)
    subprocess.run(["git", "diff", "--check"], cwd=ROOT, check=True)
    print(
        "L2 gate passed: L0/L1 preserved; all seven requirements mapped to passing tests; "
        "real HTTPS demo."
    )


if __name__ == "__main__":
    main()
