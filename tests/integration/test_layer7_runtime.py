import asyncio

import httpx
import pytest

from modules.agent_client.signing import contentDigest
from tests.layer3_support import localNode
from tests.layer4_support import DiscoveredWorker, Layer4Rig
from tests.layer7_support import ValidatorHarness, localKubo, submit


@pytest.mark.parametrize("answer,reason", [(7, "SUCCESS"), (8, "VALIDATION_FAILED")])
def testProductionValidatorKuboSettlementAndExport(tmp_path, answer, reason, caplog):
    caplog.set_level("INFO", logger="modules.validation.runtime")

    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(
            env,
            tmp_path,
            kubo,
            lambda req: httpx.Response(200, stream=httpx.ByteStream(worker.blobs[req.url.path])),
        )
        try:
            task = submit(env, worker, answer)
            result = await validator.drain(task["taskRef"])
            assert result["receipt"]["reason"] == reason
            assert result["feedback"]["publication"]["feedbackIndex"] == "1"
            assert result["feedback"]["publication"]["feedback"]["value"] == (answer == 7)
            key, job = validator.store.rows("job")[0]
            evidence = job["evidence"]
            raw = await validator.content.fetchBytes(evidence["uri"], 1048576, evidence["digest"])
            assert contentDigest(raw) == evidence["digest"]
            original = job["record"]
            await validator.restart()
            await validator.tick()
            assert validator.store.get("job", key)["record"] == original
            assert (await validator.publisher.readPublication(task["taskRef"]))[
                "feedbackIndex"
            ] == "1"
            messages = "\n".join(
                r.getMessage() for r in caplog.records if r.name == "modules.validation.runtime"
            )
            for event in ("validation_admitted", "validation_stage", "feedback_published"):
                assert "event=" + event in messages
            assert "task_id=" + task["taskRef"]["taskId"] in messages
            assert original["signature"] not in messages and evidence["uri"] not in messages
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


@pytest.mark.parametrize(
    "stage",
    [
        "FETCHED",
        "EVIDENCE_READY",
        "EVIDENCE_PUBLISHED",
        "PREPARED_SIGNATURE",
        "SIGNED",
        "RELAY_PENDING",
        "SETTLED",
    ],
)
def testValidatorRestartAtDurableBoundaries(tmp_path, stage):
    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(
            env,
            tmp_path,
            kubo,
            lambda req: httpx.Response(200, stream=httpx.ByteStream(worker.blobs[req.url.path])),
        )
        try:
            task = submit(env, worker)
            for _ in range(20):
                await validator.tick()
                key, job = validator.store.rows("job")[0]
                if job["stage"] == stage or (
                    stage == "PREPARED_SIGNATURE"
                    and job["record"] is not None
                    and job["record"]["signature"] == "0x"
                ):
                    break
            else:
                raise AssertionError(job)
            before = job
            await validator.restart()
            result = await validator.drain(task["taskRef"])
            after = validator.store.get("job", key)
            assert result["receipt"]["reason"] == "SUCCESS"
            assert (
                after["attempts"]["evaluate"] == before["attempts"].get("evaluate", 0)
                or stage == "FETCHED"
            )
            if before["record"]:
                assert after["record"]["nonce"] == before["record"]["nonce"]
            if before["evidenceDigest"]:
                assert after["evidenceDigest"] == before["evidenceDigest"]
            assert result["feedback"]["publication"]["feedbackIndex"] == "1"
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


def testValidatorUnavailableExpiresWithoutInventedFailure(tmp_path):
    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(env, tmp_path, kubo, lambda req: httpx.Response(503))
        try:
            task = submit(env, worker)
            for _ in range(7):
                await validator.tick()
            key, job = validator.store.rows("job")[0]
            assert job["stage"] == "EXHAUSTED" and job["attempts"]["fetch"] == 3
            assert job["record"] is None
            await validator.restart()
            await validator.tick()
            assert validator.store.get("job", key)["attempts"]["fetch"] == 3
            await validator.runtime.grantRetry(task["taskRef"], "job")
            await validator.tick()
            assert validator.store.get("job", key)["attempts"]["fetch"] == 4
            assert validator.store.get("job", key)["retryGrants"] == 3
            env.rig.advance(int(task["terms"]["validationBy"]))
            outcome = await validator.drain(task["taskRef"])
            assert outcome["receipt"]["reason"] == "VALIDATOR_TIMEOUT"
            assert outcome["receipt"]["refundAtoms"] == "100"
            assert outcome["feedback"]["state"] == "NOT_APPLICABLE"
            assert not (await validator.publisher.readPublication(task["taskRef"]))["published"]
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


