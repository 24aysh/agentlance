import asyncio

import pytest

from tests.layer3_support import localNode
from tests.layer6_market_support import runDelegation


@pytest.mark.l3socket
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "unallocated",
        "restart",
        "failed",
        "validator-timeout",
        "unavailable",
        "parent-failure",
    ],
)
def testPublicChildMarketsAndFallback(tmp_path, failure):
    with localNode(tmp_path) as rpc:
        result = asyncio.run(runDelegation(rpc, tmp_path, failure=failure))
    assert result["childDeposits"] == 2
    assert result["independentProcesses"] == (3 if failure is None else 0)
    if failure != "parent-failure":
        assert result["result"] is not None


@pytest.mark.l3socket
def testSoloSdkEconomicExecutionAndEvaluation(tmp_path):
    from tests.layer6_market_support import runSolo

    with localNode(tmp_path) as rpc:
        result = asyncio.run(runSolo(rpc, tmp_path))
    assert result["modelCalls"] == 2
