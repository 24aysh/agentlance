"""Real port/crypto/core integration with deterministic time and durable storage."""

import asyncio
from copy import deepcopy

import pytest

from modules.adapters.a2a.profile import ProfileError
from modules.adapters.fixtures import commandRecord
from modules.adapters.storage.journal import Journal
from modules.agent_client.ports import AdapterError
from modules.domain.records import agentKey


def testCompleteDuplicateRestart(l2):
    async def run():
        await l2.award(pending=True)
        rows = await asyncio.gather(*(l2.participant.receiveHint(l2.message) for _ in range(12)))
        assert len({row["correlation"]["a2aTaskId"] for row in rows}) == 1
        await l2.participant.advance(l2.ref)
        assert not l2.calls
        l2.restart()
        l2.host.setClock(1400)
        l2.host.pendingCommands.add("acceptAward")
        await l2.participant.advance(l2.ref)
        await l2.participant.advance(l2.ref)
        assert not l2.calls
        l2.restart()
        l2.host.setClock(1600)
        for _ in range(4):
            await l2.participant.advance(l2.ref)
        assert l2.calls == [b'{"value":7}\n']
        before = l2.journal.get(l2.ref)
        l2.restart()
        assert await l2.participant.receiveHint(l2.message) == before
        await l2.participant.tick()
        assert l2.calls == [b'{"value":7}\n']
        assert (await l2.market.readSettlement(l2.config["taskRef"]))["receipt"] is None
        l2.host.setClock(3000)
        await l2.host.submit(
            l2.config["requester"],
            "expire",
            commandRecord("expireTask", taskRef=l2.config["taskRef"]),
        )
        receipt = (await l2.market.readSettlement(l2.config["taskRef"]))["receipt"]
        assert (
            receipt["reason"],
            receipt["paidAtoms"],
            receipt["refundAtoms"],
            receipt["counterEffect"],
        ) == ("VALIDATOR_TIMEOUT", "0", "100", "NONE")
        assert l2.host.state.credits == {l2.config["requester"]: 100}

    asyncio.run(run())


@pytest.mark.parametrize("change", ["agent", "task", "input", "policy", "expired", "open"])
def testInvalidAwardCannotPoison(l2, change):
    async def run():
        if change == "open":
            await l2.create()
        else:
            await l2.award()
        message = deepcopy(l2.message)
        ext = message["metadata"]["urn:agentlance:a2a:1"]
        if change == "agent":
            ext["agentRef"]["agentId"] = "8"
        if change == "task":
            ext["executionRef"]["taskRef"]["taskId"] = "999"
        if change == "input":
            ext["inputDigest"] = "0x" + "01" * 32
        if change == "policy":
            ext["validationPolicyDigest"] = "0x" + "01" * 32
        if change == "expired":
            l2.host.setClock(1500)
        with pytest.raises(ProfileError):
            await l2.participant.receiveHint(message)
        assert l2.journal.rows() == [] and l2.calls == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "phase",
    [
        "WAIT",
        "ACCEPT_PENDING",
        "READY",
        "STARTED",
        "ARTIFACT_READY",
        "RESULT_PENDING",
        "RESULT_RECORDED",
    ],
)
def testCrashPhases(l2, phase):
    async def run():
        await l2.running()
        if phase in {"WAIT", "ACCEPT_PENDING", "READY", "STARTED"}:
            l2.journal.update(l2.ref, phase=phase)
        else:
            await l2.participant.advance(l2.ref)
            if phase != "ARTIFACT_READY":
                await l2.participant.advance(l2.ref)
            if phase == "RESULT_RECORDED":
                await l2.participant.advance(l2.ref)
        l2.restart()
        for _ in range(4):
            await l2.participant.advance(l2.ref)
        row = l2.journal.get(l2.ref)
        assert row["phase"] == ("INTERRUPTED" if phase == "STARTED" else "RESULT_RECORDED")
        assert len(l2.calls) == (0 if phase == "STARTED" else 1)

    asyncio.run(run())


