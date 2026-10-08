import asyncio
from copy import deepcopy

import pytest

from modules.adapters.storage.economics import EconomicsStore
from modules.adapters.storage.validation import ValidationStore
from modules.agent_client.ports import AdapterError
from modules.economics.history import ExecutionHistory
from modules.validation.reconciliation import Reconciler
from tests.layer3_support import localNode
from tests.layer4_support import DiscoveredWorker, Layer4Rig
from tests.layer5_support import economicConfig, usageReport
from tests.layer6_market_support import settleFixture
from tests.layer7_support import submit


@pytest.mark.parametrize("usageFirst", [False, True])
def testReceiptUsageOrderingCorrectionsAndMissingCosts(tmp_path, usageFirst):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            config = economicConfig(worker.participant)
            history = ExecutionHistory(
                EconomicsStore(env.journal, env.rig.agentRef(0), config["operator"]),
                env.rig.codec.schema,
                env.market,
            )
            reconciler = Reconciler(history, env.watcher, ValidationStore(env.journal))
            task = submit(env, worker)
            ref = {"taskRef": task["taskRef"], "awardId": 1}
            report, context = usageReport(
                task,
                config["runtime"],
                env.rig.agentRef(0),
                config["operator"],
                None,
                env.rig.now(),
            )
            if usageFirst:
                await history.recordUsage(report, context)
            settleFixture(env, task, await env.market.readTask(task["taskRef"]))
            await env.watcher.scanMarket()
            await reconciler.tick()
            first = reconciler.readReconciliation(ref)
            if not usageFirst:
                assert "MISSING_USAGE" in first["missing"]
                assert first["observations"]["ownReportedExecutionAtoms"] is None
                await history.recordUsage(report, context)
            await reconciler.tick()
            second = reconciler.readReconciliation(ref)
            assert second["observations"]["ownReportedExecutionAtoms"] == "10"
            assert second["observations"]["wholeAgentProfitAtoms"] is None
            assert second["observations"]["directChildPaidAtoms"] == "0"
            assert second["observations"]["comparison"]["excludedReasons"]["MISSING_ESTIMATE"] == 1
            corrected, _ = usageReport(
                task,
                config["runtime"],
                env.rig.agentRef(0),
                config["operator"],
                None,
                env.rig.now(),
                cost=15,
            )
            bad = deepcopy(corrected)
            bad["items"][0]["expenseId"] = report["items"][0]["expenseId"]
            with pytest.raises(AdapterError, match="Conflicting expense"):
                await history.recordUsage(bad, context, report["reportId"])
            await history.recordUsage(corrected, context, report["reportId"])
            await reconciler.tick()
            assert (
                reconciler.readReconciliation(ref)["observations"]["ownReportedExecutionAtoms"]
                == "15"
            )
            assert second in [row for _, row in reconciler.store.rows("reconciliation")]
            count = len(reconciler.store.rows("reconciliation"))
            await reconciler.tick()
            assert len(reconciler.store.rows("reconciliation")) == count
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testReceiptConsumerCheckpointIsAtomicAndConflictsHalt(tmp_path):
    from modules.adapters.a2a.profile import jsonBytes, strictJson
    from modules.economics.records import recordDigest
    from modules.validation.observer import OutcomeObserver

    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            task = submit(env, worker)
            settleFixture(env, task, await env.market.readTask(task["taskRef"]))
            await env.watcher.scanMarket()
            store = ValidationStore(env.journal, maxRows=1)
            observer = OutcomeObserver(env.market, env.watcher, store)
            with pytest.raises(AdapterError, match="storage full"):
                await observer.consume()
            assert store.get("receipt", recordDigest(task["taskRef"])) is None
            checkpoint = store.get("cursor", "outcomes-v1")
            assert checkpoint is not None
            store.maxRows = 10
            await observer.consume()
            receipt = store.get("receipt", recordDigest(task["taskRef"]))
            assert receipt["receipt"]["reason"] == "SUCCESS"
            await observer.consume()
            assert len(store.rows("receipt")) == 1
            with store.db:
                store.db.execute("DELETE FROM validation_records WHERE kind='cursor'")
                rows = list(
                    store.db.execute(
                        "SELECT hash,position,body FROM observed_logs WHERE stream='market'"
                    )
                )
                for blockHash, position, raw in rows:
                    event = strictJson(raw)
                    if event["event"]["name"] == "TaskSettled":
                        event["event"]["payload"]["receipt"]["paidAtoms"] = "0"
                        store.db.execute(
                            "UPDATE observed_logs SET body=? "
                            "WHERE stream='market' AND hash=? AND position=?",
                            (jsonBytes(event), blockHash, position),
                        )
            with pytest.raises(AdapterError, match="FINALITY_CONFLICT"):
                await observer.consume()
            assert env.journal.halted()
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testSameBlockSubmissionAndSettlementSkipsEvaluation(tmp_path):
    from modules.validation.observer import OutcomeObserver

    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            buffered = []
            command = env.command

            def deferSubmission(name, data, **options):
                if name == "submitResult":
                    buffered.append((name, data))
                else:
                    return command(name, data, **options)

            env.command = deferSubmission
            task = submit(env, worker)
            view = await env.market.readTask(task["taskRef"])
            view["result"] = buffered[0][1]
            env.command = lambda name, data: buffered.append((name, data))
            settleFixture(env, task, view)
            env.command = command
            rpc.call("evm_setAutomine", False)
            hashes = [
                rpc.call(
                    "eth_sendTransaction",
                    {
                        "from": env.rig.actors["signer"],
                        "to": env.rig.market,
                        "data": env.rig.codec.commandData(name, data),
                        "gas": hex(5_000_000),
                    },
                )
                for name, data in buffered
            ]
            rpc.call("evm_mine")
            rpc.call("evm_setAutomine", True)
            receipts = [rpc.receipt(tx) for tx in hashes]
            assert all(receipt["status"] == "0x1" for receipt in receipts)
            assert receipts[0]["blockHash"] == receipts[1]["blockHash"]
            env.finalize()
            await env.watcher.scanMarket()
            admitted = []
            observer = OutcomeObserver(
                env.market,
                env.watcher,
                ValidationStore(env.journal),
                admit=lambda *args: admitted.append(args),
            )
            await observer.consume()
            outcome = observer.readOutcome(task["taskRef"])
            assert not admitted
            assert outcome["status"] == "SETTLED"
            assert outcome["result"]["artifact"] == view["result"]["artifact"]
            assert outcome["receipt"]["reason"] == "SUCCESS"
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
