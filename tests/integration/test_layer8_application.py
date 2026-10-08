"""Requester recovery and public views against real isolated Monad EVM state."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from eth_account import Account

from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection
from modules.adapters.storage.journal import Journal
from modules.agent_client.application import validateOutput
from modules.agent_client.discovery import MarketWatcher, pageCursor, readCursor
from modules.agent_client.inspection import Inspector
from modules.agent_client.participant import makeCommand
from modules.agent_client.ports import AdapterError
from modules.agent_client.requester import Requester
from modules.agent_client.signing import contentDigest
from tests.layer3_support import localNode
from tests.layer4_support import DiscoveredWorker, Layer4Rig

pytestmark = pytest.mark.l3socket


def testArtifactInspectionPreservesValidatorResultTransportLimit():
    class Content:
        def __init__(self):
            self.limits = {}

        async def fetchBytes(self, uri, limit, digest):
            self.limits[uri] = limit
            return b""

    content = Content()
    inspector = Inspector(
        SimpleNamespace(chain=object(), schema={}),
        object(),
        content,
        networkScope="LOCAL_FIXTURE",
    )
    refs = {
        name: {"uri": "https://example.test/" + name, "digest": "0x" + "00" * 32}
        for name in ("input", "outputSchema", "validationPolicy", "result")
    }
    view = {
        "task": {"terms": {name: refs[name] for name in refs if name != "result"}},
        "result": {"artifact": refs["result"]},
        "receipt": None,
    }

    items = asyncio.run(inspector.readArtifacts(view))

    assert all(item["status"] == "VERIFIED" for item in items)
    assert content.limits[refs["input"]["uri"]] == 1048576
    assert content.limits[refs["result"]["uri"]] == 2097152


def requesterFor(env, worker, tmp_path):
    journal = Journal(tmp_path / "requester.sqlite", {"role": "application"})
    chain = ChainConnection(
        env.transport,
        env.facts,
        env.genesis,
        journal,
        env.rig.codec,
        env.rig.readAbi,
        60,
        clock=env.rig.now,
    )
    market = MonadMarket(
        chain, Account.from_key(env.rig.keys["requester"]), maxGas=5000000, clock=env.rig.now
    )
    requester = Requester(market, worker.participant.content)
    inspector = Inspector(
        market, MarketWatcher(chain), worker.participant.content, networkScope="LOCAL_FIXTURE"
    )
    return requester, inspector


def requestTerms(env, worker):
    terms = env.rig.terms()
    for name, path in (
        ("input", "/input.json"),
        ("outputSchema", "/output-schema.json"),
        ("validationPolicy", "/policy.json"),
    ):
        terms[name] = {
            "uri": "https://agent.example" + path,
            "digest": contentDigest(worker.blobs[path]),
        }
    return terms


def testApplicationRejectsIncompleteLiveQualification(tmp_path):
    from apps.cli.main import application
    from modules.adapters.a2a.profile import jsonBytes
    from tests.layer3_support import readJson

    facts = next(
        row["value"]
        for row in readJson("specs/fixtures/objects.json")["objects"]
        if row["type"] == "DeploymentManifest"
    )
    manifest, registry = tmp_path / "manifest.json", tmp_path / "registry.json"
    manifest.write_bytes(jsonBytes(facts))
    config = {
        "networkScope": "QUALIFIED_TESTNET",
        "manifestPath": str(manifest),
        "registryVerificationPath": str(registry),
        "validatorVerificationPath": None,
        "rpcUrl": facts["rpcUrls"][0],
        "genesisHash": "0x" + "00" * 32,
        "database": str(tmp_path / "must-not-open.sqlite"),
        "keystore": None,
        "maxFinalizedAgeSeconds": 60,
        "maxGas": 2000000,
        "maxGasPriceWei": "1000000000",
        "ipfsGateway": "https://gateway.example",
        "fixtureOrigins": [],
        "caFile": None,
    }
    identity = {k: facts[k] for k in ("identityRegistry", "identityVersion")}

    async def run():
        for evidence in (
            {},
            {"identityVerification": identity},
            {
                "identityVerification": identity | {"identityRegistry": facts["market"]},
                "feedbackVerification": {"present": True},
            },
            {
                "identityVerification": identity | {"identityVersion": "foreign"},
                "feedbackVerification": {"present": True},
            },
        ):
            registry.write_bytes(jsonBytes(evidence))
            with pytest.raises(AdapterError, match="Complete registry qualification binding"):
                async with application(config, signing=True):
                    pytest.fail("Unqualified configuration reached the application")
        assert not (tmp_path / "must-not-open.sqlite").exists()

    asyncio.run(run())


def testRequesterRootCancelWithdrawRecovery(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        app, inspector = requesterFor(env, worker, tmp_path)
        try:
            terms = requestTerms(env, worker)
            command = makeCommand("createTask", terms=terms)
            created = await app.submit(command, "root")
            assert created["taskRef"] is None
            tx = created["transactionHash"]
            # Inspection cannot sign/rebroadcast, even before confirmation.
            originalCall = env.transport.call

            async def readOnly(method, *params):
                assert method != "eth_sendRawTransaction"
                return await originalCall(method, *params)

            env.transport.call = readOnly
            assert (await app.readOperation("root"))["confirmation"] == "MINED_UNFINALIZED"
            env.transport.call = originalCall
            env.finalize()
            created = await app.resume("root")
            assert created["confirmation"] == "FINALIZED_APPLIED"
            ref = created["taskRef"]
            assert ref["taskId"] == "1"
            assert (await app.submit(command, "root"))["transactionHash"] == tx
            changed = deepcopy(command)
            changed["input"]["terms"]["budgetAtoms"] = "101"
            with pytest.raises(AdapterError, match="body changed"):
                await app.submit(changed, "root")
            details = validateOutput(await inspector.readTaskDetails(ref))
            assert details["data"]["view"]["task"]["terms"] == terms
            assert details["data"]["auction"]["status"] == "PREVIEW"
            assert details["complete"]
            await app.submit(makeCommand("cancelTask", taskRef=ref), "cancel")
            env.finalize()
            assert (await app.resume("cancel"))["result"]["events"][0]["payload"]["receipt"][
                "reason"
            ] == "CANCELLED"
            credit = validateOutput(await inspector.readCredit(env.rig.actors["requester"]))
            assert credit["data"]["credit"]["amountAtoms"] == terms["budgetAtoms"]
            withdrawal = makeCommand(
                "withdrawCredit",
                receiver=env.rig.actors["receiver"],
                amountAtoms=terms["budgetAtoms"],
            )
            sent = await app.submit(withdrawal, "withdraw")
            env.finalize()
            done = await app.resume("withdraw")
            assert done["transactionHash"] == sent["transactionHash"]
            assert done["confirmation"] == "FINALIZED_APPLIED"
            assert (await app.submit(withdrawal, "withdraw"))["transactionHash"] == sent[
                "transactionHash"
            ]
            assert (await inspector.readCredit(env.rig.actors["requester"]))["data"]["credit"][
                "amountAtoms"
            ] == "0"
            # Retained creation does not depend on today's deadlines or content.
            env.rig.advance(int(terms["validationBy"]))
            env.finalize()
            worker.blobs.clear()
            assert (await app.submit(command, "root"))["taskRef"] == ref
        finally:
            app.journal.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testRequesterValidationAndAuctionPagination(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        app, inspector = requesterFor(env, worker, tmp_path)
        try:
            env.rig.register(1)
            env.finalize()
            terms = requestTerms(env, worker)
            bad = deepcopy(terms)
            bad["alphaNum"] = bad["alphaDen"] = "2"
            with pytest.raises(AdapterError):
                await app.submit(makeCommand("createTask", terms=bad), "bad")
            assert app.journal.nativeOperations() == []
            bad = deepcopy(terms)
            bad["input"]["digest"] = "0x" + "12" * 32
            with pytest.raises(AdapterError):
                await app.validateTaskRequest(bad, env.rig.actors["requester"])
            await app.submit(makeCommand("createTask", terms=terms), "root")
            env.finalize()
            ref = (await app.resume("root"))["taskRef"]
            env.command("submitBid", env.rig.bid(1, agentId=0), caller="owner")
            env.command("submitBid", env.rig.bid(1, agentId=1), caller="owner")
            rejected = await app.submit(makeCommand("cancelTask", taskRef=ref), "cancel-race")
            assert rejected["confirmation"] == "REJECTED"
            page = validateOutput(await inspector.listTaskBids(ref, 1))
            assert len(page["data"]["bids"]) == 1 and page["nextCursor"]
            other = validateOutput(await inspector.listTaskBids(ref, 1, page["nextCursor"]))
            assert other["data"]["auction"] == page["data"]["auction"]
            assert other["observedAt"] == page["observedAt"]
            forged = readCursor(page["nextCursor"])
            forged["stamp"]["blockHash"] = "0x" + "12" * 32
            with pytest.raises(AdapterError, match="Cursor"):
                await inspector.listTaskBids(ref, 1, pageCursor(forged))
            await app.chain.qualify()  # Forged input did not halt the chain journal.
            env.rig.advance(int(terms["biddingClose"]))
            env.finalize()
            await app.submit(makeCommand("allocateTask", taskRef=ref), "allocate")
            env.finalize()
            await app.resume("allocate")
            assert (await inspector.listTaskBids(ref))["data"]["auction"]["status"] == "MATCH"
            agent = validateOutput(await inspector.readAgentDetails(env.rig.agentRef(0), ref))
            assert agent["data"]["counters"] == {"successes": "0", "failures": "0"}
            assert agent["data"]["prior"]["kind"] == "SYNTHETIC_BETA"
            assert agent["data"]["endpoint"] == "UNPROBED"
            validateOutput(await inspector.readTaskTree(ref))
        finally:
            app.journal.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testRequesterLostSendAndWithdrawalBoundaries(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        receiver = env.rig.deploy("RevertingReceiver", sourceName="HostileActors")
        env.finalize()
        worker = DiscoveredWorker(env)
        app, inspector = requesterFor(env, worker, tmp_path)
        try:
            original = env.transport.call

            async def loseSend(method, *args):
                value = await original(method, *args)
                if method == "eth_sendRawTransaction":
                    raise AdapterError("UNAVAILABLE", "Injected lost response after broadcast")
                return value

            env.transport.call = loseSend
            command = makeCommand("createTask", terms=requestTerms(env, worker))
            sent = await app.submit(command, "lost-root")
            env.transport.call = original
            env.finalize()
            # Reopen the actual durable journal; no in-memory operation recovery.
            app.journal.close()
            app, inspector = requesterFor(env, worker, tmp_path)
            created = await app.submit(command, "lost-root")
            assert created["transactionHash"] == sent["transactionHash"]
            ref = created["taskRef"]
            await app.submit(makeCommand("cancelTask", taskRef=ref), "cancel")
            env.finalize()
            await app.resume("cancel")
            amount = (await app.market.readCredit(env.rig.actors["requester"]))["amountAtoms"]
            large = await app.submit(
                makeCommand("withdrawCredit", receiver=receiver, amountAtoms=str(2**200)), "uint256"
            )
            assert large["confirmation"] == "REJECTED"  # Encodes uint256; balance check rejects it.
            rejected = await app.submit(
                makeCommand("withdrawCredit", receiver=receiver, amountAtoms=amount),
                "receiver-rejects",
            )
            assert rejected["confirmation"] == "REJECTED"
            assert (await app.market.readCredit(env.rig.actors["requester"]))[
                "amountAtoms"
            ] == amount
            withdrawal = makeCommand(
                "withdrawCredit", receiver=env.rig.actors["receiver"], amountAtoms=amount
            )
            env.transport.call = loseSend
            sent = await app.submit(withdrawal, "lost-withdraw")
            env.transport.call = original
            env.finalize()
            # A read verifies finality/effects but does not reconcile or send bytes.
            originalCount = len(app.journal.nativeOperations())
            readonly = await app.readOperation("lost-withdraw")
            assert readonly["confirmation"] == "FINALIZED_APPLIED" and readonly["result"] is None
            assert len(app.journal.nativeOperations()) == originalCount
            recovered = await app.submit(withdrawal, "lost-withdraw")
            assert recovered["transactionHash"] == sent["transactionHash"]
            assert (await inspector.readCredit(env.rig.actors["requester"]))["data"]["credit"][
                "amountAtoms"
            ] == "0"
        finally:
            app.journal.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testPublicMetadataFailureRetainsFrozenBid(tmp_path):
    from tests.layer6_market_support import settleFixture
    from tests.layer7_support import submit

    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            task = submit(env, worker)
            originalView = await env.market.readTask(task["taskRef"])
            settleFixture(env, task, originalView)
            env.rig.transfer(0)
            env.finalize()
            inspector = Inspector(
                env.market, env.watcher, worker.participant.content, networkScope="LOCAL_FIXTURE"
            )
            agent = validateOutput(
                await inspector.readAgentDetails(env.rig.agentRef(0), task["taskRef"])
            )
            assert agent["data"]["counters"] == {"successes": "1", "failures": "0"}
            assert agent["data"]["frozen"]["bid"]["counters"] == {"successes": "0", "failures": "0"}
            assert agent["data"]["identity"]["owner"] == env.rig.actors["other"]
            assert agent["data"]["frozen"]["bid"]["ownerAtBid"] == env.rig.actors["owner"]

            async def unavailable(*args, **kwargs):
                raise AdapterError("UNAVAILABLE", "Fixture outage")

            inspector.content.fetchBytes = unavailable
            details = validateOutput(
                await inspector.readTaskDetails(task["taskRef"], artifacts=True)
            )
            assert details["data"]["view"]["receipt"]["reason"] == "SUCCESS"
            assert details["data"]["feedback"]["status"] == "UNAVAILABLE"
            assert all(x["status"] == "UNAVAILABLE" for x in details["data"]["artifacts"])
            assert not details["complete"]
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testDockerCommandWithoutInputCapturesExit():
    import sys

    from modules.adapters.execution.docker import command

    async def run():
        # Real short-lived programs deliberately close stdin; there is no input to drain.
        values = await asyncio.gather(
            *(
                command(
                    [
                        sys.executable,
                        "-c",
                        "import os; os.close(0); print('done'); raise SystemExit(7)",
                    ]
                )
                for _ in range(12)
            )
        )
        assert all(
            code == 7 and output == b"done\n" and error == b"" for code, output, error in values
        )

    asyncio.run(run())


def testFinalizedRevertIsVisibleWithoutRebroadcast(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        app, inspector = requesterFor(env, worker, tmp_path)
        try:
            await app.submit(makeCommand("createTask", terms=requestTerms(env, worker)), "root")
            env.finalize()
            ref = (await app.resume("root"))["taskRef"]
            original = env.transport.call
            raced = False

            async def race(method, *args):
                nonlocal raced
                if method == "eth_sendRawTransaction" and not raced:
                    raced = True
                    env.command("submitBid", env.rig.bid(1), caller="owner")
                return await original(method, *args)

            env.transport.call = race
            await app.submit(makeCommand("cancelTask", taskRef=ref), "race")
            env.transport.call = original
            env.finalize()
            observed = await app.readOperation("race")
            assert observed["confirmation"] == "FINALIZED_REVERTED"
            with pytest.raises(AdapterError, match="Finalized failed"):
                await app.resume("race")
            observed = await app.readOperation("race")
            assert observed["result"]["state"] == "UNKNOWN"
            assert observed["diagnostic"] and observed["confirmation"] == "FINALIZED_REVERTED"
            assert (await inspector.readTaskDetails(ref))["data"]["view"]["status"] == "OPEN"
        finally:
            app.journal.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testBoundedHistoryKeepsCurrentTaskExplicitlyPartial(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        app, inspector = requesterFor(env, worker, tmp_path)
        try:
            task = worker.create()
            # Several explicit bounded scans are necessary to reach this task.
            inspector.watcher.maxBlocksPerTick = 34
            details = validateOutput(await inspector.readTaskDetails(task["taskRef"]))
            assert details["data"]["view"]["task"] == task
            assert not details["complete"]
            assert details["data"]["auction"]["status"] == "UNAVAILABLE"
            assert int(details["indexedThrough"]["blockNumber"]) < int(
                details["observedAt"]["blockNumber"]
            )
            inspector.watcher.maxBlocksPerTick = 2048
            fresh = validateOutput(await inspector.readTaskDetails(task["taskRef"]))
            assert fresh["complete"] and fresh["data"]["auction"]["status"] == "PREVIEW"
            other = worker.create()
            first = await inspector.listOpenTasks(limit=1)
            assert first["nextCursor"]
            cursor = readCursor(first["nextCursor"])
            cursor["market"] = env.rig.registry
            with pytest.raises(AdapterError, match="Cursor market"):
                await inspector.listOpenTasks(limit=1, cursor=pageCursor(cursor))
            second = validateOutput(
                await inspector.listOpenTasks(limit=1, cursor=first["nextCursor"])
            )
            assert second["data"]["tasks"] == [other]
        finally:
            app.journal.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
