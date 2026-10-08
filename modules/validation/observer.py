"""Independent finalized-log consumer; projection and checkpoint commit together."""

from modules.adapters.a2a.profile import strictJson
from modules.agent_client.ports import ensure
from modules.economics.records import recordDigest


class OutcomeObserver:
    def __init__(self, market, watcher, store, *, admit=None, publisher=None):
        self.market, self.watcher, self.store = market, watcher, store
        self.admit, self.publisher = admit, publisher

    async def consume(self, limit=100):
        ensure(1 <= limit <= 100, "Outcome tick limit")
        cursor = self.store.get("cursor", "outcomes-v1")
        position = tuple(map(int, cursor["position"])) if cursor else (-1, -1)
        logs = [
            strictJson(row[0])
            for row in self.store.db.execute("SELECT body FROM observed_logs WHERE stream='market'")
        ]
        logs.sort(key=lambda row: (int(row["blockNumber"]), int(row["logIndex"])))
        for event in [
            row for row in logs if (int(row["blockNumber"]), int(row["logIndex"])) > position
        ][:limit]:
            name, payload = event["event"]["name"], event["event"]["payload"]
            view = None
            if name in {"ResultSubmitted", "TaskSettled"}:
                ref = (
                    payload["result"]["executionRef"]["taskRef"]
                    if name == "ResultSubmitted"
                    else payload["receipt"]["taskRef"]
                )
                stamp = await self.market.chain.block(event["blockNumber"])
                await self.market.chain.checkCanonical(stamp)
                ensure(
                    stamp["blockHash"] == event["blockHash"],
                    "Outcome log binding",
                    "FINALITY_CONFLICT",
                )
                view = await self.market.readTaskAt(ref, stamp)
                if name == "TaskSettled" and view["receipt"] != payload["receipt"]:
                    self.store.journal.halt("Receipt differs from finalized event")
            if name == "TaskSettled":
                previous = self.store.get("receipt", recordDigest(ref))
                if previous and (
                    previous["receipt"] != view["receipt"] or previous["event"] != event
                ):
                    self.store.journal.halt("Conflicting finalized receipt projection")
            with self.store.db:
                if view is not None:
                    # Historical eth_call returns end-of-block state; a competing
                    # verdict can already have settled this submission in that block.
                    if name == "ResultSubmitted" and self.admit and view["receipt"] is None:
                        self.admit(view, event)
                    elif name == "TaskSettled":
                        key = recordDigest(ref)
                        body = {
                            "version": 1,
                            "task": view["task"],
                            "result": view["result"],
                            "receipt": view["receipt"],
                            "event": event,
                            "stamp": stamp,
                        }
                        self.store.put("receipt", key, body, immutable=True)
                        if self.publisher and self.store.get("export", key) is None:
                            self.store.put(
                                "export",
                                key,
                                {
                                    "version": 1,
                                    "taskRef": ref,
                                    "state": "NOT_APPLICABLE"
                                    if view["receipt"]["counterEffect"] == "NONE"
                                    else "PENDING",
                                    "operationId": recordDigest(
                                        ["feedback-v1", self.publisher, ref, 0]
                                    ),
                                    "attempts": 0,
                                    "nextAttempt": 0,
                                    "diagnostic": None,
                                    "publication": None,
                                    "retryGrants": 0,
                                },
                            )
                self.store.put(
                    "cursor",
                    "outcomes-v1",
                    {"version": 1, "position": [event["blockNumber"], event["logIndex"]]},
                )

    def readOutcome(self, taskRef):
        receipt = self.store.get("receipt", recordDigest(taskRef))
        job = next(
            (job for _, job in self.store.rows("job") if job["view"]["task"]["taskRef"] == taskRef),
            None,
        )
        export = self.store.get("export", recordDigest(taskRef))
        return {
            "taskRef": taskRef,
            "task": receipt["task"] if receipt else job["view"]["task"] if job else None,
            "result": receipt["result"] if receipt else job["view"]["result"] if job else None,
            "status": "SETTLED" if receipt else job["view"]["status"] if job else None,
            "receipt": receipt["receipt"] if receipt else None,
            "observedAt": receipt["stamp"] if receipt else job["view"]["stamp"] if job else None,
            "validation": {k: job[k] for k in ("stage", "diagnostic", "evidence", "record")}
            if job
            else None,
            "feedback": export,
        }
