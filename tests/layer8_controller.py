"""External conformance fixture control, outside the independent agent's public API."""

import asyncio
import os
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from modules.adapters.a2a.profile import awardHint, jsonBytes, strictJson
from modules.agent_client.signing import contentDigest
from tests.layer3_support import Rpc
from tests.layer4_support import Layer4Rig
from tests.layer8_support import PASSWORD, ROOT, ExternalAgent


async def prepare(path, stage):
    config = strictJson(path.read_bytes())
    if config.get("pid"):
        try:
            os.kill(config["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.sleep(0.15)
    rpc = Rpc(config["rpc"])
    # Fresh local state avoids Anvil snapshot-revert historical-state cache reuse.
    rpc.call("anvil_reset")
    rpc.call("anvil_setBlockTimestampInterval", 0)
    directory = path.parent / ("case-" + str(uuid4()))
    directory.mkdir()
    env = Layer4Rig(rpc, directory)
    public = SimpleNamespace(
        certificate=Path(config["certificate"]),
        key=Path(config["tlsKey"]),
        origin=config["publicOrigin"],
    )
    agent = ExternalAgent(
        env, directory, public, observeOnly=stage == "award", origin=config["agentOrigin"]
    )
    invocationPath = path.parent / "invocations.jsonl"
    invocationPath.write_bytes(b"")
    agent.config["invocationLog"] = str(invocationPath)
    agent.path.write_bytes(jsonBytes(agent.config))
    terms = env.rig.terms()
    terms.update(config["prerequisites"])
    receipt = env.command("createTask", {"terms": terms}, value=int(terms["budgetAtoms"]))
    task = env.rig.codec.event(receipt["logs"][0])["payload"]["task"]
    ref = task["taskRef"]
    bid = env.rig.bid(1, signer="owner")
    bid["offer"]["profileDigest"] = contentDigest(Path(agent.config["cardPath"]).read_bytes())
    env.command("submitBid", bid, caller="owner")
    env.rig.advance(int(terms["biddingClose"]))
    env.command("allocateTask", {"taskRef": ref})
    with (directory / "external-process.log").open("ab") as log:
        process = subprocess.Popen(
            ["node", str(ROOT / "examples/external-agent/main.mjs"), str(agent.path)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=os.environ | {"AGENTLANCE_FIXTURE_PASSWORD": PASSWORD},
        )
    config["pid"] = process.pid
    path.write_bytes(jsonBytes(config))
    deadline = time.monotonic() + 22
    try:
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=public.certificate), trust_env=False
        ) as http:
            while time.monotonic() < deadline:
                assert process.poll() is None, (directory / "external-process.log").read_text()
                env.finalize()
                view = await env.market.readTask(ref)
                try:
                    response = await http.get(agent.origin + "/.well-known/agent-card.json")
                    if response.status_code == 200 and (
                        stage == "award" or view["status"] == "SUBMITTED"
                    ):
                        print(jsonBytes({"message": awardHint(view, "conformance-hint")}).decode())
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise AssertionError((directory / "external-process.log").read_text())
    finally:
        await env.close()
        rpc.close()


if __name__ == "__main__":
    asyncio.run(prepare(Path(sys.argv[1]), sys.argv[2]))
