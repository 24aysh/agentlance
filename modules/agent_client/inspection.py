"""Public read composition at one canonical finalized height; no transaction sends."""

from modules.adapters.a2a.profile import strictJson
from modules.adapters.registry.resolution import resolveProfile
from modules.agent_client.discovery import pageCursor, readCursor
from modules.agent_client.ports import AdapterError, closed, ensure, recordCheck
from modules.agent_client.signing import buildValidationVerdictTypedData, verifyEoa
from modules.market_core.market import calculateAllocation, calculateScore, updateTopTwo
from modules.market_core.reputation import calculateProbability


def eventRef(row):
    payload = row["event"]["payload"]
    item = next(
        (payload[k] for k in ("task", "bid", "allocation", "receipt", "result") if k in payload),
        payload,
    )
    return item.get("taskRef", item.get("executionRef", item.get("offer", {})).get("taskRef"))


class Inspector:
    def __init__(
        self, market, watcher, content, *, networkScope, publisher=None, signingTypes=None
    ):
        ensure(networkScope in {"LOCAL_FIXTURE", "QUALIFIED_TESTNET"}, "Network scope")
        self.market, self.watcher, self.content = market, watcher, content
        self.chain, self.publisher = market.chain, publisher
        self.networkScope, self.signingTypes = networkScope, signingTypes

    def envelope(self, kind, data, stamp, head=None, missing=(), cursor=None):
        progress = self.watcher.progress()
        indexed = progress["stamp"] if progress else None
        missing = list(missing)
        if kind not in {"operation", "validation"} and (
            indexed is None or int(indexed["blockNumber"]) < int(stamp["blockNumber"])
        ):
            missing.append({"component": "history", "reason": "INCOMPLETE_HISTORY"})
        head = head or stamp
        if int(stamp["blockNumber"]) < int(head["blockNumber"]):
            missing.append({"component": "freshness", "reason": "INDEX_LAG"})
        return {
            "version": 1,
            "kind": kind,
            "networkScope": self.networkScope,
            "chainId": self.chain.facts["chainId"],
            "market": self.chain.facts["market"],
            "observedAt": stamp,
            "indexedThrough": indexed,
            "finalizedHead": head,
            "complete": not missing,
            "missing": missing,
            "nextCursor": cursor,
            "data": data,
        }

    async def snapshot(self, cursor=None, query=None):
        head = await self.chain.qualify()
        missing = []
        try:
            await self.watcher.scanMarket()
        except AdapterError as error:
            if error.kind != "UNAVAILABLE":
                raise
            missing.append({"component": "history", "reason": "UNAVAILABLE"})
        progress = self.watcher.progress()
        stamp = progress["stamp"] if progress else head
        # A concurrent finalized head can advance during the bounded scan.
        head = await self.chain.qualify()
        after = 0
        if cursor:
            page = readCursor(cursor)
            closed(page, "query market stamp after")
            ensure(
                page["query"] == query and page["market"] == self.chain.facts["market"],
                "Cursor query binding",
                "CONFLICT",
            )
            candidate = page["stamp"]
            from modules.agent_client.ports import checkStamp

            checkStamp(candidate, self.market.schema)
            ensure(
                candidate["chainId"] == head["chainId"]
                and candidate["source"] == "MONAD"
                and candidate["finality"] == "FINALIZED"
                and int(self.chain.facts["deploymentBlock"])
                <= int(candidate["blockNumber"])
                <= int(stamp["blockNumber"])
                and candidate == await self.chain.block(candidate["blockNumber"]),
                "Cursor snapshot invalid",
                "CONFLICT",
            )
            ensure(
                type(page["after"]) is int and 0 <= page["after"] <= 100000,
                "Cursor position",
                "CONFLICT",
            )
            stamp, after = candidate, page["after"]
        await self.chain.checkCanonical(stamp)
        return stamp, head, missing, after

    def logs(self, stamp, ref=None):
        logs = [
            strictJson(row[0])
            for row in self.watcher.db.execute(
                "SELECT body FROM observed_logs WHERE stream='market'"
            )
        ]
        return sorted(
            (
                row
                for row in logs
                if int(row["blockNumber"]) <= int(stamp["blockNumber"])
                and (ref is None or eventRef(row) == ref)
            ),
            key=lambda row: (int(row["blockNumber"]), int(row["logIndex"])),
        )

    async def bidsAt(self, view, stamp):
        ref, rows = view["task"]["taskRef"], []
        for row in self.logs(stamp, ref):
            if row["event"]["name"] != "BidAccepted":
                continue
            bid = row["event"]["payload"]["bid"]
            actual = await self.market.readBid(ref, bid["offer"]["agentRef"], stamp)
            ensure(actual["bid"] == bid, "Admitted bid differs from canonical read", "CONFLICT")
            ensure(
                all(x["bid"]["offer"]["agentRef"] != bid["offer"]["agentRef"] for x in rows),
                "Duplicate admitted identity",
                "CONFLICT",
            )
            rows.append({"bid": bid, "event": row})
        return rows

    def auction(self, view, bids, complete):
        if not complete:
            return {"status": "UNAVAILABLE", "calculated": None, "matchesAllocation": None}
        top, task = (), view["task"]
        for row in bids:
            bid, terms = row["bid"], task["terms"]
            ensure(
                bid["snapshotBlock"] == task["reputationSnapshotBlock"]
                and bid["p"]
                == calculateProbability(
                    int(bid["counters"]["successes"]), int(bid["counters"]["failures"])
                )
                and int(bid["score"])
                == calculateScore(
                    bid["p"],
                    int(terms["alphaNum"]),
                    int(terms["alphaDen"]),
                    int(bid["offer"]["bidAtoms"]),
                ),
                "Auction bid snapshot conflict",
                "CONFLICT",
            )
            top = updateTopTwo(top, bid)
        actual = view["allocation"]
        calculated = calculateAllocation(task, top, int(actual["allocatedAt"]) if actual else 0)
        return {
            "status": "MATCH" if actual == calculated else "CONFLICT" if actual else "PREVIEW",
            "calculated": calculated,
            "matchesAllocation": actual == calculated if actual else None,
        }

    async def readTaskDetails(self, ref, *, artifacts=False, limit=20, cursor=None):
        ensure(type(limit) is int and 1 <= limit <= 100, "Page limit")
        self.market.checkRef(ref)
        query = ["task", ref, artifacts]
        stamp, head, missing, after = await self.snapshot(cursor, query)
        try:
            view = await self.market.readTaskAt(ref, stamp)
        except AdapterError as error:
            if error.kind != "NOT_FOUND" or cursor:
                raise
            view = await self.market.readTaskAt(ref, head)
            stamp = head
            missing.append({"component": "history", "reason": "INCOMPLETE_HISTORY"})
        logs = self.logs(stamp, ref)
        complete = any(x["event"]["name"] == "TaskCreated" for x in logs)
        bids = await self.bidsAt(view, stamp)
        auction = self.auction(view, bids, complete)
        if auction["status"] in {"UNAVAILABLE", "CONFLICT"}:
            missing.append({"component": "auction", "reason": auction["status"]})
        receipt = view["receipt"]
        feedback = {"status": "NOT_APPLICABLE", "publication": None}
        if receipt and receipt["counterEffect"] != "NONE":
            try:
                ensure(self.publisher is not None, "Publisher not configured", "UNAVAILABLE")
                publication = await self.publisher.readPublicationAt(ref, stamp)
                feedback = {
                    "status": "PUBLISHED" if publication["published"] else "PENDING",
                    "publication": publication,
                }
            except AdapterError as error:
                if error.kind == "FINALITY_CONFLICT":
                    raise
                feedback["status"] = "UNAVAILABLE"
                missing.append({"component": "feedback", "reason": error.kind})
        availability = await self.readArtifacts(view) if artifacts else []
        missing.extend(
            {"component": item["component"], "reason": item["status"]}
            for item in availability
            if item["status"] != "VERIFIED"
        )
        cursor = self.cursor(query, stamp, after, limit, len(logs))
        data = {
            "view": view,
            "events": logs[after : after + limit],
            "auction": auction,
            "feedback": feedback,
            "artifacts": availability,
            "unit": {"currency": "MON", "decimals": 18},
        }
        return self.envelope("task", data, stamp, head, missing, cursor)

    def cursor(self, query, stamp, after, limit, size):
        return (
            pageCursor(
                {
                    "query": query,
                    "market": self.chain.facts["market"],
                    "stamp": stamp,
                    "after": after + limit,
                }
            )
            if after + limit < size
            else None
        )

    async def listTaskBids(self, ref, limit=20, cursor=None):
        ensure(type(limit) is int and 1 <= limit <= 100, "Page limit")
        self.market.checkRef(ref)
        query = ["bids", ref]
        stamp, head, missing, after = await self.snapshot(cursor, query)
        view = await self.market.readTaskAt(ref, stamp)
        rows = await self.bidsAt(view, stamp)
        complete = any(x["event"]["name"] == "TaskCreated" for x in self.logs(stamp, ref))
        data = {
            "task": view["task"],
            "bids": rows[after : after + limit],
            "auction": self.auction(view, rows, complete),
        }
        return self.envelope(
            "bids", data, stamp, head, missing, self.cursor(query, stamp, after, limit, len(rows))
        )

    async def listOpenTasks(self, kind="ALL", parentRef=None, limit=20, cursor=None):
        await self.snapshot()
        if cursor:
            outer = readCursor(cursor)
            closed(outer, "market cursor")
            ensure(outer["market"] == self.chain.facts["market"], "Cursor market", "CONFLICT")
            cursor = outer["cursor"]
        page = await self.watcher.listOpenTasks(kind, parentRef, cursor, limit)
        return self.envelope(
            "tasks",
            {"tasks": page["tasks"]},
            page["indexedThrough"],
            page["finalizedHead"],
            cursor=pageCursor({"market": self.chain.facts["market"], "cursor": page["nextCursor"]})
            if page["nextCursor"]
            else None,
        )

    async def readTaskTree(self, ref):
        stamp, head, missing, _ = await self.snapshot()
        first = await self.market.readTaskAt(ref, stamp)
        root = first["task"]["rootRef"]
        indexed = self.watcher.taskRows(stamp["blockNumber"])
        pending, nodes = [root], []
        while pending:
            ensure(len(nodes) + len(pending) <= 21, "Protocol tree bound", "CONFLICT")
            current = pending.pop(0)
            view = await self.market.readTaskAt(current, stamp)
            children = [x["task"]["taskRef"] for x in indexed if x["task"]["parentRef"] == current]
            complete = len(children) == view["childrenCreated"]
            if not complete:
                missing.append({"component": "children", "reason": "INCOMPLETE_HISTORY"})
            ensure(all(x["task"]["taskRef"] != current for x in nodes), "Tree cycle")
            nodes.append(view)
            pending.extend(children)
        return self.envelope("tree", {"rootRef": root, "nodes": nodes}, stamp, head, missing)

    async def readAgentDetails(self, ref, taskRef=None):
        self.market.checkRef(ref, "AgentRef")
        stamp, head, missing, _ = await self.snapshot()
        identity = profile = None
        try:
            ensure(self.market.registry is not None, "Registry not configured", "UNAVAILABLE")
            identity = await self.market.registry.readIdentity(ref, stamp)
            resolved = await resolveProfile(
                ref,
                self.market.registry,
                self.content,
                self.market.schema,
                ref["chainId"],
                ref["identityRegistry"],
                identity=identity,
            )
            profile = resolved.profile
        except AdapterError as error:
            if error.kind == "FINALITY_CONFLICT":
                raise
            missing.append(
                {"component": "metadata" if identity else "identity", "reason": error.kind}
            )
        successes, failures = await self.chain.read(
            "readCounters",
            [
                self.market.checkRef(ref, "AgentRef"),
                "structured-output-v1",
                int(stamp["blockNumber"]),
            ],
            stamp,
        )
        frozen = None
        if taskRef:
            view = await self.market.readTaskAt(taskRef, stamp)
            bid = (await self.market.readBid(taskRef, ref, stamp))["bid"]
            frozen = {"task": view["task"], "bid": bid}
        data = {
            "agentRef": ref,
            "identity": identity,
            "profile": profile,
            "endpoint": "UNPROBED",
            "prior": {"kind": "SYNTHETIC_BETA", "alpha": 1, "beta": 1},
            "taskFamily": "structured-output-v1",
            "counters": {"successes": str(successes), "failures": str(failures)},
            "p": calculateProbability(successes, failures),
            "frozen": frozen,
        }
        return self.envelope("agent", data, stamp, head, missing)

    async def readCredit(self, owner, limit=20, cursor=None):
        ensure(type(limit) is int and 1 <= limit <= 100, "Page limit")
        recordCheck(owner, "Address", self.market.schema)
        query = ["credit", owner]
        stamp, head, missing, after = await self.snapshot(cursor, query)
        credit = await self.market.readCredit(owner, stamp)
        entries = []
        for row in self.logs(stamp):
            name, payload = row["event"]["name"], row["event"]["payload"]
            if name == "CreditWithdrawn" and payload["owner"] == owner:
                entries.append(row)
            elif name == "TaskSettled" and owner in (
                payload["receipt"]["payout"],
                payload["receipt"]["refundAddress"],
            ):
                entries.append(row)
        return self.envelope(
            "credit",
            {
                "credit": credit,
                "events": entries[after : after + limit],
                "unit": {"currency": "MON", "decimals": 18},
            },
            stamp,
            head,
            missing,
            self.cursor(query, stamp, after, limit, len(entries)),
        )

    async def readArtifacts(self, view):
        terms, result, receipt = view["task"]["terms"], view["result"], view["receipt"]
        refs = [
            (name, terms[name], maximum)
            for name, maximum in (
                ("input", 1048576),
                ("outputSchema", 65536),
                ("validationPolicy", 65536),
            )
        ]
        if result:
            # L7 intentionally fetches up to 2 MiB so a complete result above
            # the 1 MiB semantic limit can still be hash-verified and shown.
            refs.append(("result", result["artifact"], 2097152))
        record = receipt["validation"] if receipt else None
        if record:
            refs.append(("evidence", record["evidence"], 65536))
        items = []
        for name, ref, maximum in refs:
            status = "VERIFIED"
            try:
                raw = await self.content.fetchBytes(ref["uri"], maximum, ref["digest"])
                if name == "evidence":
                    evidence = strictJson(raw)
                    recordCheck(evidence, "EvaluationEvidence", self.market.schema)
                    ensure(
                        all(
                            evidence[k] == value
                            for k, value in {
                                "executionRef": record["executionRef"],
                                "verdict": record["verdict"],
                                "inputDigest": terms["input"]["digest"],
                                "resultDigest": record["resultDigest"],
                                "outputSchemaDigest": terms["outputSchema"]["digest"],
                                "validationPolicyDigest": terms["validationPolicy"]["digest"],
                            }.items()
                        ),
                        "Evidence binding",
                    )
                    ensure(
                        self.signingTypes is not None, "Signing types unavailable", "UNAVAILABLE"
                    )
                    typed = buildValidationVerdictTypedData(
                        record,
                        {
                            "name": "AgentLance",
                            "version": "1",
                            "chainId": int(self.chain.facts["chainId"]),
                            "verifyingContract": self.chain.facts["market"],
                        },
                        self.signingTypes,
                    )
                    ensure(
                        verifyEoa(typed, record["signature"], record["validator"]),
                        "Validation signature",
                    )
            except AdapterError as error:
                status = error.kind
            items.append({"component": name, "ref": ref, "status": status})
        return items
