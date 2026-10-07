"""Finalized public discovery, resumable deliveries and private cheap filtering."""

import base64
from copy import deepcopy

from eth_utils import keccak

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.chain.codec import abiType
from modules.adapters.chain.rpc import quantity
from modules.agent_client.ports import (
    AdapterError,
    checkStamp,
    checkTaskView,
    closed,
    ensure,
    recordCheck,
)


def refKey(ref):
    return jsonBytes([ref["chainId"], ref["market"], ref["taskId"]]).decode()


def pageCursor(value):
    return base64.urlsafe_b64encode(jsonBytes(value)).decode()


def readCursor(cursor):
    ensure(isinstance(cursor, str) and len(cursor) <= 4096, "Invalid page cursor", "CONFLICT")
    try:
        return strictJson(base64.b64decode(cursor, altchars=b"-_", validate=True))
    except (ValueError, AdapterError) as error:
        raise AdapterError("CONFLICT", "Invalid page cursor") from error


def queryOptions(kind, parentRef, limit, schema):
    ensure(isinstance(kind, str) and kind in {"ALL", "ROOT", "CHILD"}, "Query kind")
    ensure(type(limit) is int and 1 <= limit <= 100, "Query limit")
    ensure(parentRef is None or kind == "CHILD", "Parent filter requires CHILD")
    if parentRef is not None:
        recordCheck(parentRef, "TaskRef", schema)


async def cursorPosition(chain, cursor, kind, parentRef, maximum):
    try:
        page = readCursor(cursor)
        closed(page, "kind parentRef stamp after")
        checkStamp(page["stamp"], chain.schema)
        recordCheck(page["after"], "Uint64", chain.schema)
        stamp = page["stamp"]
        ensure(page["kind"] == kind and page["parentRef"] == parentRef, "Cursor filters changed")
        ensure(
            stamp["chainId"] == chain.facts["chainId"]
            and stamp["source"] == "MONAD"
            and stamp["finality"] == "FINALIZED",
            "Cursor namespace",
        )
        ensure(
            int(chain.facts["deploymentBlock"]) <= int(stamp["blockNumber"]) <= maximum,
            "Cursor snapshot unavailable",
        )
        # Untrusted cursors cannot poison the persistent finalized-history halt flag.
        ensure(await chain.block(stamp["blockNumber"]) == stamp, "Cursor snapshot invalidated")
        return stamp, page["after"]
    except AdapterError as error:
        raise AdapterError("CONFLICT", "Invalid or unavailable page cursor") from error


