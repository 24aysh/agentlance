"""Static reviewed outcomes. Never derive expected data from the component under test."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from modules.agent_client.signing import contentDigest

FIXTURE = Path("specs/fixtures/layer-2")
CASES = json.loads((FIXTURE / "cases.json").read_text())


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["id"])
def testReviewedCases(l2, case):
    async def run():
        name = case["id"]
        if name == "pending-award":
            await l2.award(pending=True)
            await l2.participant.receiveHint(l2.message)
        elif name == "uncertain-start":
            await l2.running()
            l2.journal.update(l2.ref, phase="READY")
            assert l2.journal.claim(l2.ref)
            l2.restart()
        elif name == "expired-award":
            await l2.award()
            await l2.participant.receiveHint(l2.message)
            l2.host.setClock(1500)
        else:
            await l2.complete()
            if name == "known-replay":
                l2.restart()
                await l2.participant.receiveHint(l2.message)
        await l2.participant.advance(l2.ref)
        actual = {
            "phase": l2.journal.get(l2.ref)["phase"],
            "invocations": len(l2.calls),
            "status": l2.host.view(l2.config["taskRef"])["status"],
            "events": len(l2.host.events),
        }
        assert actual == case["expected"]

    asyncio.run(run())


def testIndependentFixtureHashes():
    for name, digest in CASES["byteHashes"].items():
        assert contentDigest((FIXTURE / name).read_bytes()) == digest
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import fs from 'node:fs'; import assert from 'node:assert/strict'; "
            "import {keccak256} from 'ethers'; "
            "const dir='specs/fixtures/layer-2/'; "
            "const data=JSON.parse(fs.readFileSync(dir+'cases.json')); "
            "for(const [name,digest] of Object.entries(data.byteHashes)) "
            "assert.equal(keccak256(fs.readFileSync(dir+name)),digest);",
        ],
        check=True,
    )


def testReferenceImportBoundary():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import apps.reference_agent.main; "
            'assert not any(name.startswith("modules.market_core") for name in sys.modules)',
        ],
        check=True,
    )
