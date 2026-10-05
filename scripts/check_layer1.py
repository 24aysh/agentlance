"""Run the offline L0/L1 gate, including exact coverage rather than rounded percentages."""

import json
import subprocess
import sys
from pathlib import Path

repoRoot = Path(__file__).resolve().parents[1]


def checkCoverage(report, expectedFiles):
    if set(report["files"]) != set(expectedFiles):
        raise ValueError("Coverage must include every domain/core source file")
    for name, entry in report["files"].items():
        summary = entry["summary"]
        if summary["missing_lines"] or summary["missing_branches"]:
            raise ValueError(f"Incomplete statement/branch coverage: {name}")
    if not report["totals"]["num_statements"]:
        raise ValueError("Empty coverage report")


def main():
    scratch = repoRoot / ".scratch"
    scratch.mkdir(exist_ok=True)
    coveragePath = scratch / "l1-coverage.json"
    commands = [
        ["scripts/check_specs.py"],
        ["-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py"],
        ["-m", "coverage", "run", "-m", "pytest", "modules", "tests", "-q"],
        ["-m", "coverage", "json", "--fail-under=0", "-o", str(coveragePath)],
    ]
    for command in commands:
        subprocess.run([sys.executable, *command], cwd=repoRoot, check=True)
    expected = {
        str(path.relative_to(repoRoot))
        for directory in ("domain", "market_core")
        for path in (repoRoot / "modules" / directory).rglob("*.py")
        if not path.name.startswith("test_")
    }
    checkCoverage(json.loads(coveragePath.read_text()), expected)
    for command in (
        ["coverage", "report"],
        ["ruff", "check", "."],
        ["ruff", "format", "--check", "."],
    ):
        subprocess.run([sys.executable, "-m", *command], cwd=repoRoot, check=True)
    print("L1 gate passed: all checks; zero missing domain/core statements or branches.")


if __name__ == "__main__":
    main()
