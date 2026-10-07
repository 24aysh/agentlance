"""Real Envio/Postgres/Hasura queries agree with the authoritative direct watcher."""

import asyncio

import httpx
import pytest

from modules.adapters.chain.index import IndexedMarket
from modules.agent_client.discovery import pageCursor, readCursor
from modules.agent_client.ports import AdapterError
from tests.layer3_support import localNode
from tests.layer4_index_support import LocalIndex
from tests.layer4_support import Layer4Rig

pytestmark = [pytest.mark.l3socket, pytest.mark.l4index]


def testEnvioGraphqlReplayPaginationOutage(tmp_path):
    async def run(rpc):
        env = Layer4Rig(rpc, tmp_path)
        try:
            tasks = [env.create() for _ in range(3)]
            with LocalIndex(env, tmp_path) as service:
                await service.ready()
                async with httpx.AsyncClient(
                    headers={"X-Hasura-Admin-Secret": "testing"}, trust_env=False
                ) as http:
                    index = IndexedMarket(env.chain, http, service.endpoint)
                    await env.watcher.scanMarket()
                    expected = await env.watcher.listOpenTasks()
                    page = await service.catchUp(
                        index, int(expected["indexedThrough"]["blockNumber"])
                    )
                    assert page == expected
                    tampered = IndexedMarket(env.chain, http, service.endpoint)

                    async def forgedLog(query, variables=None):
                        data = await index.query(query, variables)
                        if "query Logs" in query:
                            data["MarketLog"][0]["logIndex"] = str(
                                int(data["MarketLog"][0]["logIndex"]) + 1
                            )
                        return data

                    tampered.query = forgedLog
                    with pytest.raises(AdapterError, match="Indexed creation log absent"):
                        await tampered.listOpenTasks(limit=1)
                    first = await index.listOpenTasks(limit=1)
                    env.command("cancelTask", {"taskRef": tasks[0]["taskRef"]})
                    env.rig.advance(int(tasks[1]["terms"]["biddingClose"]))
                    env.command("allocateTask", {"taskRef": tasks[1]["taskRef"]})
                    await env.watcher.scanMarket()
                    expected = await env.watcher.listOpenTasks()
                    await service.catchUp(index, int(expected["indexedThrough"]["blockNumber"]))
                    assert (await index.listOpenTasks())["tasks"] == expected["tasks"] == tasks[2:]
                    assert (await index.listOpenTasks(cursor=first["nextCursor"]))[
                        "tasks"
                    ] == tasks[1:]
                    env.rig.advance(int(tasks[2]["terms"]["allocationBy"]))
                    env.command("expireTask", {"taskRef": tasks[2]["taskRef"]})
                    await env.watcher.scanMarket()
                    expected = await env.watcher.listOpenTasks()
                    # Child discovery and late child closure are the same public event stream.
                    parent = env.create(budget=200, denominator=200, delegation=True)
                    ref = parent["taskRef"]
                    env.command("submitBid", env.rig.bid(int(ref["taskId"])), caller="owner")
                    env.rig.advance(int(parent["terms"]["biddingClose"]))
                    env.command("allocateTask", {"taskRef": ref})
                    env.command(
                        "acceptAward",
                        {
                            "executionRef": {"taskRef": ref, "awardId": 1},
                            "ownWorkReserveAtoms": "20",
                        },
                        caller="signer",
                    )
                    childTerms = env.rig.terms(budget=60, denominator=80)
                    childTerms.update(
                        refundAddress=env.rig.actors["signer"],
                        delegation={"maxDepth": 1, "maxChildren": 0},
                    )
                    childReceipt = env.command(
                        "createChildTask",
                        {"parentRef": ref, "terms": childTerms},
                        caller="signer",
                        value=60,
                    )
                    child = env.rig.codec.event(childReceipt["logs"][0])["payload"]["task"]
                    await env.watcher.scanMarket()
                    expected = await env.watcher.listOpenTasks()
                    await service.catchUp(index, int(expected["indexedThrough"]["blockNumber"]))
                    assert (await index.listOpenTasks("CHILD", ref))["tasks"] == [child]
                    assert (await index.listOpenTasks("ROOT"))["tasks"] == []
                    env.rig.advance(int(parent["terms"]["resultBy"]))
                    env.command("expireTask", {"taskRef": ref})
                    before = (await env.market.readSettlement(ref))["receipt"]
                    env.command("expireTask", {"taskRef": child["taskRef"]})
                    assert (await env.market.readTask(ref))["receipt"] == before
                    assert (await env.market.readTask(ref))["activeChildren"] == 0
                    await env.watcher.scanMarket()
                    expected = await env.watcher.listOpenTasks()
                    bad = readCursor(first["nextCursor"])
                    bad["stamp"]["blockHash"] = "0x" + "ff" * 32
                    for query in (index, env.watcher):
                        with pytest.raises(AdapterError, match="CONFLICT"):
                            await query.listOpenTasks(cursor=pageCursor(bad))
                    assert not env.journal.halted()
                    service.start()
                    assert (
                        await service.catchUp(index, int(expected["indexedThrough"]["blockNumber"]))
                    )["tasks"] == expected["tasks"]
                    service.start(rebuild=True)
                    assert (
                        await service.catchUp(index, int(expected["indexedThrough"]["blockNumber"]))
                    )["tasks"] == expected["tasks"]
                    unavailable = IndexedMarket(env.chain, http, "http://127.0.0.1:1/v1/graphql")
                    with pytest.raises(AdapterError, match="UNAVAILABLE"):
                        await unavailable.listOpenTasks()
                    assert (await env.market.readTask(tasks[0]["taskRef"]))["receipt"][
                        "reason"
                    ] == "CANCELLED"
                    assert (await env.market.readTask(tasks[1]["taskRef"]))["receipt"][
                        "reason"
                    ] == "UNALLOCATED"
        finally:
            await env.close()

    with localNode(tmp_path) as rpc:
        asyncio.run(run(rpc))
