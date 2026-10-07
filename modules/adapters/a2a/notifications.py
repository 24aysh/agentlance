"""Optional winner-only hints. Their delivery never changes market state."""

import asyncio
import time
from uuid import uuid4

import httpx
from a2a.client.errors import A2AClientError

from modules.adapters.a2a.client import AgentClient
from modules.adapters.a2a.profile import ProfileError, awardHint, jsonBytes, strictJson
from modules.adapters.registry.resolution import resolveProfile
from modules.adapters.storage.journal import executionKey
from modules.agent_client.ports import AdapterError, ensure


class AwardNotifier:
    def __init__(
        self, market, registry, content, journal, http, schema, *, maxAttempts=3, clock=time.time
    ):
        ensure(type(maxAttempts) is int and 1 <= maxAttempts <= 10, "Hint retry limit")
        self.market, self.registry, self.content = market, registry, content
        self.journal, self.http, self.schema = journal, http, schema
        self.maxAttempts, self.clock = maxAttempts, clock
        self.lock = asyncio.Lock()

    def save(self, key, row):
        with self.journal.db:
            self.journal.db.execute(
                "INSERT INTO notifications VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                (key, jsonBytes(row)),
            )

    async def notify(self, executionRef):
        async with self.lock:
            return await self.deliver(executionRef)

    async def deliver(self, executionRef):
        key = executionKey(executionRef)
        found = self.journal.db.execute(
            "SELECT body FROM notifications WHERE key=?", (key,)
        ).fetchone()
        row = strictJson(found[0]) if found else None
        if row and row["state"] in {"DELIVERED", "STOPPED"}:
            return row
        view = await self.market.observeAward(executionRef)
        ensure(view["stamp"]["finality"] == "FINALIZED", "Hint waits for finality", "UNAVAILABLE")
        if view["status"] != "AWARDED" or int(view["stamp"]["blockTimestamp"]) >= int(
            view["task"]["terms"]["acceptBy"]
        ):
            if row:
                row.update(state="STOPPED", detail="Award progressed or expired")
                self.save(key, row)
            return row
        bid = view["winningBid"]["offer"]
        ensure(
            view["allocation"]["awardId"] == executionRef["awardId"]
            and view["task"]["taskRef"] == executionRef["taskRef"],
            "Hint award binding",
        )
        if row is None:
            row = {
                "state": "PENDING",
                "winner": bid["agentRef"],
                "profileDigest": bid["profileDigest"],
                "message": awardHint(view, str(uuid4())),
                "attempts": 0,
                "lastAttempt": 0,
                "detail": None,
            }
            self.save(key, row)
        ensure(
            row["winner"] == bid["agentRef"] and row["profileDigest"] == bid["profileDigest"],
            "Admitted notification binding changed",
            "FINALITY_CONFLICT",
        )
        now = int(self.clock())
        if row["attempts"] >= self.maxAttempts or (
            row["attempts"] and now - row["lastAttempt"] < 2
        ):
            return row
        row.update(attempts=row["attempts"] + 1, lastAttempt=now)
        self.save(key, row)
        try:
            try:
                card = self.journal.readContent(row["profileDigest"])
            except AdapterError as error:
                if error.kind != "NOT_FOUND":
                    raise
                resolved = await resolveProfile(
                    row["winner"],
                    self.registry,
                    self.content,
                    self.schema,
                    row["winner"]["chainId"],
                    row["winner"]["identityRegistry"],
                )
                ensure(
                    resolved.profile["agentCard"]["digest"] == row["profileDigest"],
                    "Pinned winner card unavailable",
                    "UNAVAILABLE",
                )
                card = resolved.cardBytes
                self.journal.storeContent(card)
            # The SDK adds hooks to the supplied client. Remove this attempt's hooks afterwards.
            hooks = {name: list(values) for name, values in self.http.event_hooks.items()}
            try:
                client = AgentClient(card, self.http, self.schema)
                async with asyncio.timeout(10):
                    await client.sendHint(row["message"])
            finally:
                self.http.event_hooks = hooks
            row.update(state="DELIVERED", detail=None)
        except AdapterError as error:
            row["detail"] = error.kind + ": " + error.detail
        except (A2AClientError, ProfileError, httpx.HTTPError, TimeoutError, OSError):
            row["detail"] = "Winner notification unavailable or invalid"
        self.save(key, row)
        return row