class MarketWatcher:
    def __init__(
        self,
        chain,
        registry=None,
        *,
        rangeSize=128,
        maxLogs=2000,
        maxTasks=10000,
        maxStoredLogs=100000,
        maxBlocksPerTick=2048,
    ):
        self.chain, self.registry = chain, registry
        self.journal, self.db, self.schema = chain.journal, chain.journal.db, chain.schema
        for value in (rangeSize, maxLogs, maxTasks, maxStoredLogs, maxBlocksPerTick):
            ensure(type(value) is int and value > 0, "Watcher limits")
        self.rangeSize, self.maxLogs, self.maxTasks = rangeSize, maxLogs, maxTasks
        self.maxStoredLogs = maxStoredLogs
        self.reconsidered = False
        ensure(maxBlocksPerTick > 33, "Replay budget must exceed overlap")
        self.maxBlocksPerTick = maxBlocksPerTick
        self.marketTopics = [
            "0x" + keccak(text=entry["name"] + "(" + abiType(entry["inputs"][0]) + ")").hex()
            for entry in chain.codec.abi
            if entry["type"] == "event"
        ]

    def progress(self, stream="market"):
        row = self.db.execute("SELECT body FROM streams WHERE name=?", (stream,)).fetchone()
        return strictJson(row[0]) if row else None

    async def scanMarket(self):
        head = await self.chain.qualify()
        return await self.scan(
            "market",
            self.chain.facts["market"],
            int(self.chain.facts["deploymentBlock"]),
            self.marketTopics,
            head,
        )

    async def scanRegistry(self):
        ensure(self.registry is not None, "No registry adapter", "UNAVAILABLE")
        head = await self.chain.qualify()
        await self.registry.verifyDependencies(head)
        start = int(self.registry.verification["deploymentBlock"])
        return await self.scan(
            "registry",
            self.chain.facts["identityRegistry"],
            start,
            list(self.registry.events),
            head,
        )

    async def scan(self, stream, address, start, topics, head):
        previous = self.progress(stream)
        if previous:
            ensure(
                previous["startBlock"] == str(start) and previous["address"] == address,
                "Observation stream configuration changed",
                "CONFLICT",
            )
            await self.chain.checkCanonical(previous["stamp"])
        first = max(start, int(previous["stamp"]["blockNumber"]) - 32) if previous else start
        end, size = min(int(head["blockNumber"]), first + self.maxBlocksPerTick - 1), self.rangeSize
        while first <= end:
            last = min(end, first + size - 1)
            try:
                logs = await self.chain.rpc.call(
                    "eth_getLogs",
                    {
                        "address": address,
                        "fromBlock": hex(first),
                        "toBlock": hex(last),
                        "topics": [topics],
                    },
                )
                ensure(isinstance(logs, list), "Malformed log response")
                ensure(len(logs) <= self.maxLogs, "Log response limit", "UNAVAILABLE")
            except AdapterError as error:
                if error.kind != "UNAVAILABLE" or size == 1:
                    raise
                size = max(1, size // 2)
                continue
            byBlock = {height: [] for height in range(first, last + 1)}
            for log in logs:
                ensure(
                    isinstance(log, dict) and first <= quantity(log["blockNumber"]) <= last,
                    "Log outside requested range",
                )
                ensure(
                    log["address"].lower() == address
                    and log["topics"]
                    and log["topics"][0] in topics,
                    "Foreign log",
                )
                if log.get("removed"):
                    # A finalized-only request cannot legitimately contain a removed log.
                    self.journal.halt("Removed log in finalized history")
                byBlock[quantity(log["blockNumber"])].append(log)
            for height, blockLogs in byBlock.items():
                stamp = await self.chain.block(height)
                ordered = sorted(blockLogs, key=lambda log: quantity(log["logIndex"]))
                verified = []
                receipts = {}
                for log in ordered:
                    ensure(log["blockHash"] == stamp["blockHash"], "Log block mismatch")
                    txHash = log["transactionHash"]
                    recordCheck(txHash, "Digest", self.schema)
                    if txHash not in receipts:
                        receipts[txHash] = await self.chain.rpc.call(
                            "eth_getTransactionReceipt", txHash
                        )
                    receipt = receipts[txHash]
                    ensure(
                        isinstance(receipt, dict)
                        and receipt["transactionHash"] == txHash
                        and receipt["blockHash"] == stamp["blockHash"]
                        and quantity(receipt["status"]) == 1,
                        "Log receipt mismatch",
                    )
                    ensure(
                        any(
                            all(
                                item.get(key) == log.get(key)
                                for key in (
                                    "address",
                                    "topics",
                                    "data",
                                    "blockHash",
                                    "blockNumber",
                                    "transactionHash",
                                    "logIndex",
                                )
                            )
                            for item in receipt["logs"]
                        ),
                        "Log missing from receipt",
                    )
                    if stream == "market":
                        event = self.chain.codec.event(log)
                        value = {
                            "chainId": self.chain.facts["chainId"],
                            "market": address,
                            "blockNumber": str(height),
                            "blockHash": stamp["blockHash"],
                            "transactionHash": txHash,
                            "logIndex": str(quantity(log["logIndex"])),
                            "event": event,
                        }
                        recordCheck(value, "EventEnvelope", self.schema)
                    else:
                        value = {
                            "agentRef": self.registry.decodeLog(log),
                            "stamp": stamp,
                            "transactionHash": txHash,
                            "logIndex": str(quantity(log["logIndex"])),
                        }
                    verified.append(value)
                await self.chain.checkCanonical(head)
                self.journal.observe(stamp)
                try:
                    self.commitBlock(stream, address, start, stamp, verified)
                except AdapterError as error:
                    if error.kind == "FINALITY_CONFLICT":
                        self.journal.halt(error.detail)
                    raise
            first = last + 1
        return self.progress(stream)

    def commitBlock(self, stream, address, start, stamp, logs):
        with self.db:
            for value in logs:
                key = (stream, stamp["blockHash"], value["logIndex"])
                old = self.db.execute(
                    "SELECT body FROM observed_logs WHERE stream=? AND hash=? AND position=?", key
                ).fetchone()
                if old:
                    ensure(
                        strictJson(old[0]) == value,
                        "Conflicting duplicate log",
                        "FINALITY_CONFLICT",
                    )
                    continue
                ensure(
                    self.db.execute("SELECT count(*) FROM observed_logs").fetchone()[0]
                    < self.maxStoredLogs,
                    "Observation storage capacity",
                    "UNAVAILABLE",
                )
                self.db.execute(
                    "INSERT INTO observed_logs VALUES (?,?,?,?)", (*key, jsonBytes(value))
                )
                if stream == "market":
                    self.project(value, stamp)
                else:
                    key = jsonBytes(
                        [
                            value["agentRef"][name]
                            for name in ("chainId", "identityRegistry", "agentId")
                        ]
                    ).decode()
                    known = self.db.execute(
                        "SELECT 1 FROM discovered_agents WHERE key=?", (key,)
                    ).fetchone()
                    ensure(
                        known
                        or self.db.execute("SELECT count(*) FROM discovered_agents").fetchone()[0]
                        < self.maxTasks,
                        "Registry observation capacity",
                        "UNAVAILABLE",
                    )
                    self.db.execute(
                        "INSERT INTO discovered_agents VALUES (?,?) "
                        "ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                        (key, jsonBytes(value)),
                    )
            previous = self.progress(stream)
            if previous is None or int(stamp["blockNumber"]) > int(
                previous["stamp"]["blockNumber"]
            ):
                progress = {
                    "chainId": self.chain.facts["chainId"],
                    "address": address,
                    "startBlock": str(start),
                    "stamp": stamp,
                    "transactionHash": logs[-1]["transactionHash"] if logs else None,
                    "logIndex": logs[-1]["logIndex"] if logs else None,
                }
                self.db.execute(
                    "INSERT INTO streams VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET body=excluded.body",
                    (stream, jsonBytes(progress)),
                )

    def project(self, envelope, stamp):
        event = envelope["event"]
        name, payload = event["name"], event["payload"]
        if name in {"BidAccepted", "CreditWithdrawn"}:
            return
        if name == "TaskCreated":
            task = payload["task"]
            key = refKey(task["taskRef"])
            ensure(
                self.db.execute("SELECT count(DISTINCT key) FROM task_versions").fetchone()[0]
                < self.maxTasks,
                "Task observation capacity",
                "UNAVAILABLE",
            )
            row = {"task": task, "status": "OPEN", "envelope": envelope}
        else:
            if name == "TaskAwarded":
                ref = payload["allocation"]["taskRef"]
            elif name == "TaskSettled":
                ref = payload["receipt"]["taskRef"]
            elif name == "ResultSubmitted":
                ref = payload["result"]["executionRef"]["taskRef"]
            else:
                ref = payload["executionRef"]["taskRef"]
            key = refKey(ref)
            old = self.db.execute(
                "SELECT body FROM task_versions WHERE key=? ORDER BY height DESC LIMIT 1", (key,)
            ).fetchone()
            ensure(old is not None, "Task event without creation")
            row = strictJson(old[0])
            row.update(
                status={
                    "TaskAwarded": "AWARDED",
                    "AwardAccepted": "RUNNING",
                    "ResultSubmitted": "SUBMITTED",
                    "TaskSettled": "SETTLED",
                }[name],
                envelope=envelope,
            )
        self.db.execute(
            "INSERT INTO task_versions VALUES (?,?,?) "
            "ON CONFLICT(key,height) DO UPDATE SET body=excluded.body",
            (key, stamp["blockNumber"].zfill(20), jsonBytes(row)),
        )
        if name in {"TaskCreated", "TaskAwarded"}:
            deliveryKey = ("task:" if name == "TaskCreated" else "award:") + key
            body = {
                "kind": "TASK" if name == "TaskCreated" else "AWARD",
                "taskRef": row["task"]["taskRef"],
                "pending": True,
                "policyVersion": None,
                "capacityVersion": None,
            }
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?,?)", (deliveryKey, jsonBytes(body))
            )

    def taskRows(self, height):
        rows = {}
        for key, raw in self.db.execute(
            "SELECT key,body FROM task_versions WHERE height<=? ORDER BY height",
            (str(height).zfill(20),),
        ):
            rows[key] = strictJson(raw)
        return sorted(rows.values(), key=lambda row: int(row["task"]["taskRef"]["taskId"]))

    async def listOpenTasks(self, kind="ALL", parentRef=None, cursor=None, limit=100):
        queryOptions(kind, parentRef, limit, self.schema)
        if parentRef is not None:
            ensure(
                all(parentRef[name] == self.chain.facts[name] for name in ("chainId", "market")),
                "Foreign query parent",
            )
        head = await self.chain.qualify()
        progress = self.progress()
        ensure(progress is not None, "Market history not indexed", "UNAVAILABLE")
        stamp, after = progress["stamp"], 0
        if cursor is not None:
            stamp, after = await cursorPosition(
                self.chain, cursor, kind, parentRef, int(progress["stamp"]["blockNumber"])
            )
            after = int(after)
        await self.chain.checkCanonical(stamp)
        tasks = [
            row["task"]
            for row in self.taskRows(stamp["blockNumber"])
            if row["status"] == "OPEN"
            and int(row["task"]["taskRef"]["taskId"]) > after
            and (kind == "ALL" or (row["task"]["parentRef"] is None) == (kind == "ROOT"))
            and (parentRef is None or row["task"]["parentRef"] == parentRef)
        ]
        selected = tasks[:limit]
        nextCursor = (
            pageCursor(
                {
                    "kind": kind,
                    "parentRef": parentRef,
                    "stamp": stamp,
                    "after": selected[-1]["taskRef"]["taskId"],
                }
            )
            if len(tasks) > limit
            else None
        )
        return {
            "tasks": selected,
            "indexedThrough": stamp,
            "finalizedHead": head,
            "nextCursor": nextCursor,
        }

    def pendingDeliveries(self):
        return [
            (key, strictJson(body))
            for key, body in self.db.execute("SELECT key,body FROM deliveries")
            if strictJson(body)["pending"]
        ]

    def acknowledge(self, key, delivery):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET body=? WHERE key=?",
                (jsonBytes(delivery | {"pending": False}), key),
            )

    def reconsider(self, policyVersion, capacityVersion):
        with self.db:
            for key, raw in self.db.execute("SELECT key,body FROM deliveries").fetchall():
                row = strictJson(raw)
                if row["kind"] == "TASK" and (
                    not self.reconsidered
                    or (row["policyVersion"], row["capacityVersion"])
                    != (policyVersion, capacityVersion)
                ):
                    row.update(
                        pending=True, policyVersion=policyVersion, capacityVersion=capacityVersion
                    )
                    self.db.execute(
                        "UPDATE deliveries SET body=? WHERE key=?", (jsonBytes(row), key)
                    )
        self.reconsidered = True


