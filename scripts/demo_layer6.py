"""Offline SDK/Docker/local-EVM evidence. No model key, testnet or production validator."""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.layer3_tools import toolPath, verifyToolchain  # noqa: E402
from scripts.layer6_image import buildImage  # noqa: E402
from tests.layer3_support import localNode  # noqa: E402
from tests.layer6_market_support import runDelegation, runSolo  # noqa: E402


def main():
    verifyToolchain()
    subprocess.run(
        [toolPath("forge"), "build", "--locked", "--offline", "--quiet"], cwd=ROOT, check=True
    )
    image = buildImage()
    directory = Path(tempfile.mkdtemp(prefix="demo-", dir=ROOT / ".scratch/layer6"))
    reports = []
    for name in ("solo", "delegation", "unallocated"):
        scenario = directory / name
        scenario.mkdir()
        with localNode(scenario) as rpc:
            result = asyncio.run(
                runSolo(rpc, scenario)
                if name == "solo"
                else runDelegation(
                    rpc, scenario, failure="unallocated" if name == "unallocated" else None
                )
            )
        reports.append({"name": name, "result": result})
    output = directory / "demo.json"
    output.write_text(
        json.dumps(
            {
                "layer": "L6",
                "image": image,
                "scenarios": reports,
                "liveQualification": "pending",
                "paidModelCalls": 0,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"L6 solo/public delegation/fallback evidence: {output}")


if __name__ == "__main__":
    main()
