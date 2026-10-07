"""Optional HyperIndex queries, with RPC-verified watermarks and creation provenance."""

import asyncio

import httpx

from modules.adapters.a2a.profile import strictJson
from modules.adapters.chain.rpc import quantity
from modules.agent_client.discovery import cursorPosition, pageCursor, queryOptions
from modules.agent_client.ports import AdapterError, ensure, recordCheck


class IndexedMarket:
    def __init__(self, chain, http, endpoint):
        self.chain, self.http, self.endpoint = chain, http, endpoint

    async def query(self, query, variables=None):
        try:
            async with asyncio.timeout(10):
                async with self.http.stream(
                    "POST",
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    ensure(response.status_code == 200, "Index unavailable", "UNAVAILABLE")
                    ensure(
                        response.headers.get("content-encoding", "identity") == "identity",
                        "Compressed index response",
                    )
                    raw = bytearray()
                    async for chunk in response.aiter_raw():
                        # Up to 101 ABI-hex creation records, each with three legal 2 KiB URIs.
                        ensure(len(raw) + len(chunk) <= 2 * 1048576, "Index response limit")
                        raw.extend(chunk)
            body = strictJson(bytes(raw))
            ensure(
                isinstance(body, dict)
                and not body.get("errors")
                and isinstance(body.get("data"), dict),
                "Index query failed",
                "UNAVAILABLE",
            )
            return body["data"]
        except (httpx.HTTPError, TimeoutError, OSError) as error:
            raise AdapterError("UNAVAILABLE", "Index transport unavailable") from error

    async def listOpenTasks(self, kind="ALL", parentRef=None, cursor=None, limit=100):
        try:
            return await self.readPage(kind, parentRef, cursor, limit)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise AdapterError("INVALID_DATA", "Malformed index response") from error

    async def readPage(self, kind, parentRef, cursor, limit):
        queryOptions(kind, parentRef, limit, self.chain.schema)
        head = await self.chain.qualify()
        metadata = (await self.query("{ _meta { chainId startBlock progressBlock } }"))["_meta"]
        entries = [item for item in metadata if str(item["chainId"]) == self.chain.facts["chainId"]]
        ensure(len(entries) == 1, "Index chain unavailable", "UNAVAILABLE")
        meta = entries[0]
        ensure(
            type(meta["startBlock"]) is int
            and meta["startBlock"] <= int(self.chain.facts["deploymentBlock"]),
            "Incomplete indexed history",
            "UNAVAILABLE",
        )
        ensure(
            type(meta["progressBlock"]) is int
            and meta["progressBlock"] >= int(self.chain.facts["deploymentBlock"]),
            "Index catching up",
            "UNAVAILABLE",
        )
        height = min(meta["progressBlock"], int(head["blockNumber"]))
        stamp, after = await self.chain.block(height), "0"
        if cursor is not None:
            stamp, after = await cursorPosition(self.chain, cursor, kind, parentRef, height)
        self.chain.journal.observe(stamp)
        filters = {
            "chainId": {"_eq": int(self.chain.facts["chainId"])},
            "market": {"_eq": self.chain.facts["market"]},
            "taskId": {"_gt": after},
            "createdBlock": {"_lte": stamp["blockNumber"]},
            "_or": [
                {"closedBlock": {"_is_null": True}},
                {"closedBlock": {"_gt": stamp["blockNumber"]}},
            ],
        }
        if kind != "ALL":
            filters["parentId"] = {"_is_null": kind == "ROOT"}
        if parentRef is not None:
            ensure(
                parentRef["chainId"] == self.chain.facts["chainId"]
                and parentRef["market"] == self.chain.facts["market"],
                "Foreign query parent",
            )
            filters["parentId"] = {"_eq": parentRef["taskId"]}
        data = await self.query(
            "query Tasks($where: MarketTask_bool_exp!, $limit: Int!) { "
            "MarketTask(where: $where, order_by: {taskId: asc}, limit: $limit) { "
            "taskId parentId createdBlock closedBlock creationLog } }",
            {"where": filters, "limit": limit + 1},
        )
        rows = data["MarketTask"]
        ensure(isinstance(rows, list) and len(rows) <= limit + 1, "Index page size")
        logs = []
        if rows:
            logs = (
                await self.query(
                    "query Logs($where: MarketLog_bool_exp!) { MarketLog(where: $where) { "
                    "id market blockNumber blockHash transactionHash "
                    "logIndex data topic eventName } }",
                    {
                        "where": {
                            "chainId": filters["chainId"],
                            "id": {"_in": [row["creationLog"] for row in rows]},
                        }
                    },
                )
            )["MarketLog"]
        ensure(len(logs) == len(rows), "Missing indexed creation logs")
        byId = {log["id"]: log for log in logs}
        tasks, previous = [], int(after)
        for row in rows:
            recordCheck(str(row["taskId"]), "Uint64", self.chain.schema)
            ensure(int(row["taskId"]) > previous, "Index order/duplicate")
            previous = int(row["taskId"])
            log = byId[row["creationLog"]]
            ensure(
                log["market"] == self.chain.facts["market"] and log["eventName"] == "TaskCreated",
                "Foreign creation log",
            )
            ensure(
                int(log["blockNumber"]) == int(row["createdBlock"]) <= int(stamp["blockNumber"]),
                "Creation beyond watermark",
            )
            block = await self.chain.block(log["blockNumber"])
            ensure(block["blockHash"] == log["blockHash"], "Orphaned index entry", "CONFLICT")
            recordCheck(log["transactionHash"], "Digest", self.chain.schema)
            recordCheck(str(log["logIndex"]), "Uint64", self.chain.schema)
            receipt = await self.chain.rpc.call("eth_getTransactionReceipt", log["transactionHash"])
            ensure(
                isinstance(receipt, dict)
                and receipt.get("transactionHash", "").lower() == log["transactionHash"]
                and receipt.get("blockHash") == block["blockHash"]
                and quantity(receipt["status"]) == 1
                and isinstance(receipt.get("logs"), list)
                and any(
                    item.get("address", "").lower() == log["market"]
                    and item.get("transactionHash", "").lower() == log["transactionHash"]
                    and item.get("blockHash") == block["blockHash"]
                    and quantity(item["logIndex"]) == int(log["logIndex"])
                    and item.get("topics") == [log["topic"]]
                    and item.get("data", "").lower() == log["data"].lower()
                    for item in receipt["logs"]
                ),
                "Indexed creation log absent from canonical receipt",
            )
            event = self.chain.codec.event({"topics": [log["topic"]], "data": log["data"]})
            task = event["payload"]["task"]
            expectedRef = {
                "chainId": self.chain.facts["chainId"],
                "market": self.chain.facts["market"],
                "taskId": str(row["taskId"]),
            }
            ensure(task["taskRef"] == expectedRef, "Indexed task binding")
            ensure(
                kind == "ALL" or (task["parentRef"] is None) == (kind == "ROOT"),
                "Index ignored kind filter",
            )
            ensure(
                parentRef is None or task["parentRef"] == parentRef, "Index ignored parent filter"
            )
            expectedParent = task["parentRef"]["taskId"] if task["parentRef"] else None
            ensure(
                (None if row["parentId"] is None else str(row["parentId"])) == expectedParent,
                "Indexed parent binding",
            )
            ensure(
                row["closedBlock"] is None or int(row["closedBlock"]) > int(stamp["blockNumber"]),
                "Closed indexed candidate",
            )
            tasks.append(task)
        await self.chain.checkCanonical(stamp)
        return {
            "tasks": tasks[:limit],
            "indexedThrough": stamp,
            "finalizedHead": head,
            "nextCursor": pageCursor(
                {
                    "kind": kind,
                    "parentRef": parentRef,
                    "stamp": stamp,
                    "after": tasks[limit - 1]["taskRef"]["taskId"],
                }
            )
            if len(tasks) > limit
            else None,
        }