def checkFilterPolicy(policy, schema):
    closed(
        policy,
        "version supportedDigests requiredSkills advertisedSkills minBudgetAtoms "
        "maxBudgetAtoms leadTimeSeconds denyRequesters allowRequesters",
    )
    ensure(
        isinstance(policy["version"], str) and 0 < len(policy["version"]) <= 128, "Policy version"
    )
    ensure(type(policy["leadTimeSeconds"]) is int and policy["leadTimeSeconds"] >= 0, "Lead time")
    ensure(
        isinstance(policy["supportedDigests"], list) and len(policy["supportedDigests"]) == 2,
        "Supported digests",
    )
    for value in policy["supportedDigests"]:
        recordCheck(value, "Digest", schema)
    for name in ("minBudgetAtoms", "maxBudgetAtoms"):
        recordCheck(policy[name], "Uint96", schema)
    ensure(int(policy["minBudgetAtoms"]) <= int(policy["maxBudgetAtoms"]), "Budget bounds")
    for name in ("requiredSkills", "advertisedSkills", "denyRequesters", "allowRequesters"):
        ensure(isinstance(policy[name], list) and len(policy[name]) <= 1000, "Policy list limit")
        ensure(
            all(isinstance(value, str) and 0 < len(value) <= 64 for value in policy[name]),
            "Policy item",
        )
        ensure(len(set(policy[name])) == len(policy[name]), "Duplicate policy item")
    for address in policy["denyRequesters"] + policy["allowRequesters"]:
        recordCheck(address, "Address", schema)


