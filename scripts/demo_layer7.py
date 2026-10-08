"""Complete local validator lifecycle; no paid model or public deployment required."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.agent_client.signing import contentDigest  # noqa: E402
from tests.layer3_support import localNode  # noqa: E402
from tests.layer4_support import DiscoveredWorker, Layer4Rig  # noqa: E402
from tests.layer6_market_support import runDelegation, runSolo  # noqa: E402
from tests.layer7_support import ValidatorProcess, localKubo, submit  # noqa: E402


async def scenario(rpc, directory, kubo, name):
    validators = []

    async def factory(env, public):
        process = await ValidatorProcess().open(env, directory, public, kubo)
        process.restartOnTransactions = name == "solo"
        validators.append(process)
        return process

    if name in {"solo", "children"}:
        result = await (
            runSolo(rpc, directory, validatorFactory=factory)
            if name == "solo"
            else runDelegation(rpc, directory, validatorFactory=factory)
        )
        assert result["validation"]["receipt"]["reason"] == "SUCCESS"
        result["transactions"] = validators[0].snapshot["transactions"]
        result["validatorRestarts"] = sorted(validators[0].restarted)
        return result
    env = Layer4Rig(rpc, directory)
    worker = DiscoveredWorker(env)
    validator = None
    try:
        task = submit(env, worker, 8 if name == "fail" else 7)
        public = directory / "public"
        public.mkdir()
        for path, raw in worker.blobs.items():
            (public / contentDigest(("https://agent.example" + path).encode())).write_bytes(raw)
        if name == "timeout":
            env.rig.advance(int(task["terms"]["validationBy"]))
            env.finalize()
        validator = await factory(env, public)
        outcome = await validator.settle(task)
        assert outcome["receipt"]["reason"] == (
            "VALIDATION_FAILED" if name == "fail" else "VALIDATOR_TIMEOUT"
        )
        return {"outcome": outcome, "transactions": validator.snapshot["transactions"]}
    finally:
        if validator:
            await validator.close()
        await worker.http.aclose()
        await env.close()


def main():
    from scripts.layer7_tools import setupLayer7

    setupLayer7()
    scratch = ROOT / ".scratch/layer7"
    directory = Path(tempfile.mkdtemp(prefix="demo-", dir=scratch))
    reports = []
    with localKubo() as kubo:
        for name in ("solo", "children", "fail", "timeout"):
            work = directory / name
            work.mkdir()
            with localNode(work) as rpc:
                result = asyncio.run(scenario(rpc, work, kubo, name))
            reports.append({"name": name, "result": result})
    report = {
        "layer": "L7",
        "liveQualification": "pending",
        "paidModelCalls": 0,
        "scope": (
            "real local EVM/pinned reputation/publisher/Docker/Kubo; "
            "fixture identity, artifact HTTPS and model transport; shared test ownership"
        ),
        "scenarios": reports,
    }
    output = directory / "demo.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print("L7 offline lifecycle evidence: " + str(output))


if __name__ == "__main__":
    main()
