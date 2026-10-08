"""Disclosure selection, correction, read-only access and adversarial free-text redaction."""

import asyncio
from copy import deepcopy

from modules.adapters.storage.economics import EconomicsStore
from modules.adapters.storage.validation import ValidationStore
from modules.economics.history import ExecutionHistory
from modules.economics.records import recordDigest
from modules.validation.disclosure import exportCost
from modules.validation.reconciliation import Reconciler
from tests.layer3_support import localNode
from tests.layer4_support import DiscoveredWorker, Layer4Rig
from tests.layer5_support import economicConfig, usageReport
from tests.layer6_market_support import settleFixture
from tests.layer7_support import submit


def testDisclosureCorrectionMissingZeroAndPrivacy(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            config = economicConfig(worker.participant)
            store = EconomicsStore(env.journal, env.rig.agentRef(0), config["operator"])
            history = ExecutionHistory(store, env.chain.schema, env.market)
            reconciler = Reconciler(history, env.watcher, ValidationStore(env.journal))
            task = submit(env, worker)
            ref = {"taskRef": task["taskRef"], "awardId": 1}
            missing = exportCost(env.path, ref, env.chain.schema, now=1)
            assert missing["ownReportedExecutionAtoms"] is None
            assert "MISSING_USAGE" in missing["missing"]
            report, context = usageReport(
                task,
                config["runtime"],
                env.rig.agentRef(0),
                config["operator"],
                None,
                env.rig.now(),
            )
            await history.recordUsage(report, context)
            settleFixture(env, task, await env.market.readTask(task["taskRef"]))
            await env.watcher.scanMarket()
            await reconciler.tick()
            # Poison only fields outside the allowlist in the retained selected record.
            selection = reconciler.store.get("selection", recordDigest([config["operator"], ref]))
            row = reconciler.store.get("reconciliation", selection["key"])
            row["observations"]["context"] = {
                "provider": "SECRET_PROVIDER",
                "prompt": "SECRET_PROMPT",
                "key": "SECRET_CREDENTIAL",
            }
            row["observations"]["comparison"]["private"] = "SECRET_PATH"
            reconciler.store.save("reconciliation", selection["key"], row)
            before = env.journal.db.total_changes
            first = exportCost(env.path, ref, env.chain.schema, now=2)
            assert first["ownReportedExecutionAtoms"] == "10"
            assert first["directChildPaidAtoms"] == "0"
            assert first["trust"] == "OPERATOR_REPORTED_UNAUTHENTICATED"
            assert "SECRET" not in str(first) and first["forecast"] is None
            assert env.journal.db.total_changes == before
            corrected, _ = usageReport(
                task,
                config["runtime"],
                env.rig.agentRef(0),
                config["operator"],
                None,
                env.rig.now(),
                cost=0,
            )
            await history.recordUsage(corrected, context, report["reportId"])
            stale = exportCost(env.path, ref, env.chain.schema, now=3)
            assert (
                stale["reportId"] == corrected["reportId"]
                and stale["ownReportedExecutionAtoms"] is None
            )
            assert "STALE_RECONCILIATION" in stale["missing"]
            await reconciler.tick()
            zero = exportCost(env.path, ref, env.chain.schema, now=4)
            assert zero["ownReportedExecutionAtoms"] == "0"
            assert (
                first["reportId"] != zero["reportId"] and first["ownReportedExecutionAtoms"] == "10"
            )
            assert zero["wholeAgentProfitAtoms"] is None
            incomplete = deepcopy(corrected)
            incomplete.update(reportId="0x" + "be" * 32, completeness="CENSORED")
            await history.recordUsage(incomplete, context, corrected["reportId"])
            await reconciler.tick()
            censored = exportCost(env.path, ref, env.chain.schema)
            assert censored["ownReportedExecutionAtoms"] is None
            assert "INCOMPLETE_USAGE" in censored["missing"]
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
