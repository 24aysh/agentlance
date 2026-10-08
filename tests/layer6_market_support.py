"""Actual child markets with separate bidders; only content and fixture verdicts are isolated."""

import asyncio
import sys
from pathlib import Path

import httpx

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.storage.journal import executionKey
from modules.agent_client.economics import EconomicRuntime
from modules.agent_client.signing import contentDigest
from modules.execution.rules import executionDigest
from tests.layer3_support import readJson
from tests.layer4_support import DiscoveredWorker, Layer4Rig
from tests.layer5_support import drainEconomics, economicConfig
from tests.layer6_process import PublicContent, publicTransport
from tests.layer6_support import composeExecution, drainExecution, executionProfile, parentFixtures


class WorkerProcess:
    async def open(self, config, directory, number):
        path = directory / f"worker-{number}.json"
        path.write_bytes(jsonBytes(config))
        self.log = (directory / f"worker-{number}.log").open("wb")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.layer6_process",
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
            limit=4 * 1048576,
        )
        assert strictJson(await asyncio.wait_for(self.process.stdout.readline(), 20))["ready"]
        return self

    async def tick(self):
        self.process.stdin.write(b'{"command":"tick"}\n')
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), 60)
        assert raw, "Worker exited; inspect worker log"
        return strictJson(raw)

    async def close(self):
        if self.process.returncode is None:
            self.process.stdin.write(b'{"command":"close"}\n')
            await self.process.stdin.drain()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.log.close()


def publishRegistration(env, directory, agentId, ownerName, payoutName):
    env.rig.register(agentId, ownerName, payoutName)
    origin = f"https://agent-{agentId}.example"
    card = readJson("specs/fixtures/layer-2/card.json")
    card["supportedInterfaces"][0]["url"] = origin + "/a2a"
    registration = readJson("specs/fixtures/layer-2/registration.json")
    registration["services"][0]["endpoint"] = origin + "/card.json"
    registration["registrations"] = [
        {"agentId": agentId, "agentRegistry": f"eip155:31337:{env.rig.registry}"}
    ]
    for name, value in (("card", card), ("registration", registration)):
        (directory / contentDigest((origin + "/" + name + ".json").encode())).write_bytes(
            jsonBytes(value)
        )
    env.rig.external(
        env.rig.registry,
        "setURI",
        ["uint256", "string"],
        [agentId, origin + "/registration.json"],
        caller=ownerName,
    )


def settleFixture(env, task, view, verdict="PASS"):
    ref = {"taskRef": task["taskRef"], "awardId": 1}
    record = {
        "schemaVersion": 1,
        "executionRef": ref,
        "agentRef": view["winningBid"]["offer"]["agentRef"],
        "resultDigest": view["result"]["artifact"]["digest"],
        "validationPolicyDigest": task["terms"]["validationPolicy"]["digest"],
        "verdict": verdict,
        "evidence": task["terms"]["input"],
        "validator": env.rig.actors["validator"],
        "nonce": task["taskRef"]["taskId"],
        "expiry": task["terms"]["validationBy"],
        "signature": "0x",
    }
    unsigned = {k: v for k, v in record.items() if k not in {"schemaVersion", "signature"}}
    record["signature"] = env.rig.sign("ValidationVerdict", unsigned, "validator")
    env.command("settleVerdict", {"record": record})


