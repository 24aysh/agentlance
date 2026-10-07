import asyncio
from copy import deepcopy

import httpx
import pytest

from modules.adapters.jev import JevPredictor
from modules.agent_client.economics import EconomicRuntime
from modules.agent_client.ports import AdapterError
from modules.economics.records import recordDigest
from tests.layer5_support import economicConfig, filterPolicy, usageReport


def createRuntime(l2, config=None, predictor=None):
    return EconomicRuntime(
        l2.participant,
        filterPolicy(l2.participant),
        1,
        l2.sign,
        config or economicConfig(l2.participant),
        lambda: l2.host.now,
        predictor,
    )


@pytest.mark.parametrize("failure", ["capacity", "denied", "expired", "price", "config"])
def testCandidatePreflightDoesNotForecast(l2, failure):
    async def run():
        await l2.create()
        view = await l2.market.readTask(l2.config["taskRef"])
        calls = []

        async def serve(request):
            calls.append(request)
            return httpx.Response(500)

        config = economicConfig(l2.participant)
        config["forecast"]["enabled"] = True
        if failure == "price":
            config["pricing"]["expiresAt"] = str(l2.host.now)
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            runtime = createRuntime(
                l2, config, JevPredictor(http, "test", "jev-1.13.0", "cost-choice-v1")
            )
            try:
                await runtime.enqueueCandidate(view["task"], view["stamp"])
                if failure == "capacity":
                    runtime.capacity = 0
                if failure == "denied":
                    runtime.filterPolicy["denyRequesters"] = [view["task"]["requester"]]
                if failure == "expired":
                    l2.host.setClock(int(view["task"]["terms"]["biddingClose"]))
                if failure == "config":
                    runtime.config["policy"]["version"] = "changed"
                await runtime.processCandidate(runtime.store.candidateKey(view["task"]["taskRef"]))
                assert calls == [] and runtime.store.items("forecast_attempts") == []
                assert (await l2.market.readBid(l2.config["taskRef"], l2.participant.agentRef))[
                    "bid"
                ] is None
            finally:
                await runtime.close()

    asyncio.run(run())


