"""Funded Layer 3 scenarios on an isolated Monad Anvil node.

Every output is actual transaction/receipt data. Artifacts and validator verdicts
are synthetic fixture material; no model execution or production validator runs.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.layer3_tools import toolPath, verifyToolchain  # noqa: E402
from tests.integration.test_layer3_transactions import (  # noqa: E402
    runMalicious,
    runTerminal,
    runTree,
)
from tests.layer3_support import Layer3Rig, localNode, readJson  # noqa: E402


def runLocal(workDir, scenario):
    versions = verifyToolchain()
    subprocess.run(
        [toolPath("forge"), "build", "--locked", "--offline", "--quiet"], cwd=ROOT, check=True
    )
    cases = readJson("specs/fixtures/layer-3.json")["cases"]
    scenarios = (
        [scenario] if scenario != "all" else ["solo", "parent-child", "timeouts", "malicious"]
    )
    reports = []
    with localNode(workDir) as rpc:
        for selected in scenarios:
            endings = (
                ["SUCCESS", "PARENT_TIMEOUT", "ACTIVE_CHILD"]
                if selected == "parent-child"
                else [None]
            )
            selectedCases = cases[1:] if selected == "timeouts" else [cases[0]]
            if selected not in ("solo", "timeouts"):
                selectedCases = [None]
            for ending in endings:
                for case in selectedCases:
                    rig = Layer3Rig(rpc)
                    if selected in ("solo", "timeouts"):
                        runTerminal(rig, case)
                    elif selected == "parent-child":
                        runTree(rig, ending)
                    else:
                        runMalicious(rig)
                    reports.append(
                        {
                            "scenario": selected,
                            "case": case["id"] if case else ending,
                            **rig.report(),
                        }
                    )
    result = {
        "layer": "L3",
        "scope": "local funded transactions",
        "toolchain": versions,
        "reports": reports,
        "liveGate": "pending",
        "compatibilityExceptions": readJson("specs/fixtures/layer-3.json")[
            "compatibilityExceptions"
        ],
    }
    output = Path(workDir) / "demo.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=["local", "monad-testnet"], default="local")
    parser.add_argument(
        "--scenario",
        choices=["all", "solo", "parent-child", "timeouts", "malicious"],
        default="all",
    )
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--report", type=Path, help="Previously qualified L3 testnet verification report"
    )
    args = parser.parse_args()
    if args.network == "monad-testnet":
        from scripts.layer3_testnet import runTestnet

        if args.report is None or args.scenario == "all":
            parser.error("Testnet requires --report and one named scenario")
        output = runTestnet(args.report, args.scenario)
    else:
        scratch = ROOT / ".scratch/layer3"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = args.work_dir or Path(tempfile.mkdtemp(prefix="demo-", dir=scratch))
        output = runLocal(directory, args.scenario)
    print(f"Layer 3 transaction evidence: {output}")


if __name__ == "__main__":
    main()