async def runDelegation(rpc, directory, failure=None, validatorFactory=None):
    env = Layer4Rig(rpc, directory)
    worker = DiscoveredWorker(env)
    public = directory / "public"
    public.mkdir()
    for path, raw in worker.blobs.items():
        (public / contentDigest(("https://agent.example" + path).encode())).write_bytes(raw)
    await worker.http.aclose()
    worker.http = httpx.AsyncClient(transport=publicTransport(public))
    worker.blobs.update(
        dict(
            zip(
                ("/input.json", "/output-schema.json", "/policy.json"),
                parentFixtures(),
                strict=True,
            )
        )
    )
    for path, raw in worker.blobs.items():
        (public / contentDigest(("https://agent.example" + path).encode())).write_bytes(raw)
    worker.policy["supportedDigests"] = [
        contentDigest(worker.blobs[p]) for p in ("/output-schema.json", "/policy.json")
    ]
    worker.open()
    participant = worker.participant
    participant.content = PublicContent(
        worker.http,
        participant.content.policy,
        env.journal,
        "https://agent.example",
        directory=public,
    )
    from apps.reference_agent.container_worker import transform

    def checkParent(raw):
        transform({"kind": "SOLO", "input": strictJson(raw)})

    participant.checkInput = checkParent
    profile = executionProfile(participant, manager=True)
    profile["delegation"]["templates"] = [
        [
            contentDigest(Path("specs/fixtures/layer-2", f).read_bytes())
            for f in ("output-schema.json", "policy.json")
        ]
    ]
    profile["delegation"].update(childBudgetAtoms=str(2 * 10**18), depositAtoms=str(4 * 10**18))
    profile["delegation"]["gasAtoms"] = str(6 * env.market.maxGas * env.market.maxGasPriceWei)
    profile["runtime"]["configDigest"] = executionDigest(profile)
    coordinator, _ = composeExecution(participant, env.rig.now, profile=profile)
    config = economicConfig(participant)
    config.update(runtime=profile["runtime"], pricing=profile["pricing"])
    economics = EconomicRuntime(
        participant,
        worker.policy,
        2,
        lambda p: env.rig.sign("BidPermit", p, "owner"),
        config,
        env.rig.now,
    )
    coordinator.history = economics.history
    worker.runtime.economics = economics
    worker.runtime.bidAtoms = None
    validator = await validatorFactory(env, public) if validatorFactory else None
    processes = []
    snapshots = []
    crashes = 0

    class InjectedCrash(BaseException):
        pass

    def armCrash():
        if failure != "restart":
            return
        if crashes == 0:

            async def freeze(*args):
                raise InjectedCrash()

            coordinator.freezePlan = freeze
        elif crashes == 1:
            publish = participant.publishChild

            async def child(*args):
                await publish(*args)
                raise InjectedCrash()

            participant.publishChild = child
        elif crashes == 2:
            publishResult = participant.content.publishResult

            def artifact(*args):
                publishResult(*args)
                raise InjectedCrash()

            participant.content.publishResult = artifact

    async def driveParent():
        nonlocal coordinator, economics, participant, crashes
        try:
            await worker.runtime.tick()
            await drainExecution(coordinator)
        except InjectedCrash:
            crashes += 1
            await coordinator.close()
            await economics.close()
            await env.restart()
            worker.open()
            participant = worker.participant
            participant.content = PublicContent(
                worker.http,
                participant.content.policy,
                env.journal,
                "https://agent.example",
                directory=public,
            )
            participant.checkInput = checkParent
            coordinator, _ = composeExecution(participant, env.rig.now, profile=profile)
            economics = EconomicRuntime(
                participant,
                worker.policy,
                2,
                lambda p: env.rig.sign("BidPermit", p, "owner"),
                config,
                env.rig.now,
            )
            coordinator.history = economics.history
            worker.runtime.economics = economics
            worker.runtime.bidAtoms = None
            armCrash()

    armCrash()
    try:
        if failure is None:
            for agentId, owner, signer, payout, preference in [
                (1, "childOwner", "childSigner", "childPayout", 3),
                (2, "other", "relayer", "receiver", 1),
                (3, "payout", "receiver", "other", None),
            ]:
                publishRegistration(env, public, agentId, owner, payout)
                workerConfig = {
                    "rpc": str(rpc.client.base_url),
                    "facts": env.facts,
                    "genesis": env.genesis,
                    "verification": env.verification,
                    "agentRef": env.rig.agentRef(agentId),
                    "ownerKey": "0x" + env.rig.keys[owner].hex(),
                    "signerKey": "0x" + env.rig.keys[signer].hex(),
                    "database": str(directory / f"worker-{agentId}.sqlite"),
                    "public": str(public),
                    "preference": preference,
                }
                processes.append(await WorkerProcess().open(workerConfig, directory, agentId))
        task = worker.create(budget=5 * 10**18, denominator=10**21, delegation=True)
        await worker.runtime.tick()
        await drainEconomics(economics)
        for intent in env.journal.nativeOperations():
            await env.applied(await env.market.readOperation(intent["operationId"]))
        env.rig.advance(int(task["terms"]["biddingClose"]))
        env.command("allocateTask", {"taskRef": task["taskRef"]})
        ref = {"taskRef": task["taskRef"], "awardId": 1}
        for _ in range(12):
            env.rig.advance(env.rig.now() + 2)
            env.finalize()
            await driveParent()
            run = coordinator.store.get("run", executionKey(ref))
            if run and len([c for c in run["children"].values() if c.get("taskRef")]) == 2:
                break
        run = coordinator.store.get("run", executionKey(ref))
        assert run and run["stage"] == "WAITING_CHILDREN", (run, env.journal.get(ref))
        assert len(run["children"]) == 2, run
        children = [c["task"] for c in run["children"].values()]
        if processes:
            # Each process discovers tasks independently from finalized logs.
            for process in processes:
                snapshots.append(await process.tick())
                env.finalize()
            for process in processes:
                snapshots.append(await process.tick())
                env.finalize()
        elif failure in {"failed", "validator-timeout", "unavailable", "parent-failure"}:
            # Fixture native bidder, used only to drive failure outcome cases.
            env.rig.register(1, "childOwner", "childPayout")
            for child in children:
                offer = env.rig.bid(
                    int(child["taskRef"]["taskId"]),
                    agentId=1,
                    amount=20,
                    signer="childSigner",
                    payout="childPayout",
                )
                env.command(
                    "submitBid",
                    offer,
                    caller="childOwner",
                )
        env.rig.advance(max(int(c["terms"]["biddingClose"]) for c in children))
        env.finalize()
        for _ in range(10):
            env.rig.advance(env.rig.now() + 2)
            await driveParent()
            env.finalize()
            views = [await env.market.readTask(c["taskRef"]) for c in children]
            if all(v["status"] != "OPEN" for v in views):
                break
        if processes:
            for _ in range(10):
                for process in processes:
                    snapshots.append(await process.tick())
                    env.finalize()
                views = [await env.market.readTask(c["taskRef"]) for c in children]
                if all(v["status"] == "SUBMITTED" for v in views):
                    break
            assert all(v["status"] == "SUBMITTED" for v in views), views
            for child, view in zip(children, views, strict=True):
                if validator:
                    await validator.settle(child)
                else:
                    settleFixture(env, child, view)
        elif failure in {"failed", "validator-timeout", "unavailable", "parent-failure"}:
            for child in children:
                childRef = {"taskRef": child["taskRef"], "awardId": 1}
                env.command(
                    "acceptAward",
                    {"executionRef": childRef, "ownWorkReserveAtoms": "0"},
                    caller="childSigner",
                )
                artifact = child["terms"]["input"]
                env.command(
                    "submitResult",
                    {"executionRef": childRef, "artifact": artifact},
                    caller="childSigner",
                )
                if failure != "validator-timeout" and (
                    failure != "parent-failure" or child == children[0]
                ):
                    settleFixture(
                        env,
                        child,
                        await env.market.readTask(child["taskRef"]),
                        verdict="FAIL" if failure == "failed" else "PASS",
                    )
            if failure == "validator-timeout":
                env.rig.advance(max(int(c["terms"]["validationBy"]) for c in children))
                env.finalize()
            elif failure == "unavailable":
                for child in children:
                    (public / child["terms"]["input"]["digest"]).unlink(missing_ok=True)
                    env.journal.db.execute(
                        "DELETE FROM content WHERE digest=?", (child["terms"]["input"]["digest"],)
                    )
                env.journal.db.commit()
            elif failure == "parent-failure":
                env.rig.advance(int(task["terms"]["resultBy"]))
                env.command("expireTask", {"taskRef": task["taskRef"]})
                await env.watcher.scanMarket()
                await worker.runtime.reconciler.tick()
        for _ in range(12):
            env.rig.advance(env.rig.now() + 2)
            env.finalize()
            await driveParent()
            view = await env.market.readTask(task["taskRef"])
            if view["status"] in {"SUBMITTED", "SETTLED"} and view["activeChildren"] == 0:
                break
        view = await env.market.readTask(task["taskRef"])
        if failure != "parent-failure":
            assert view["status"] == "SUBMITTED", (
                view,
                coordinator.store.get("run", executionKey(ref)),
                env.journal.get(ref),
            )
            assert strictJson(env.journal.readContent(view["result"]["artifact"]["digest"])) == {
                "left": 7,
                "right": 9,
            }
        receipts = [(await env.market.readTask(c["taskRef"]))["receipt"] for c in children]
        if failure == "parent-failure":
            assert int(receipts[0]["paidAtoms"]) > 0
            assert receipts[1]["reason"] == "VALIDATOR_TIMEOUT"
            assert int(receipts[1]["settledAt"]) > int(view["receipt"]["settledAt"])
            assert env.rig.read("readCredit", [env.rig.actors["childPayout"]])[0] == int(
                receipts[0]["paidAtoms"]
            )
            assert view["activeChildren"] == 0
        if failure == "restart":
            assert crashes == 3
            assert len(coordinator.store.steps(ref)) == 4
        if processes:
            assert sum(bool(s["steps"]) for s in snapshots) > 0
            assert len({r["winner"]["agentId"] for r in receipts}) == 2
        validation = None
        reconciliation = None
        if failure == "parent-failure":
            await env.watcher.scanMarket()
            await worker.runtime.reconciler.tick()
            reconciliation = worker.runtime.reconciler.readReconciliation(ref)
        if validator:
            validation = await validator.settle(task)
            await worker.runtime.tick()
            reconciliation = worker.runtime.reconciler.readReconciliation(ref)
        return {
            "validation": validation,
            "reconciliation": reconciliation,
            "reconciliationVersions": [
                row for _, row in worker.runtime.reconciler.store.rows("reconciliation")
            ],
            "scope": "local EVM + isolated content/model; not live qualification",
            "failure": failure,
            "result": view["result"],
            "receipts": receipts,
            "independentProcesses": len(processes),
            "childDeposits": sum(
                op["command"]["command"] == "createChildTask"
                for op in env.journal.nativeOperations()
            ),
            "steps": len(coordinator.store.steps(ref)),
            "usage": coordinator.store.get("outbox", executionKey(ref)),
        }
    finally:
        if validator:
            await validator.close()
        for process in processes:
            await process.close()
        await coordinator.close()
        await economics.close()
        await worker.http.aclose()
        await env.close()