def testForecastDoesNotBlockAcceptedWork(l2):
    async def run():
        await l2.running()
        # Publish another independent root using the fixture protocol, while the own
        # accepted execution is awaiting its next tick.
        l2.terms.update(
            biddingClose="1500",
            allocationBy="1600",
            acceptBy="1800",
            resultBy="2000",
            validationBy="2200",
        )
        created = await l2.create()
        assert created["state"] == "APPLIED"
        ref = dict(l2.config["taskRef"], taskId="2")
        view = await l2.market.readTask(ref)
        started, release = asyncio.Event(), asyncio.Event()

        async def serve(request):
            started.set()
            await release.wait()
            return httpx.Response(429)

        config = economicConfig(l2.participant)
        config["forecast"]["enabled"] = True
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            runtime = createRuntime(
                l2, config, JevPredictor(http, "test", "jev-1.13.0", "cost-choice-v1")
            )
            runtime.capacity = 2
            try:
                await runtime.enqueueCandidate(view["task"], view["stamp"])
                await runtime.tick()
                await asyncio.wait_for(started.wait(), 2)
                for _ in range(3):
                    await l2.participant.tick()
                assert len(l2.calls) == 1
                assert l2.journal.get(l2.ref)["phase"] == "RESULT_RECORDED"
                assert not runtime.active.done()
                release.set()
                await runtime.active
                attempt = runtime.store.items("forecast_attempts")[0][1]
                assert attempt["state"] == "UNKNOWN" and attempt["reservedAtoms"] == "1"
            finally:
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["expiry", "clock-rollback"])
def testExpiryDuringOwnerSigningDoesNotCreateIntent(l2, failure):
    async def run():
        await l2.create()
        view = await l2.market.readTask(l2.config["taskRef"])
        runtime = createRuntime(l2)
        original = l2.participant.prepareBid

        async def delay(*args):
            # Beyond the forecast TTL (before bid close), or behind the persisted clock.
            if failure == "expiry":
                l2.host.setClock(1061)
            else:
                runtime.clock = lambda: 999
            return await original(*args)

        l2.participant.prepareBid = delay
        try:
            await runtime.enqueueCandidate(view["task"], view["stamp"])
            key = runtime.store.candidateKey(view["task"]["taskRef"])
            await runtime.processCandidate(key)
            assert not l2.journal.db.execute("SELECT * FROM operations").fetchall()
            if failure == "expiry":
                await runtime.processCandidate(key)
                assert runtime.store.get("economic_candidates", key)["disposition"] == "EXPIRED"
        finally:
            l2.participant.prepareBid = original
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["halt", "config", "foreign-reputation"])
def testChangeDuringReadsPreventsPaidForecast(l2, change):
    async def run():
        await l2.create()
        view = await l2.market.readTask(l2.config["taskRef"])
        calls = []

        async def serve(request):
            calls.append(request)
            return httpx.Response(500)

        config = economicConfig(l2.participant)
        config["forecast"]["enabled"] = True
        original = l2.market.readReputation
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
            runtime = createRuntime(
                l2, config, JevPredictor(http, "test", "jev-1.13.0", "cost-choice-v1")
            )

            async def changedRead(*args):
                reputation = await original(*args)
                if change == "halt":
                    with pytest.raises(AdapterError, match="halt fixture"):
                        l2.journal.halt("halt fixture")
                elif change == "config":
                    runtime.config["policy"]["version"] = "changed-during-read"
                else:
                    reputation["agentRef"] = dict(reputation["agentRef"], agentId="999")
                return reputation

            l2.market.readReputation = changedRead
            try:
                await runtime.enqueueCandidate(view["task"], view["stamp"])
                key = runtime.store.candidateKey(view["task"]["taskRef"])
                if change == "config":
                    await runtime.processCandidate(key)
                    assert runtime.store.get("economic_candidates", key)["decision"]["reasons"] == [
                        "CONFIG_CHANGED"
                    ]
                else:
                    with pytest.raises(AdapterError):
                        await runtime.processCandidate(key)
                assert not calls and not runtime.store.items("forecast_attempts")
                assert not l2.journal.db.execute("SELECT * FROM operations").fetchall()
            finally:
                l2.market.readReputation = original
                await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    ["agent", "task", "estimate", "zero", "provider", "duplicate", "provenance", "no-show"],
)
def testUsageTrustBoundary(l2, mutation):
    async def run():
        await l2.complete()
        view = await l2.market.readTask(l2.config["taskRef"])
        runtime = createRuntime(l2)
        try:
            report, context = usageReport(
                view["task"],
                runtime.config["runtime"],
                l2.participant.agentRef,
                runtime.config["operator"],
                None,
                l2.host.now,
            )
            if mutation == "agent":
                context["agentRef"] = dict(context["agentRef"], agentId="999")
            if mutation == "task":
                report["executionRef"]["taskRef"] = dict(
                    report["executionRef"]["taskRef"], taskId="999"
                )
            if mutation == "estimate":
                report["estimateId"] = recordDigest({"unknown": "estimate"})
            if mutation == "zero":
                report["items"] = []
            if mutation == "provider":
                report["items"][0]["provenance"] = "PROVIDER_ATTESTED"
            if mutation == "duplicate":
                report["items"].append(deepcopy(report["items"][0]))
            if mutation == "provenance":
                context["evidence"] = "LOCAL"
            if mutation == "no-show":
                context["status"] = "NO_SHOW"
            with pytest.raises(AdapterError):
                await runtime.history.recordUsage(report, context)
            assert runtime.store.activeReports() == []
        finally:
            await runtime.close()

    asyncio.run(run())
