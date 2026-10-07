"""Own finalized watcher drives the real participant with no notification/index service."""

import asyncio

import pytest

from tests.layer3_support import localNode
from tests.layer4_support import Layer4Rig, runDiscoveredWorker

pytestmark = pytest.mark.l3socket


def testDirectDiscoveredWorkerRestart(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            report = await runDiscoveredWorker(env)
            assert len(report["transactionHashes"]) == 3
            assert len(set(report["transactionHashes"])) == 3
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