@pytest.mark.parametrize("method,stage", [("acceptAward", "award"), ("commitResult", "running")])
def testLostWriteResponseReconciles(l2, monkeypatch, method, stage):
    async def run():
        if stage == "award":
            await l2.award()
            await l2.participant.receiveHint(l2.message)
        else:
            await l2.running()
            await l2.participant.advance(l2.ref)
        original = getattr(l2.market, method)
        calls = []

        async def lose(*args):
            calls.append(args)
            await original(*args)
            raise AdapterError("UNAVAILABLE", "Lost response")

        monkeypatch.setattr(l2.market, method, lose)
        await l2.participant.advance(l2.ref)
        l2.restart()
        for _ in range(4):
            await l2.participant.advance(l2.ref)
        assert len(calls) == 1 and len(l2.calls) == 1
        assert l2.journal.get(l2.ref)["phase"] == "RESULT_RECORDED"

    asyncio.run(run())


def testUnknownWriteNeverResends(l2, monkeypatch):
    async def run():
        await l2.award()
        await l2.participant.receiveHint(l2.message)
        calls = []

        async def lose(*args):
            calls.append(args)
            raise AdapterError("UNAVAILABLE", "Unknown submission")

        monkeypatch.setattr(l2.market, "acceptAward", lose)
        for _ in range(3):
            await l2.participant.advance(l2.ref)
        l2.restart()
        await l2.participant.advance(l2.ref)
        assert len(calls) == 1 and not l2.calls
        assert l2.host.view(l2.config["taskRef"])["status"] == "AWARDED"

    asyncio.run(run())


def testTransferPreservesObligation(l2):
    async def run():
        await l2.award()
        identity = l2.host.identities[agentKey(l2.config["agentRef"])]
        identity.update(
            owner="0x" + "42" * 20,
            verifiedWallet=None,
            registrationUri="https://changed.example/registration",
        )
        await l2.participant.receiveHint(l2.message)
        for _ in range(4):
            await l2.participant.advance(l2.ref)
        assert len(l2.calls) == 1
        bid = (await l2.market.readBid(l2.config["taskRef"], l2.config["agentRef"]))["bid"]
        assert bid["offer"]["payout"] == l2.config["payout"]
        assert bid["ownerAtBid"] != identity["owner"]

    asyncio.run(run())


def testFinalityConflictPersists(l2):
    async def run():
        await l2.award()
        await l2.participant.receiveHint(l2.message)
        stamp = l2.host.stamp()
        stamp["blockHash"] = "0x" + "ff" * 32
        with pytest.raises(AdapterError, match="FINALITY_CONFLICT"):
            l2.journal.observe(stamp)
        l2.restart()
        await l2.participant.advance(l2.ref)
        assert l2.journal.halted() and not l2.calls
        with pytest.raises(AdapterError, match="FINALITY_CONFLICT"):
            await l2.participant.prepareBid(l2.config["taskRef"], "20", l2.sign, "2", "1200")

    asyncio.run(run())


def testJournalLockSettingsAndCorruption(l2):
    with pytest.raises(AdapterError, match="already in use"):
        Journal(l2.config["database"], l2.settings)
    l2.journal.close()
    with pytest.raises(AdapterError, match="session mismatch"):
        Journal(l2.config["database"], l2.settings | {"sessionId": "different"})
    l2.journal = Journal(l2.config["database"], l2.settings)
    digest = l2.journal.storeContent(b"original")
    l2.journal.db.execute("UPDATE content SET raw=? WHERE digest=?", (b"changed", digest))
    with pytest.raises(AdapterError, match="corrupt"):
        l2.journal.readContent(digest)


def testWorkerFailureIsNotEconomicFailure(l2):
    async def run():
        await l2.running()

        def fail(raw):
            raise ValueError("worker failure")

        l2.participant.execute = fail
        await l2.participant.advance(l2.ref)
        l2.restart()
        await l2.participant.advance(l2.ref)
        assert l2.journal.get(l2.ref)["phase"] == "INTERRUPTED"
        assert l2.host.view(l2.config["taskRef"])["status"] == "RUNNING"
        assert not l2.host.state.credits

    asyncio.run(run())


def testTentativeInvalidation(l2):
    async def run():
        await l2.award(pending=True)
        await l2.participant.receiveHint(l2.message)
        l2.host.state = deepcopy(l2.host.finalizedState)
        l2.host.setClock(1250)
        await l2.participant.advance(l2.ref)
        assert l2.journal.get(l2.ref)["phase"] == "STOPPED"
        assert not l2.calls
        l2.restart()
        await l2.participant.advance(l2.ref)
        assert not l2.calls

    asyncio.run(run())


