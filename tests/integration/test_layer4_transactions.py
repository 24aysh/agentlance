"""Production Web3 adapters against funded isolated Monad Anvil transactions."""

import asyncio

import pytest

from modules.agent_client.participant import makeCommand
from modules.agent_client.ports import AdapterError
from tests.layer3_support import localNode
from tests.layer4_support import Layer4Rig

pytestmark = pytest.mark.l3socket


def testDirectReadSubmitReplayRestart(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            task = env.create()
            ref = task["taskRef"]
            identity = await env.registry.readIdentity(env.rig.agentRef(0))
            assert identity["owner"] == env.rig.actors["owner"]
            assert identity["registrationUri"] == "https://agent.example/registration.json"
            await env.watcher.scanRegistry()
            assert (
                env.journal.db.execute("SELECT count(*) FROM discovered_agents").fetchone()[0] == 1
            )
            await env.watcher.scanMarket()
            assert (await env.watcher.listOpenTasks())["tasks"] == [task]
            assert (await env.market.readTask(ref))["status"] == "OPEN"
            bid = makeCommand("submitBid", **env.rig.bid(int(ref["taskId"]), signed=True))
            result = await env.applied(await env.market.submitSignedBid(bid, "bid-1"))
            assert result["state"] == "APPLIED"
            txHash = env.journal.nativeOperation("bid-1")["transactionHash"]
            await env.restart()
            assert await env.market.submitSignedBid(bid, "bid-1") == result
            assert env.journal.nativeOperation("bid-1")["transactionHash"] == txHash
            env.rig.advance(int(task["terms"]["biddingClose"]))
            env.command("allocateTask", {"taskRef": ref})
            execution = {"taskRef": ref, "awardId": 1}
            accepted = await env.applied(
                await env.market.acceptAward(
                    makeCommand("acceptAward", executionRef=execution, ownWorkReserveAtoms="0"),
                    "accept-1",
                )
            )
            assert accepted["state"] == "APPLIED"
            committed = await env.applied(
                await env.market.commitResult(
                    makeCommand(
                        "submitResult", executionRef=execution, artifact=task["terms"]["input"]
                    ),
                    "result-1",
                )
            )
            assert committed["state"] == "APPLIED"
            assert (await env.market.readSettlement(ref))["receipt"] is None
            await env.watcher.scanMarket()
            assert (await env.watcher.listOpenTasks())["tasks"] == []
            count = env.journal.db.execute("SELECT count(*) FROM observed_logs").fetchone()[0]
            await env.restart()
            await env.watcher.scanMarket()
            assert (
                env.journal.db.execute("SELECT count(*) FROM observed_logs").fetchone()[0] == count
            )
            assert (await env.market.readTask(ref))["status"] == "SUBMITTED"
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testFundedChildAndLateTerminalParentRefresh(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            parent = env.create(budget=200, denominator=200, delegation=True)
            ref = parent["taskRef"]
            env.command("submitBid", env.rig.bid(int(ref["taskId"])), caller="owner")
            env.rig.advance(int(parent["terms"]["biddingClose"]))
            env.command("allocateTask", {"taskRef": ref})
            await env.applied(
                await env.market.acceptAward(
                    makeCommand(
                        "acceptAward",
                        executionRef={"taskRef": ref, "awardId": 1},
                        ownWorkReserveAtoms="20",
                    ),
                    "parent-accept",
                )
            )
            childTerms = env.rig.terms(budget=60, denominator=80)
            childTerms.update(
                refundAddress=env.rig.actors["signer"], delegation={"maxDepth": 1, "maxChildren": 0}
            )
            childCommand = makeCommand("createChildTask", parentRef=ref, terms=childTerms)
            with pytest.raises(AdapterError, match="funding"):
                await env.market.publishChild(childCommand, "59", "wrong-funding")
            result = await env.applied(
                await env.market.publishChild(childCommand, "60", "child-create")
            )
            assert result["state"] == "APPLIED", result
            child = result["events"][0]["payload"]["task"]
            view = await env.market.readTask(ref)
            assert (
                view["childrenCreated"],
                view["activeChildren"],
                view["reservedChildBudgets"],
            ) == (1, 1, "60")
            await env.watcher.scanMarket()
            assert (await env.watcher.listOpenTasks("CHILD", ref))["tasks"] == [child]
            env.rig.advance(int(parent["terms"]["resultBy"]))
            env.command("expireTask", {"taskRef": ref})
            receipt = (await env.market.readSettlement(ref))["receipt"]
            env.command("expireTask", {"taskRef": child["taskRef"]})
            await env.watcher.scanMarket()
            refreshed = await env.market.readTask(ref)
            assert refreshed["receipt"] == receipt
            assert (refreshed["activeChildren"], refreshed["reservedChildBudgets"]) == (0, "0")
            assert (await env.watcher.listOpenTasks())["tasks"] == []
            assert await env.market.publishChild(childCommand, "60", "child-create") == result
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))


def testUnallocatedAndStablePagination(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            tasks = [env.create() for _ in range(3)]
            await env.watcher.scanMarket()
            first = await env.watcher.listOpenTasks(limit=1)
            env.rig.advance(int(tasks[0]["terms"]["biddingClose"]))
            env.command("allocateTask", {"taskRef": tasks[0]["taskRef"]})
            extra = env.create()
            await env.watcher.scanMarket()
            page = await env.watcher.listOpenTasks(cursor=first["nextCursor"])
            assert page["tasks"] == tasks[1:]
            assert page["indexedThrough"] == first["indexedThrough"]
            fresh = await env.watcher.listOpenTasks()
            assert fresh["tasks"] == [*tasks[1:], extra]
            view = await env.market.readTask(tasks[0]["taskRef"])
            assert view["allocation"]["outcome"] == "UNALLOCATED"
            assert view["receipt"]["reason"] == "UNALLOCATED"
            with pytest.raises(AdapterError):
                await env.watcher.listOpenTasks("ROOT", cursor=first["nextCursor"])
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
