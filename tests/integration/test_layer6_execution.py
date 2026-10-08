import asyncio
from copy import deepcopy

import pytest

from modules.adapters.storage.journal import executionKey
from modules.agent_client.ports import AdapterError
from modules.execution.rules import executionDigest, quantities
from tests.layer2_support import Layer2Rig
from tests.layer6_support import composeExecution, drainExecution, executionProfile


@pytest.mark.parametrize("sdk", [False, True])
def testAcceptedDockerAndActualSdk(tmp_path, sdk):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, model = composeExecution(rig.participant, lambda: rig.host.now, sdk=sdk)
        try:
            assert model.calls == 0
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            assert not coordinator.store.steps(rig.ref)
            await rig.participant.tick()
            await drainExecution(coordinator)
            row = rig.journal.get(rig.ref)
            assert row["phase"] == "ARTIFACT_READY", row
            assert rig.journal.readContent(row["result"]["digest"]) == b'{"value":7}'
            await rig.participant.tick()
            await rig.participant.tick()
            await coordinator.tick()
            assert rig.journal.get(rig.ref)["phase"] == "RESULT_RECORDED"
            assert model.calls == (2 if sdk else 0)
            outbox = coordinator.store.get("outbox", executionKey(rig.ref))
            assert outbox["state"] == "DELIVERED", outbox
            assert outbox["report"]["completeness"] == "COMPLETE"
            count = len(coordinator.store.steps(rig.ref))
            await coordinator.close()
            rig.restart()
            coordinator, model2 = composeExecution(rig.participant, lambda: rig.host.now, sdk=sdk)
            await rig.participant.tick()
            assert len(coordinator.store.steps(rig.ref)) == count and model2.calls == 0
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testCapacityBeforeSignatureAndUnknownHold(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant)
        profile["capacity"] = 1
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, profile=profile)
        try:
            await rig.create()
            await rig.host.submit(
                rig.config["requester"],
                "second-task",
                {"schemaVersion": 1, "command": "createTask", "input": {"terms": rig.terms}},
                rig.terms["budgetAtoms"],
            )
            task = (await rig.market.readTask(rig.ref["taskRef"]))["task"]
            other = deepcopy(task)
            other["taskRef"]["taskId"] = "2"
            results = await asyncio.gather(
                coordinator.reserveBid(task, "20"),
                coordinator.reserveBid(other, "20"),
                return_exceptions=True,
            )
            assert sum(isinstance(r, AdapterError) for r in results) == 1
            assert coordinator.store.available() == 0
            assert rig.journal.findIntent("bid:" + executionKey(rig.ref)) is None
            # A reserved, unsubmitted candidate can release only after canonical close.
            rig.host.setClock(1200)
            await coordinator.observeReservations()
            assert coordinator.store.available() == 1
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testManagedCrashRetainsClaimAndBudget(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            rig.journal.update(rig.ref, phase="READY")
            coordinator.store.claim(rig.ref, b'{"value":7}', rig.host.now, rig.host.now + 100)
            step = coordinator.store.prepareStep(
                rig.ref,
                "solo",
                "TOOL",
                {"input": 7},
                quantities(coordinator.profile, transform=1),
                rig.host.now,
                "20000",
            )
            coordinator.store.startStep(step)
            rig.restart()
            assert rig.journal.get(rig.ref)["phase"] == "STARTED"
            coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
            await coordinator.tick()
            await drainExecution(coordinator)
            assert rig.journal.get(rig.ref)["phase"] == "INTERRUPTED"
            assert coordinator.store.steps(rig.ref)[0]["state"] == "UNKNOWN"
            assert coordinator.store.steps(rig.ref)[0]["chargeAtoms"] == "20000"
            assert (
                coordinator.store.get("outbox", executionKey(rig.ref))["report"]["completeness"]
                == "INCOMPLETE"
            )
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testResourceExhaustionAndUsageDeliveryRetry(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant, sdk=True)
        profile["limits"]["modelCalls"] = 1
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, model = composeExecution(
            rig.participant, lambda: rig.host.now, profile=profile
        )
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await drainExecution(coordinator)
            assert model.calls == 1
            assert rig.journal.get(rig.ref)["phase"] == "INTERRUPTED"
            report = coordinator.store.get("outbox", executionKey(rig.ref))
            assert report["state"] == "DELIVERED"
            assert all(item["category"] == "EXECUTION" for item in report["report"]["items"])
            assert (
                await coordinator.history.recordUsage(report["report"], report["context"])
                == "UNCHANGED"
            )
            previous = report["report"]["reportId"]
            corrected = deepcopy(report["report"])
            from modules.economics.records import recordDigest

            corrected["reportId"] = recordDigest([previous, "correction"])
            assert (
                await coordinator.history.recordUsage(corrected, report["context"], previous)
                == "STORED"
            )
            assert coordinator.store.get("outbox", executionKey(rig.ref)) == report
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testNeverStartedAwardProducesExplicitZeroUsage(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            rig.host.setClock(int(rig.terms["acceptBy"]))
            await rig.participant.tick()
            await rig.participant.tick()
            outbox = coordinator.store.get("outbox", executionKey(rig.ref))
            assert outbox["context"]["status"] == "TIMED_OUT" and outbox["context"]["zeroUsage"]
            assert outbox["report"]["completeness"] == "INCOMPLETE"
            assert outbox["report"]["items"] == [] and outbox["state"] == "DELIVERED"
            assert not rig.journal.get(rig.ref)["startClaimed"]
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testCapacityIsEnforcedAtCommonSigningPath(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant)
        profile["capacity"] = 1
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, profile=profile)
        signatures = []

        def sign(permit):
            signatures.append(permit)
            return rig.sign(permit)

        try:
            await rig.create()
            await rig.host.submit(
                rig.config["requester"],
                "second-task",
                {"schemaVersion": 1, "command": "createTask", "input": {"terms": rig.terms}},
                rig.terms["budgetAtoms"],
            )
            other = rig.ref["taskRef"] | {"taskId": "2"}
            results = await asyncio.gather(
                rig.participant.prepareBid(rig.ref["taskRef"], "20", sign, "1", "1200"),
                rig.participant.prepareBid(other, "20", sign, "2", "1200"),
                return_exceptions=True,
            )
            assert len(signatures) == 1 and sum(isinstance(r, AdapterError) for r in results) == 1
            rig.host.setClock(1200)
            intent = rig.journal.findIntent("bid:" + executionKey(rig.ref))
            original = rig.market.readOperation

            async def unknown(operationId):
                return {
                    "operationId": operationId,
                    "state": "UNKNOWN",
                    "events": [],
                    "stamp": None,
                    "error": None,
                }

            rig.market.readOperation = unknown
            await coordinator.observeReservations()
            assert coordinator.store.available() == 0
            rig.market.readOperation = original
            # Finalized no-award terminal state releases even a previously unknown bid.
            rig.host.setClock(int(rig.terms["allocationBy"]))
            await rig.host.submit(
                rig.config["requester"],
                "expire",
                {
                    "schemaVersion": 1,
                    "command": "expireTask",
                    "input": {"taskRef": rig.ref["taskRef"]},
                },
            )
            await coordinator.observeReservations()
            assert coordinator.store.available() == 1 and intent is not None
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testCompletedStepResumesWithoutRepeatingDocker(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            rig.journal.update(rig.ref, phase="READY")
            coordinator.store.claim(rig.ref, b'{"value":7}', rig.host.now, rig.host.now + 100)
            view = await rig.market.observeAward(rig.ref)
            plan = coordinator.proposal(rig.ref, view, coordinator.profile, b'{"value":7}')
            await coordinator.freezePlan(rig.ref, plan, view)
            output = await coordinator.runStep(
                rig.ref, "solo", {"kind": "SOLO", "input": {"value": 7}}
            )
            assert output == b'{"value":7}' and rig.journal.get(rig.ref)["result"] is None
            profile = coordinator.profile
            rig.restart()
            # New config affects new bids only: retained plan and profile stay unchanged.
            changed = deepcopy(profile)
            changed["limits"]["wallSeconds"] += 10
            changed["runtime"]["configDigest"] = executionDigest(changed)
            coordinator, _ = composeExecution(
                rig.participant, lambda: rig.host.now, profile=changed
            )

            async def forbidden(*args):
                raise AssertionError("A completed step was dispatched twice")

            coordinator.docker.run = forbidden
            await coordinator.tick()
            await drainExecution(coordinator)
            assert rig.journal.get(rig.ref)["phase"] == "ARTIFACT_READY"
            run = coordinator.store.get("run", executionKey(rig.ref))
            assert run["profile"] == profile and run["plan"] == plan
            assert len(coordinator.store.steps(rig.ref)) == 1
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testSlowExecutionDoesNotBlockAnotherAcceptance(tmp_path):
    async def scenario():
        from modules.adapters.a2a.profile import awardHint
        from modules.agent_client.participant import makeCommand

        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        release = asyncio.Event()
        entered = asyncio.Event()
        run = coordinator.docker.run

        async def slow(ref, stepId, request, profile):
            if ref == rig.ref:
                entered.set()
                await release.wait()
            return await run(ref, stepId, request, profile)

        coordinator.docker.run = slow
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await asyncio.wait_for(entered.wait(), 5)
            terms = deepcopy(rig.terms)
            for field in ("biddingClose", "allocationBy", "acceptBy", "resultBy", "validationBy"):
                terms[field] = str(int(terms[field]) + 200)
            await rig.host.submit(
                rig.config["requester"],
                "task2",
                makeCommand("createTask", terms=terms),
                terms["budgetAtoms"],
            )
            other = {"taskRef": rig.ref["taskRef"] | {"taskId": "2"}, "awardId": 1}
            await rig.participant.prepareBid(
                other["taskRef"], "20", rig.sign, "2", terms["biddingClose"]
            )
            rig.host.setClock(int(terms["biddingClose"]))
            await rig.host.submit(
                rig.config["requester"],
                "allocate2",
                makeCommand("allocateTask", taskRef=other["taskRef"]),
            )
            await asyncio.wait_for(
                rig.participant.receiveHint(
                    awardHint(await rig.market.observeAward(other), "second")
                ),
                2,
            )
            await asyncio.wait_for(rig.participant.advance(other), 2)
            assert (await rig.market.observeAward(other))["status"] == "RUNNING"
            assert not release.is_set()
        finally:
            release.set()
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testUsageOutboxRetriesIdenticalEvidenceAndStops(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        original = coordinator.history.recordUsage
        sent = []

        async def unavailable(report, context, supersedes):
            sent.append(deepcopy((report, context, supersedes)))
            raise AdapterError("UNAVAILABLE", "history offline")

        coordinator.history.recordUsage = unavailable
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await drainExecution(coordinator)
            for _ in range(6):
                rig.host.setClock(rig.host.now + 2)
                await rig.participant.tick()
            row = coordinator.store.get("outbox", executionKey(rig.ref))
            assert row["state"] == "PENDING" and len(sent) == 3 and all(x == sent[0] for x in sent)
            assert rig.journal.get(rig.ref)["phase"] == "RESULT_RECORDED"
            assert await original(row["report"], row["context"], row["supersedes"]) == "STORED"
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testFinalityHaltPreventsFurtherPaidCalls(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, sdk=True)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            rig.journal.update(rig.ref, phase="READY")
            coordinator.store.claim(rig.ref, b'{"value":7}', rig.host.now, rig.host.now + 100)
            with pytest.raises(AdapterError):
                rig.journal.halt("test conflict")
            with pytest.raises(AdapterError, match="FINALITY_CONFLICT"):
                await coordinator.runStep(rig.ref, "solo", {"kind": "SOLO", "input": {"value": 7}})
            assert coordinator.store.steps(rig.ref) == []
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "provider", "malformed"])
def testSdkFailureTelemetryIsNotFreeRetry(tmp_path, failure):
    async def scenario():
        from agents import ModelResponse
        from agents.usage import Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        from tests.layer6_support import IsolatedModel

        class BrokenModel(IsolatedModel):
            async def get_response(self, *args, **kwargs):
                self.calls += 1
                if failure == "timeout":
                    await asyncio.sleep(2)
                if failure != "malformed":
                    raise RuntimeError("Provider response lost")
                return ModelResponse(
                    output=[
                        ResponseOutputMessage(
                            id="malformed",
                            type="message",
                            role="assistant",
                            status="completed",
                            content=[
                                ResponseOutputText(
                                    type="output_text", text="not JSON", annotations=[]
                                )
                            ],
                        )
                    ],
                    usage=Usage(requests=1, input_tokens=10, output_tokens=10, total_tokens=20),
                    response_id=None,
                )

        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant, sdk=True)
        profile["limits"]["requestSeconds"] = 1
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, profile=profile)
        model = BrokenModel()
        coordinator.agents.model = model
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await drainExecution(coordinator)
            await rig.participant.tick()
            assert model.calls == 1 and rig.journal.get(rig.ref)["phase"] == "INTERRUPTED"
            report = coordinator.store.get("outbox", executionKey(rig.ref))["report"]
            assert report["completeness"] == (
                "COMPLETE" if failure == "malformed" else "INCOMPLETE"
            )
            if failure != "malformed":
                assert all(
                    i["quantity"] is None and i["costAtoms"] is None for i in report["items"]
                )
            assert sum(int(s["chargeAtoms"]) for s in coordinator.store.steps(rig.ref)) == 20000
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testUsageStorageBackpressureCannotBlockSavedResult(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await drainExecution(coordinator)
            assert rig.journal.get(rig.ref)["phase"] == "ARTIFACT_READY"

            # Simulate an independently exhausted analytics quota after the artifact commit.
            async def unavailable():
                raise AdapterError("UNAVAILABLE", "analytics quota exhausted")

            coordinator.tick = unavailable
            with pytest.raises(AdapterError):
                await rig.participant.tick()
            assert (await rig.market.observeAward(rig.ref))["status"] == "SUBMITTED"
            with pytest.raises(AdapterError):
                await rig.participant.tick()
            assert rig.journal.get(rig.ref)["phase"] == "RESULT_RECORDED"
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testLiveSdkRequiresExplicitProviderAndKey(tmp_path):
    async def scenario():
        from modules.adapters.execution.docker import DockerExecutor
        from modules.adapters.execution.sdk import AgentsExecutor

        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant, sdk=True)
        try:
            for provider, key in (("fixture", "isolated-key"), ("openai", None)):
                profile["runtime"]["provider"] = provider
                executor = AgentsExecutor(DockerExecutor(), apiKey=key)
                with pytest.raises(AdapterError, match="UNAVAILABLE"):
                    await executor.preflight(profile)
        finally:
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testLiveSdkPinsEndpointAndDoesNotRetryProviderError(tmp_path, monkeypatch):
    async def scenario():
        import httpx
        from openai import AsyncOpenAI

        from modules.adapters.execution import sdk

        requests = []

        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(503, json={"error": {"message": "Isolated outage"}})

        def createClient(**kwargs):
            return AsyncOpenAI(
                **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
            )

        monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-be-used.invalid/v1")
        monkeypatch.setattr(sdk, "AsyncOpenAI", createClient)
        rig = Layer2Rig(tmp_path)
        profile = executionProfile(rig.participant, sdk=True)
        profile["runtime"]["provider"] = "openai"
        profile["pricing"]["provider"] = "openai"
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, profile=profile)
        coordinator.agents = sdk.AgentsExecutor(coordinator.docker, apiKey="isolated-key")
        try:
            await coordinator.agents.preflight(profile)
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await drainExecution(coordinator)
            assert requests == ["https://api.openai.com/v1/responses"]
            assert rig.journal.get(rig.ref)["phase"] == "INTERRUPTED"
            report = coordinator.store.get("outbox", executionKey(rig.ref))["report"]
            assert report["completeness"] == "INCOMPLETE"
            assert all(i["quantity"] is None and i["costAtoms"] is None for i in report["items"])
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


