import asyncio
from copy import deepcopy

import httpx
import pytest

from modules.adapters.jev import JevPredictor
from modules.agent_client.economics import EconomicRuntime
from modules.agent_client.ports import AdapterError
from modules.economics.estimator import estimateCost
from modules.economics.records import recordDigest
from tests.layer5_support import drainEconomics, economicConfig, filterPolicy, usageReport


def runtimeFor(l2, config=None, predictor=None):
    return EconomicRuntime(
        l2.participant,
        filterPolicy(l2.participant),
        1,
        l2.sign,
        config or economicConfig(l2.participant),
        lambda: l2.host.now,
        predictor,
    )


def testFixtureReputationAndCandidateReplay(l2):
    async def run():
        await l2.create()
        task = (await l2.market.readTask(l2.config["taskRef"]))["task"]
        p = await l2.market.readReputation(task["taskRef"], l2.participant.agentRef)
        assert p["p"] == 500000 and p["snapshotBlock"] == task["reputationSnapshotBlock"]
        economics = runtimeFor(l2)
        try:
            await asyncio.gather(*(economics.enqueueCandidate(task, p["stamp"]) for _ in range(5)))
            await drainEconomics(economics)
            assert len(economics.store.items("economic_candidates")) == 1
            candidate = economics.store.items("economic_candidates")[0][1]
            assert candidate["decision"]["contextDigest"] == recordDigest(
                candidate["evaluation"]["context"]
            )
            assert (await l2.market.readBid(task["taskRef"], l2.participant.agentRef))["bid"][
                "offer"
            ]["bidAtoms"] == "20"
        finally:
            await economics.close()
        l2.restart()
        changed = economicConfig(l2.participant)
        changed["pricing"]["rates"][0]["rate"]["numerator"] = "999"
        economics = runtimeFor(l2, changed)
        try:
            await drainEconomics(economics)
            assert len(economics.store.items("economic_estimates")) == 1
            assert len(l2.journal.db.execute("SELECT * FROM operations").fetchall()) == 1
        finally:
            await economics.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "crash", ["before-call", "after-response", "after-decision", "after-intent"]
)
def testForecastCrashRetainsChargeAndIntent(l2, crash):
    async def run():
        await l2.create()
        view = await l2.market.readTask(l2.config["taskRef"])
        config = economicConfig(l2.participant)
        config["forecast"]["enabled"] = True
        calls = []

        async def serve(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {
                        "cost": {
                            "type": "choice",
                            "choice": "normal",
                            "confidence": 1,
                            "probabilities": {"normal": 1, "other": 0},
                        }
                    },
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            predictor = JevPredictor(http, "fixture", "jev-1.13.0", "cost-choice-v1")
            economics = runtimeFor(l2, config, predictor)
            await economics.enqueueCandidate(view["task"], view["stamp"])
            key = economics.store.candidateKey(view["task"]["taskRef"])
            if crash == "before-call":
                assert economics.store.reserveForecast(
                    key, config["forecast"], recordDigest({}), l2.host.now
                )
            else:
                if crash == "after-response":
                    original = economics.store.saveEstimate

                    def fail(bundle):
                        raise RuntimeError("crash after durable provider response")

                    economics.store.saveEstimate = fail
                elif crash == "after-decision":
                    original = l2.participant.prepareBid

                    async def fail(*args):
                        raise RuntimeError("crash before bid intent")

                    l2.participant.prepareBid = fail
                else:
                    original = l2.market.submitSignedBid

                    async def fail(*args):
                        raise RuntimeError("crash after bid intent")

                    l2.market.submitSignedBid = fail
                with pytest.raises(RuntimeError, match="crash"):
                    await economics.processCandidate(key)
                if crash == "after-response":
                    economics.store.saveEstimate = original
                elif crash == "after-decision":
                    l2.participant.prepareBid = original
                else:
                    l2.market.submitSignedBid = original
            await economics.close()
            l2.restart()
            # Fixture market does not opt in to unknown-operation retransmission. L4's
            # real-chain crash path is independently covered by its regression suite.
            if crash == "after-intent":
                l2.market.recoverUnknown = True
            l2.host.setClock(l2.host.now + 2)
            economics = runtimeFor(l2, config, predictor)
            try:
                await economics.processCandidate(key)
                assert len(calls) == (0 if crash == "before-call" else 1)
                attempt = economics.store.get("forecast_attempts", key)
                assert attempt["reservedAtoms"] == "1"
                assert (await l2.market.readBid(view["task"]["taskRef"], l2.participant.agentRef))[
                    "bid"
                ] is not None
                assert len(l2.journal.db.execute("SELECT * FROM operations").fetchall()) == 1
            finally:
                await economics.close()

    asyncio.run(run())


def testHistoryCorrectionsCensoringAndEvaluation(l2):
    async def run():
        await l2.complete()
        view = await l2.market.readTask(l2.config["taskRef"])
        task = view["task"]
        economics = runtimeFor(l2)
        config = economics.config
        try:
            empty = {"samples": [], "adequate": True, "expiresAt": 2000}
            estimate = estimateCost(
                task, config["runtime"], config["pricing"], empty, 1000, l2.schema
            )["estimate"]
            economics.store.saveEstimate(
                {
                    "estimate": estimate,
                    "baseline": estimate,
                    "pricing": config["pricing"],
                    "provenance": {},
                }
            )
            report, context = usageReport(
                task,
                config["runtime"],
                l2.participant.agentRef,
                config["operator"],
                estimate["estimateId"],
                l2.host.now,
            )
            assert await economics.history.recordUsage(report, context) == "STORED"
            assert await economics.history.recordUsage(report, context) == "UNCHANGED"
            changed = deepcopy(report)
            changed["items"][0]["costAtoms"] = "11"
            with pytest.raises(AdapterError, match="Conflicting report"):
                await economics.history.recordUsage(changed, context)
            changed["reportId"] = recordDigest(changed)
            with pytest.raises(AdapterError, match="active report"):
                await economics.history.recordUsage(changed, context)
            with pytest.raises(AdapterError, match="Conflicting expense"):
                await economics.history.recordUsage(changed, context, report["reportId"])
            changed["items"][0]["expenseId"] = recordDigest(changed["items"][0])
            assert (
                await economics.history.recordUsage(changed, context, report["reportId"])
                == "STORED"
            )
            assert await economics.history.recordUsage(report, context) == "UNCHANGED"
            evaluation = economics.history.evaluateForecasts([report["executionRef"]])
            assert evaluation["forecast"]["p80"]["eligible"] == 1
            assert evaluation["observations"][0]["comparisonAtoms"] == "10"
            assert evaluation["observations"][0]["reportedItems"][0]["costAtoms"] == "11"
            assert evaluation["observations"][0]["evidence"] == "FIXTURE"
            later = deepcopy(task)
            later["taskRef"]["taskId"] = "2"
            for alternate, at, reason in (
                (task, l2.host.now, "OWN_TASK"),
                (later, l2.host.now + config["runtime"]["maxHistoryAgeSeconds"], "AGE"),
                (
                    later | {"taskRef": dict(later["taskRef"], market="0x" + "ab" * 20)},
                    l2.host.now,
                    "INCOMPATIBLE",
                ),
            ):
                selection = economics.history.selectHistory(
                    alternate, config["runtime"], at, "FIXTURE"
                )
                assert selection["samples"] == []
                assert selection["excludedReasons"][reason] == 1
            censored = deepcopy(changed)
            censored.update(completeness="CENSORED", reportId=recordDigest(["censored", changed]))
            assert (
                await economics.history.recordUsage(censored, context, changed["reportId"])
                == "STORED"
            )
            selected = economics.history.selectHistory(
                later, config["runtime"], l2.host.now, "FIXTURE"
            )
            assert not selected["adequate"] and selected["excludedCount"] == 1
            evaluation = economics.history.evaluateForecasts([report["executionRef"]] * 2)
            assert evaluation["excluded"] == 1 and evaluation["missing"] == 0
            assert evaluation["excludedReasons"]["INCOMPLETE"] == 1
            assert evaluation["forecast"]["p80"]["unavailableReason"] == "NO_ELIGIBLE_QUANTILES"
            assert evaluation["forecast"]["meanAbsoluteErrorAtoms"] is None
        finally:
            await economics.close()

    asyncio.run(run())
