"""On-chain registry, native submission, observation and immutable intent boundaries."""

import asyncio
from copy import deepcopy

import httpx
import pytest

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.chain.index import IndexedMarket
from modules.adapters.chain.rpc import RpcError
from modules.agent_client.participant import makeCommand
from modules.agent_client.ports import AdapterError
from modules.agent_client.signing import verifyContractReturn
from tests.layer3_support import localNode
from tests.layer4_support import DiscoveredWorker, Layer4Rig

pytestmark = pytest.mark.l3socket


def testRegistryStaticSignatureTransferAndOutage(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            owner = env.rig.deploy("SignatureOwner", sourceName="HostileActors")
            digest = "0x" + "ab" * 32
            for mode in (0, 1, 2, 3, 4, 6, 7):
                env.rig.external(
                    owner, "configure", ["uint256", "bytes32"], [mode, bytes.fromhex(digest[2:])]
                )
                env.finalize()
                stamp = await env.chain.qualify()
                if mode == 3:
                    with pytest.raises(AdapterError, match="Oversized"):
                        await env.registry.checkContractSignature(
                            owner, digest, "0x" + "11" * 65, stamp
                        )
                    continue
                result = await env.registry.checkContractSignature(
                    owner, digest, "0x" + "11" * 65, stamp
                )
                assert verifyContractReturn(result) == (mode == 0), (mode, result)
            with pytest.raises(AdapterError, match="namespace"):
                await env.registry.checkContractSignature(
                    owner, digest, "0x" + "11" * 65, stamp | {"chainId": "1"}
                )
            assert not env.journal.halted()
            task = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(task["taskRef"]["taskId"]), signed=True)
            )
            assert (await env.applied(await env.market.submitSignedBid(command, "bid")))[
                "state"
            ] == "APPLIED"
            pinned = (await env.market.readBid(task["taskRef"], env.rig.agentRef(0)))["bid"]
            env.rig.external(
                env.rig.registry,
                "transferIdentity",
                ["uint256", "address"],
                [0, env.rig.actors["other"]],
                caller="owner",
            )
            env.finalize()
            identity = await env.registry.readIdentity(env.rig.agentRef(0))
            assert (
                identity["verifiedWallet"] is None and identity["owner"] == env.rig.actors["other"]
            )
            env.rig.external(env.rig.registry, "setUnavailable", ["bool"], [True])
            env.finalize()
            with pytest.raises(AdapterError):
                await env.registry.readIdentity(env.rig.agentRef(0))
            env.rig.advance(int(task["terms"]["biddingClose"]))
            env.command("allocateTask", {"taskRef": task["taskRef"]})
            result = await env.applied(
                await env.market.acceptAward(
                    makeCommand(
                        "acceptAward",
                        executionRef={"taskRef": task["taskRef"], "awardId": 1},
                        ownWorkReserveAtoms="0",
                    ),
                    "accept",
                )
            )
            assert result["state"] == "APPLIED"
            assert (await env.market.readTask(task["taskRef"]))["winningBid"] == pinned
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