def testFinalizedObligationChangeHalts(l2):
    from modules.domain.records import taskKey

    async def run():
        await l2.running()
        l2.host.state.tasks[taskKey(l2.config["taskRef"])].spec["terms"]["input"]["digest"] = (
            "0x" + "ff" * 32
        )
        l2.host.setClock(1450)
        await l2.participant.advance(l2.ref)
        assert l2.journal.halted() and not l2.calls

    asyncio.run(run())


def testProviderUnavailableWaits(l2, monkeypatch):
    async def run():
        await l2.award()
        await l2.participant.receiveHint(l2.message)

        async def unavailable(ref):
            raise AdapterError("UNAVAILABLE", "Provider unavailable")

        monkeypatch.setattr(l2.market, "observeAward", unavailable)
        await l2.participant.advance(l2.ref)
        assert l2.journal.get(l2.ref)["phase"] == "WAIT" and not l2.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "deadline",
        "accept-rejected",
        "result-rejected",
        "result-conflict",
        "late-artifact",
        "fresh-pending",
        "reserve-changed",
        "new-owner",
    ],
)
def testExecutionGuardFailures(l2, monkeypatch, case):
    from modules.domain.records import taskKey

    async def run():
        if case in {"deadline", "accept-rejected", "new-owner"}:
            await l2.award()
            await l2.participant.receiveHint(l2.message)
        else:
            await l2.running()
        if case == "deadline":
            l2.host.setClock(1500)
        if case == "new-owner":
            l2.participant.signer = l2.config["requester"]
        if case == "accept-rejected":

            async def reject(command, operationId):
                return {
                    "operationId": operationId,
                    "state": "REJECTED",
                    "error": "UNAUTHORIZED",
                    "events": [],
                    "stamp": None,
                }

            monkeypatch.setattr(l2.market, "acceptAward", reject)
        if case in {"result-rejected", "result-conflict", "late-artifact"}:
            await l2.participant.advance(l2.ref)
            if case == "result-rejected":

                async def reject(command, operationId):
                    return {
                        "operationId": operationId,
                        "state": "REJECTED",
                        "error": "ACTIVE_CHILDREN",
                        "events": [],
                        "stamp": None,
                    }

                monkeypatch.setattr(l2.market, "commitResult", reject)
            elif case == "result-conflict":
                await l2.participant.advance(l2.ref)
                l2.host.state.tasks[taskKey(l2.config["taskRef"])].result["artifact"]["digest"] = (
                    "0x" + "ff" * 32
                )
                l2.host.setClock(1600)
            else:
                l2.host.setClock(2500)
        if case == "fresh-pending":
            original = l2.market.observeAward
            reads = []

            async def pending(ref):
                view = await original(ref)
                reads.append(ref)
                if len(reads) > 1:
                    view["stamp"]["finality"] = "PENDING"
                return view

            monkeypatch.setattr(l2.market, "observeAward", pending)
        if case == "reserve-changed":
            l2.host.state.tasks[taskKey(l2.config["taskRef"])].ownWorkReserveAtoms = 1
            l2.host.setClock(1450)
        await l2.participant.advance(l2.ref)
        row = l2.journal.get(l2.ref)
        assert len(l2.calls) == (
            1 if case in {"result-rejected", "result-conflict", "late-artifact"} else 0
        )
        if case in {"result-conflict", "late-artifact"}:
            assert row["result"] is not None and row["phase"] != "RESULT_RECORDED"
        elif case in {"deadline", "accept-rejected", "new-owner"}:
            assert row["phase"] == "STOPPED"
        assert not l2.host.state.credits

    asyncio.run(run())


def testJournalImmutableResult(l2):
    async def run():
        await l2.complete()
        row = l2.journal.get(l2.ref)
        l2.journal.storeResult(l2.ref, row["result"], l2.calls[0])
        with pytest.raises(ProfileError):
            l2.journal.storeResult(
                l2.ref, row["result"] | {"uri": "https://elsewhere.example/result"}, l2.calls[0]
            )
        with pytest.raises(AdapterError):
            l2.journal.update(l2.ref, phase="READY")
        assert l2.journal.get(l2.ref) == row

    asyncio.run(run())
