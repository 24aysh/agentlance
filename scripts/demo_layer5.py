"""Disconnected-forecast demo over the existing local EVM and participant."""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.layer3_tools import toolPath, verifyToolchain  # noqa: E402
from tests.layer3_support import localNode  # noqa: E402
from tests.layer4_support import Layer4Rig  # noqa: E402
from tests.layer5_support import runEconomicWorker  # noqa: E402


async def runScenario(rpc, directory):
    env = Layer4Rig(rpc, directory)
    try:
        return await runEconomicWorker(env)
    finally:
        await env.close()


def main():
    versions = verifyToolchain()
    subprocess.run(
        [toolPath("forge"), "build", "--locked", "--offline", "--quiet"], cwd=ROOT, check=True
    )
    scratch = ROOT / ".scratch/layer5"
    scratch.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="demo-", dir=scratch))
    with localNode(directory) as rpc:
        result = asyncio.run(runScenario(rpc, directory))
    output = directory / "demo.json"
    output.write_text(
        json.dumps(
            {
                "layer": "L5",
                "toolchain": versions,
                "scenario": result,
                "liveQualification": "pending",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"L5 local forecast/bid/history/restart evidence: {output}")


if __name__ == "__main__":
    main()