def testWallCancellationDoesNotCancelCoordinatorTick(tmp_path):
    async def scenario():
        rig = Layer2Rig(tmp_path)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now)
        entered = asyncio.Event()

        async def delayed(*args):
            entered.set()
            await asyncio.Event().wait()

        coordinator.docker.run = delayed
        try:
            await rig.award()
            await rig.participant.receiveHint(rig.message)
            await rig.participant.tick()
            await rig.participant.tick()
            await asyncio.wait_for(entered.wait(), 5)
            run = coordinator.store.get("run", executionKey(rig.ref))
            rig.host.setClock(run["cutoff"])
            await coordinator.tick()
            await asyncio.gather(*coordinator.jobs.values(), return_exceptions=True)
            # A cancelled execution is an expected outcome, not cancellation of discovery.
            await coordinator.tick()
            assert not coordinator.jobs and coordinator.store.available() == 2
            assert rig.journal.get(rig.ref)["phase"] == "INTERRUPTED"
            outbox = coordinator.store.get("outbox", executionKey(rig.ref))
            assert outbox["context"]["status"] == "TIMED_OUT"
            assert outbox["report"]["completeness"] == "INCOMPLETE"
            steps = coordinator.store.steps(rig.ref)
            assert len(steps) == 1 and steps[0]["state"] == "UNKNOWN"
            assert steps[0]["chargeAtoms"] == "20000"
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("balance, admitted", [(19, 0), (20, 1)])
def testConcurrentParentsReserveFreshDepositsAndGas(tmp_path, balance, admitted):
    async def scenario():
        from modules.adapters.a2a.profile import awardHint
        from modules.agent_client.participant import makeCommand

        rig = Layer2Rig(tmp_path)
        rig.terms["delegation"] = {"maxDepth": 2, "maxChildren": 2}
        profile = executionProfile(rig.participant, manager=True)
        profile["delegation"].update(
            ownWorkReserveAtoms="0",
            childBudgetAtoms="5",
            depositAtoms="10",
            gasAtoms="10",
            templates=[list(rig.digests)],
        )
        profile["runtime"]["configDigest"] = executionDigest(profile)
        coordinator, _ = composeExecution(rig.participant, lambda: rig.host.now, profile=profile)

        async def availableFunds():
            await asyncio.sleep(0)
            return balance

        # Only the external wallet read is isolated; real plan validation/storage serialize funds.
        rig.market.availableFunds = availableFunds
        refs = [rig.ref, {"taskRef": rig.ref["taskRef"] | {"taskId": "2"}, "awardId": 1}]
        try:
            await rig.create()
            await rig.create()
            rig.host.setClock(1100)
            for index, ref in enumerate(refs):
                await rig.participant.prepareBid(
                    ref["taskRef"], "20", rig.sign, str(index + 1), "1200"
                )
            rig.host.setClock(1200)
            plans = []
            for index, ref in enumerate(refs):
                await rig.host.submit(
                    rig.config["requester"],
                    "allocate-" + str(index),
                    makeCommand("allocateTask", taskRef=ref["taskRef"]),
                )
                await rig.participant.receiveHint(
                    awardHint(await rig.market.observeAward(ref), "award-" + str(index))
                )
                await rig.participant.advance(ref)
                rig.journal.update(ref, phase="READY")
                coordinator.store.claim(ref, b'{"value":7}', rig.host.now, rig.host.now + 100)
                view = await rig.market.observeAward(ref)
                plan = coordinator.proposal(ref, view, profile, b'{"left":7,"right":9}')
                plans.append((ref, plan, view))
            results = await asyncio.gather(
                *(coordinator.freezePlan(*args) for args in plans), return_exceptions=True
            )
            assert sum(result is None for result in results) == admitted, results
            assert all(
                result is None
                or isinstance(result, AdapterError)
                and result.detail == "Fresh child funding unavailable"
                for result in results
            )
            runs = [run for _, run in coordinator.store.rows("run")]
            assert sum(int(run["depositReserved"]) for run in runs) == admitted * 10
            assert sum(coordinator.pendingFunds(run) for run in runs) == admitted * 20
            assert not rig.journal.nativeOperations()
        finally:
            await coordinator.close()
            await rig.http.aclose()
            rig.journal.close()

    asyncio.run(scenario())