def filterTask(view, policy, capacity, schema):
    checkFilterPolicy(policy, schema)
    ensure(capacity is None or (type(capacity) is int and capacity >= 0), "Capacity units")
    checkTaskView(view, schema)
    terms = view["task"]["terms"]

    def result(state, reason):
        return {"state": state, "reason": reason}

    if view["stamp"]["finality"] != "FINALIZED":
        return result("WAIT", "FINALITY")
    if view["status"] != "OPEN":
        return result("IGNORE", "CLOSED")
    if (
        int(terms["biddingClose"]) - int(view["stamp"]["blockTimestamp"])
        <= policy["leadTimeSeconds"]
    ):
        return result("IGNORE", "DEADLINE")
    if terms["asset"] != {"kind": "NATIVE", "symbol": "MON", "decimals": 18}:
        return result("IGNORE", "ASSET")
    if (
        terms["taskFamily"] != "structured-output-v1"
        or [terms["outputSchema"]["digest"], terms["validationPolicy"]["digest"]]
        != policy["supportedDigests"]
    ):
        return result("IGNORE", "TEMPLATE")
    if not policy["requiredSkills"] or not set(policy["requiredSkills"]) <= set(
        policy["advertisedSkills"]
    ):
        return result("IGNORE", "CAPABILITY")
    if (
        not int(policy["minBudgetAtoms"])
        <= int(terms["budgetAtoms"])
        <= int(policy["maxBudgetAtoms"])
    ):
        return result("IGNORE", "BUDGET")
    if view["task"]["requester"] in policy["denyRequesters"] or (
        policy["allowRequesters"] and view["task"]["requester"] not in policy["allowRequesters"]
    ):
        return result("IGNORE", "POLICY")
    if capacity is None or capacity <= 0:
        return result("WAIT", "CAPACITY")
    return result("ELIGIBLE", "SUPPORTED")


