"""Winner hints reuse the real A2A client/server boundary in the L2 ASGI fixture."""

import asyncio

import httpx

from modules.adapters.a2a.client import AgentClient
from modules.adapters.a2a.notifications import AwardNotifier


def testNotificationIsOptionalAndIdempotent(l2):
    async def run():
        await l2.award()
        notifier = AwardNotifier(
            l2.market,
            l2.market,
            l2.participant.content,
            l2.journal,
            l2.http,
            l2.schema,
            clock=lambda: 1000,
        )
        sent = await notifier.notify(l2.ref)
        assert sent["state"] == "DELIVERED"
        assert sent["winner"] == l2.config["agentRef"]
        assert await notifier.notify(l2.ref) == sent
        assert len(l2.journal.rows()) == 1 and not l2.calls

    asyncio.run(run())


def testNotificationMissingPinnedCardDoesNotRedirect(l2):
    async def run():
        await l2.award()
        l2.journal.db.execute("DELETE FROM content")
        l2.journal.db.commit()
        notifier = AwardNotifier(
            l2.market,
            l2.market,
            l2.participant.content,
            l2.journal,
            l2.http,
            l2.schema,
            maxAttempts=2,
            clock=lambda: 1000,
        )
        # Current metadata can no longer resolve to the admitted bytes.
        l2.host.contents.clear()
        waiting = await notifier.notify(l2.ref)
        assert waiting["state"] == "PENDING" and waiting["detail"]
        assert (await l2.market.readSettlement(l2.config["taskRef"]))["receipt"] is None
        l2.host.setClock(1500)
        assert (await notifier.notify(l2.ref))["state"] == "STOPPED"
        assert not l2.calls

    asyncio.run(run())


def testLostConcurrentHintsKeepMessageIdentity(l2, monkeypatch):
    async def run():
        await l2.award()
        original = AgentClient.sendHint
        messages, now = [], [1000]

        async def lost(client, message):
            messages.append(message)
            response = await original(client, message)
            if len(messages) == 1:
                raise httpx.ReadError("Lost response after the winner admitted the hint")
            return response

        monkeypatch.setattr(AgentClient, "sendHint", lost)
        notifier = AwardNotifier(
            l2.market,
            l2.market,
            l2.participant.content,
            l2.journal,
            l2.http,
            l2.schema,
            clock=lambda: now[0],
        )
        hooks = {name: len(values) for name, values in l2.http.event_hooks.items()}
        rows = await asyncio.gather(*(notifier.notify(l2.ref) for _ in range(4)))
        assert all(row["state"] == "PENDING" for row in rows) and len(messages) == 1
        now[0] += 2
        assert (await notifier.notify(l2.ref))["state"] == "DELIVERED"
        assert messages[0] == messages[1]
        assert len(l2.journal.rows()) == 1 and not l2.calls
        assert hooks == {name: len(values) for name, values in l2.http.event_hooks.items()}

    asyncio.run(run())
