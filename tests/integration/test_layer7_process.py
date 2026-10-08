import asyncio

import pytest

from tests.layer3_support import localNode
from tests.layer6_market_support import runDelegation, runSolo
from tests.layer7_support import ValidatorProcess, localKubo


@pytest.mark.parametrize("scenario", ["solo", "children"])
def testIndependentValidatorAndWorkersWithReconciliation(tmp_path, scenario):
    async def run(rpc, kubo):
        async def factory(env, public):
            return await ValidatorProcess().open(env, tmp_path, public, kubo)

        result = await (
            runSolo(rpc, tmp_path, validatorFactory=factory)
            if scenario == "solo"
            else runDelegation(rpc, tmp_path, validatorFactory=factory)
        )
        assert result["validation"]["receipt"]["reason"] == "SUCCESS"
        reconciliation = result["reconciliation"]
        assert reconciliation["observations"]["comparison"]["missing"] == 0
        assert reconciliation["observations"]["wholeAgentProfitAtoms"] is None
        if scenario == "children":
            assert result["independentProcesses"] == 3
            assert reconciliation["observations"]["directChildPaidAtoms"] == str(
                sum(int(r["paidAtoms"]) for r in result["receipts"])
            )
        return result

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


def testLateChildSettlementRefreshesFailedParentReconciliation(tmp_path):
    async def run(rpc):
        result = await runDelegation(rpc, tmp_path, failure="parent-failure")
        parent = result["reconciliation"]["executionRef"]
        versions = [r for r in result["reconciliationVersions"] if r["executionRef"] == parent]
        assert any(r["observations"]["directChildPaidAtoms"] is None for r in versions)
        assert int(result["reconciliation"]["observations"]["directChildPaidAtoms"]) > 0
        assert result["reconciliation"]["observations"]["creditedPayoutAtoms"] == "0"
        assert result["reconciliation"]["observations"]["wholeAgentProfitAtoms"] is None

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