async def runSolo(rpc, directory, validatorFactory=None):
    env = Layer4Rig(rpc, directory)
    worker = DiscoveredWorker(env)
    coordinator, model = composeExecution(worker.participant, env.rig.now, sdk=True)
    config = economicConfig(worker.participant)
    config.update(runtime=coordinator.profile["runtime"], pricing=coordinator.profile["pricing"])
    economics = EconomicRuntime(
        worker.participant,
        worker.policy,
        2,
        lambda p: env.rig.sign("BidPermit", p, "owner"),
        config,
        env.rig.now,
    )
    coordinator.history = economics.history
    worker.runtime.economics = economics
    worker.runtime.bidAtoms = None
    validator = None
    if validatorFactory:
        public = directory / "public"
        public.mkdir()
        for path, raw in worker.blobs.items():
            (public / contentDigest(("https://agent.example" + path).encode())).write_bytes(raw)
        worker.participant.content = PublicContent(
            worker.http,
            worker.participant.content.policy,
            env.journal,
            "https://agent.example",
            directory=public,
        )
        validator = await validatorFactory(env, public)
    try:
        task = worker.create(budget=5 * 10**18, denominator=10**21)
        await worker.runtime.tick()
        await drainEconomics(economics)
        for op in env.journal.nativeOperations():
            await env.applied(await env.market.readOperation(op["operationId"]))
        env.rig.advance(int(task["terms"]["biddingClose"]))
        env.command("allocateTask", {"taskRef": task["taskRef"]})
        ref = {"taskRef": task["taskRef"], "awardId": 1}
        for _ in range(8):
            env.rig.advance(env.rig.now() + 2)
            env.finalize()
            await worker.runtime.tick()
            await drainExecution(coordinator)
            if env.journal.get(ref) and env.journal.get(ref)["phase"] == "RESULT_RECORDED":
                break
        assert env.journal.get(ref)["phase"] == "RESULT_RECORDED"
        assert model.calls == 2 and worker.calls == []
        await coordinator.tick()
        report = coordinator.store.get("outbox", executionKey(ref))
        assert report["state"] == "DELIVERED" and report["report"]["estimateId"] is not None
        evaluation = economics.history.evaluateForecasts([ref])
        assert evaluation["missing"] == 0 and evaluation["excluded"] == 0
        view = await env.market.readTask(task["taskRef"])
        validation = None
        reconciliation = None
        if validator:
            validation = await validator.settle(task)
            await worker.runtime.tick()
            reconciliation = worker.runtime.reconciler.readReconciliation(ref)
        else:
            settleFixture(env, task, view)
        return {
            "validation": validation,
            "reconciliation": reconciliation,
            "executionRef": ref,
            "modelCalls": model.calls,
            "result": view["result"],
            "usage": report,
            "evaluation": evaluation,
            "scope": "isolated model, real SDK/Docker/local EVM",
        }
    finally:
        if validator:
            await validator.close()
        await coordinator.close()
        await economics.close()
        await worker.http.aclose()
        await env.close()