async def deliverObserved(watcher, participant, policy, capacity, onCandidate):
    watcher.reconsider(policy["version"], str(capacity))
    for key, delivery in watcher.pendingDeliveries():
        try:
            view = await participant.market.readTask(delivery["taskRef"])
            if delivery["kind"] == "AWARD":
                bid = view["winningBid"]
                if (
                    view["status"] in {"AWARDED", "RUNNING"}
                    and int(view["stamp"]["blockTimestamp"])
                    < int(
                        view["task"]["terms"][
                            "acceptBy" if view["status"] == "AWARDED" else "resultBy"
                        ]
                    )
                    and bid is not None
                    and bid["offer"]["agentRef"] == participant.agentRef
                    and bid["offer"]["executionSigner"] == participant.signer
                ):
                    await participant.observeOwnAward(
                        {"taskRef": delivery["taskRef"], "awardId": view["allocation"]["awardId"]}
                    )
            else:
                decision = filterTask(view, policy, capacity, watcher.schema)
                if decision["state"] == "WAIT":
                    continue
                if decision["state"] == "ELIGIBLE":
                    await onCandidate(deepcopy(view["task"]), view["stamp"])
            watcher.acknowledge(key, delivery)
        except AdapterError as error:
            if error.kind in {"FINALITY_CONFLICT", "INVALID_DATA"}:
                raise
            # Retain the delivery for a later bounded tick; never mark operational failure DONE.
            continue