@pytest.mark.parametrize(
    "stage",
    [
        "projection-before-commit",
        "cursor-after-commit",
        "delivery-before-ack",
        "delivery-after-ack",
    ],
)
def testObservationCrashReplaysWithoutDuplicateBid(tmp_path, monkeypatch, stage):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        try:
            task = worker.create()
            original = (
                env.watcher.project
                if stage == "projection-before-commit"
                else env.watcher.commitBlock
                if stage == "cursor-after-commit"
                else env.watcher.acknowledge
            )
            crashed = []

            def fail(*args):
                if stage != "delivery-before-ack":
                    result = original(*args)
                if not crashed and (stage != "cursor-after-commit" or args[-1]):
                    crashed.append(True)
                    raise RuntimeError("power loss")
                return result if stage != "delivery-before-ack" else original(*args)

            method = (
                "project"
                if stage == "projection-before-commit"
                else "commitBlock"
                if stage == "cursor-after-commit"
                else "acknowledge"
            )
            monkeypatch.setattr(env.watcher, method, fail)
            with pytest.raises(RuntimeError, match="power loss"):
                await worker.runtime.tick()
            if stage == "projection-before-commit":
                assert (
                    env.journal.db.execute("SELECT count(*) FROM observed_logs").fetchone()[0] == 0
                )
                assert env.journal.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
            await env.restart()
            worker.open()
            await worker.runtime.tick()
            for op in env.journal.nativeOperations():
                assert (await env.applied(await env.market.readOperation(op["operationId"])))[
                    "state"
                ] == "APPLIED"
            await worker.runtime.tick()
            assert len(env.journal.nativeOperations()) == 1
            assert (await env.market.readBid(task["taskRef"], env.rig.agentRef(0)))[
                "bid"
            ] is not None
            assert worker.calls == []
        finally:
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testConcurrentIntentUnknownRevertAndFailedReceipt(tmp_path, monkeypatch):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(task["taskRef"]["taskId"]), signed=True)
            )
            original = env.transport.call

            async def unavailable(method, *args):
                if method == "eth_estimateGas":
                    raise RpcError({"code": 3, "data": "0x", "message": "execution reverted"})
                return await original(method, *args)

            monkeypatch.setattr(env.transport, "call", unavailable)
            with pytest.raises(AdapterError, match="UNAVAILABLE"):
                await env.market.submitSignedBid(command, "native")
            assert env.journal.nativeOperation("native")["result"] is None
            monkeypatch.setattr(env.transport, "call", original)
            results = await asyncio.gather(
                *(env.market.submitSignedBid(command, "native") for _ in range(3))
            )
            assert all(result["state"] == "PENDING" for result in results)
            assert env.journal.nativeOperation("native")["attempts"] == 1
            assert (await env.applied(results[0]))["state"] == "APPLIED"
            other = deepcopy(command)
            other["input"]["offer"]["bidAtoms"] = "21"
            with pytest.raises(AdapterError, match="CONFLICT"):
                await env.market.submitSignedBid(other, "native")
            second = env.create()
            command = makeCommand(
                "submitBid", **env.rig.bid(int(second["taskRef"]["taskId"]), signed=True, nonce=2)
            )
            save = env.journal.saveNative

            def crash(op):
                save(op)
                if op["transactionHash"]:
                    raise RuntimeError("power loss")

            monkeypatch.setattr(env.journal, "saveNative", crash)
            with pytest.raises(RuntimeError):
                await env.market.submitSignedBid(command, "failed")
            raw = env.journal.nativeOperation("failed")["raw"]
            env.rig.advance(int(second["terms"]["biddingClose"]))
            txHash = rpc.call("eth_sendRawTransaction", raw)
            assert rpc.receipt(txHash)["status"] == "0x0"
            env.finalize()
            await env.restart()
            with pytest.raises(AdapterError, match="Finalized failed transaction"):
                await env.market.readOperation("failed")
            op = env.journal.nativeOperation("failed")
            assert op["result"]["state"] == "UNKNOWN" and op["result"]["error"] is None
            await env.restart()
            with pytest.raises(AdapterError):
                await env.market.readOperation("failed")
            assert env.journal.nativeOperation("failed")["attempts"] == 0
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testWatcherDuplicatesAdaptiveRangesAndBackpressure(tmp_path, monkeypatch):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            tasks = [env.create(), env.create()]
            env.watcher.maxTasks = 1
            original = env.transport.call
            ranges = []

            async def bounded(method, *params):
                if method == "eth_getLogs":
                    size = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
                    ranges.append(size)
                    if size > 32:
                        raise AdapterError("UNAVAILABLE", "Provider range cap")
                    result = await original(method, *params)
                    return list(reversed(result + result))
                return await original(method, *params)

            monkeypatch.setattr(env.transport, "call", bounded)
            with pytest.raises(AdapterError, match="capacity"):
                await env.watcher.scanMarket()
            assert max(ranges) > 32 and min(ranges) <= 32
            progress = env.watcher.progress()
            assert (await env.watcher.listOpenTasks())["tasks"] == tasks[:1]
            assert len(env.watcher.pendingDeliveries()) == 1
            await env.restart()
            await env.watcher.scanMarket()
            assert int(env.watcher.progress()["stamp"]["blockNumber"]) > int(
                progress["stamp"]["blockNumber"]
            )
            assert (await env.watcher.listOpenTasks())["tasks"] == tasks
            assert len(env.watcher.pendingDeliveries()) == 2
            assert env.journal.db.execute("SELECT count(*) FROM observed_logs").fetchone()[0] == 2
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testUntrustedIndexCannotOverrideFiltersOrDirectState(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            raw = rpc.call(
                "eth_getLogs", {"address": env.rig.market, "fromBlock": "0x0", "toBlock": "latest"}
            )[0]
            log = {
                "id": "creation",
                "market": env.rig.market,
                "blockNumber": str(int(raw["blockNumber"], 16)),
                "blockHash": raw["blockHash"],
                "transactionHash": raw["transactionHash"],
                "logIndex": str(int(raw["logIndex"], 16)),
                "eventName": "TaskCreated",
                "data": raw["data"],
                "topic": raw["topics"][0],
            }
            row = {
                "taskId": task["taskRef"]["taskId"],
                "parentId": None,
                "createdBlock": log["blockNumber"],
                "closedBlock": None,
                "creationLog": log["id"],
            }
            fault = [None]

            def serve(request):
                query = strictJson(request.content)["query"]
                if fault[0] == "malformed":
                    data = {"_meta": None}
                elif "_meta" in query:
                    data = {"_meta": [{"chainId": 31337, "startBlock": 2, "progressBlock": 100000}]}
                elif "query Tasks" in query:
                    data = {"MarketTask": [row]}
                else:
                    data = {"MarketLog": [log]}
                body = (
                    b"x" * (2 * 1048576 + 1)
                    if fault[0] == "oversize"
                    else jsonBytes({"data": data})
                )
                return httpx.Response(200, stream=httpx.ByteStream(body))

            async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
                index = IndexedMarket(env.chain, http, "https://index.example/graphql")
                page = await index.listOpenTasks()
                assert page["tasks"] == [task] and page["indexedThrough"] == page["finalizedHead"]
                with pytest.raises(AdapterError, match="kind filter"):
                    await index.listOpenTasks("CHILD")
                for value in ("malformed", "oversize"):
                    fault[0] = value
                    with pytest.raises(AdapterError):
                        await index.listOpenTasks()
                assert not env.journal.halted()
                assert (await env.market.readTask(task["taskRef"]))["status"] == "OPEN"
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
