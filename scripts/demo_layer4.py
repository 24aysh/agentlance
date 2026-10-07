"""Offline discovery-to-result demo using real chain adapters and a deterministic worker."""

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
from tests.layer4_support import Layer4Rig, runDiscoveredWorker  # noqa: E402


async def runScenario(rpc, directory):
    env = Layer4Rig(rpc, directory)
    try:
        return await runDiscoveredWorker(env)
    finally:
        await env.close()


def main():
    versions = verifyToolchain()
    subprocess.run(
        [toolPath("forge"), "build", "--locked", "--offline", "--quiet"], cwd=ROOT, check=True
    )
    scratch = ROOT / ".scratch/layer4"
    scratch.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="demo-", dir=scratch))
    with localNode(directory) as rpc:
        result = asyncio.run(runScenario(rpc, directory))
    report = {
        "layer": "L4",
        "scope": "isolated local EVM; synthetic registry/content and deterministic L2 worker",
        "toolchain": versions,
        "scenario": result,
        "liveGate": "pending",
    }
    output = directory / "demo.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"L4 discovery, finalized acceptance, result, restart and timeout evidence: {output}")


if __name__ == "__main__":
    main()
