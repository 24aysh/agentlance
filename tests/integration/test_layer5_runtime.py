import asyncio

import pytest

from tests.layer3_support import localNode
from tests.layer4_support import Layer4Rig
from tests.layer5_support import runEconomicWorker


@pytest.mark.l3socket
def testEconomicWorkerChainHistoryRestart(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            report = await runEconomicWorker(env)
            assert report["workerInvocations"] == 1
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