def testLostBroadcastAndPublisherQueueRecovery(tmp_path):
    from modules.agent_client.ports import AdapterError
    from modules.economics.records import recordDigest

    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(
            env,
            tmp_path,
            kubo,
            lambda req: httpx.Response(200, stream=httpx.ByteStream(worker.blobs[req.url.path])),
        )
        try:
            task = submit(env, worker)
            call = validator.rpc.call
            lost = []

            async def dropResponse(method, *args):
                value = await call(method, *args)
                if method == "eth_sendRawTransaction" and not lost:
                    lost.append(value)
                    raise AdapterError("UNAVAILABLE", "injected lost broadcast response")
                return value

            validator.rpc.call = dropResponse
            for _ in range(15):
                await validator.tick()
                if lost:
                    break
            assert lost
            await validator.restart()
            result = await validator.drain(task["taskRef"])
            assert result["receipt"]["reason"] == "SUCCESS"
            native = validator.journal.nativeOperations()
            assert len({x["transaction"]["nonce"] for x in native if x["transaction"]}) == 2
            with validator.store.db:
                validator.store.db.execute(
                    "DELETE FROM validation_records WHERE kind IN ('cursor','export')"
                )
            await validator.tick()
            await validator.tick()
            assert (
                validator.store.get("export", recordDigest(task["taskRef"]))["publication"][
                    "feedbackIndex"
                ]
                == "1"
            )
            # Changed implementation observation suspends exports but does not change settlement.
            validator.publisher.verification["codeObservations"][0]["codeHash"] = "0x" + "00" * 32
            with pytest.raises(AdapterError, match="code changed"):
                await validator.publisher.readPublication(task["taskRef"])
            assert (await validator.market.readTask(task["taskRef"]))["receipt"] == result[
                "receipt"
            ]
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


def testEvaluatorBindingCannotAuthorizeAnotherResult(tmp_path):
    from modules.validation.evaluator import evidenceBinding, evidenceBytes

    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(
            env,
            tmp_path,
            kubo,
            lambda req: httpx.Response(200, stream=httpx.ByteStream(worker.blobs[req.url.path])),
        )
        try:
            task = submit(env, worker)
            await validator.tick()
            key, job = validator.store.rows("job")[0]
            assert job["stage"] == "FETCHED"
            wrong = evidenceBinding(job["view"]["task"], job["view"]["result"]) | {
                "verdict": "PASS",
                "reason": "PASS",
                "failedPredicate": None,
            }
            wrong["resultDigest"] = "0x" + "ab" * 32

            async def wrongResult(*args):
                return evidenceBytes(wrong)

            validator.runtime.docker.run = wrongResult
            await validator.tick()
            job = validator.store.get("job", key)
            assert job["stage"] == "EXHAUSTED" and job["record"] is None
            assert job["evidenceDigest"] is None
            assert (await validator.market.readTask(task["taskRef"]))["status"] == "SUBMITTED"
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))


def testExportOwnerConflictRecoversWithoutChangingReceipt(tmp_path):
    from modules.economics.records import recordDigest

    async def run(rpc, kubo):
        env = Layer4Rig(rpc, tmp_path)
        worker = DiscoveredWorker(env)
        validator = ValidatorHarness(
            env,
            tmp_path,
            kubo,
            lambda req: httpx.Response(200, stream=httpx.ByteStream(worker.blobs[req.url.path])),
        )
        try:
            task = submit(env, worker)
            env.rig.external(
                env.rig.registry,
                "setIdentity",
                ["uint256", "address", "address"],
                [0, validator.publisher.address, env.rig.actors["payout"]],
            )
            await validator.drain(task["taskRef"], feedback=False)
            for _ in range(10):
                await validator.tick()
            key = recordDigest(task["taskRef"])
            job = validator.store.get("export", key)
            assert job["state"] == "PENDING" and job["attempts"] == 3
            assert "owner/operator" in job["diagnostic"]
            receipt = (await validator.market.readTask(task["taskRef"]))["receipt"]
            assert receipt["reason"] == "SUCCESS"
            env.rig.external(
                env.rig.registry,
                "setIdentity",
                ["uint256", "address", "address"],
                [0, env.rig.actors["owner"], env.rig.actors["payout"]],
            )
            env.finalize()
            await validator.runtime.grantRetry(task["taskRef"], "export")
            outcome = await validator.drain(task["taskRef"])
            assert outcome["receipt"] == receipt
            assert outcome["feedback"]["publication"]["feedbackIndex"] == "1"
            assert (
                len(
                    [
                        op
                        for op in validator.journal.nativeOperations()
                        if op.get("destination") and op["transactionHash"]
                    ]
                )
                == 1
            )
        finally:
            await validator.close()
            await worker.http.aclose()
            await env.close()

    with localNode(tmp_path) as rpc, localKubo() as kubo:
        asyncio.run(run(rpc, kubo))
