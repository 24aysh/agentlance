"""L5 offline composition, with synthetic prices explicitly isolated from live data."""

from copy import deepcopy

from apps.reference_agent.chain import DiscoveryRuntime
from modules.agent_client.economics import EconomicRuntime
from modules.economics.records import recordDigest
from tests.layer3_support import readJson
from tests.layer4_support import DiscoveredWorker


def economicConfig(participant):
    config = readJson("specs/fixtures/layer-5/economics.json")
    config["operator"] = participant.signer
    config["runtime"]["outputSchemaDigest"], config["runtime"]["validationPolicyDigest"] = (
        participant.supportedDigests
    )
    return config


def filterPolicy(participant):
    return {
        "version": "l5-fixture",
        "supportedDigests": list(participant.supportedDigests),
        "requiredSkills": ["structured-output-v1"],
        "advertisedSkills": ["structured-output-v1"],
        "minBudgetAtoms": "0",
        "maxBudgetAtoms": str(2**96 - 1),
        "leadTimeSeconds": 0,
        "allowRequesters": [],
        "denyRequesters": [],
    }


def usageReport(task, runtime, agentRef, operator, estimateId, observedAt, *, quantity=1, cost=10):
    ref = {"taskRef": task["taskRef"], "awardId": 1}
    report = {
        "schemaVersion": 1,
        "reportId": recordDigest(["report", ref, quantity, cost]),
        "executionRef": ref,
        "estimateId": estimateId,
        "observedAt": str(observedAt),
        "completeness": "COMPLETE",
        "items": [
            {
                "expenseId": recordDigest(["expense", ref, quantity, cost]),
                "category": "EXECUTION",
                "resource": "transform",
                "quantityUnit": "invocation",
                "quantity": {"numerator": str(quantity), "denominator": "1"},
                "costAtoms": str(cost),
                "unit": {"currency": "MON", "decimals": 18},
                "provenance": "OPERATOR_REPORTED",
                "providerRef": None,
            }
        ],
    }
    context = {
        "operator": operator,
        "agentRef": agentRef,
        "task": task,
        "runtime": runtime,
        "status": "COMPLETED",
        "zeroUsage": False,
        "evidence": "FIXTURE",
    }
    return report, context


async def drainEconomics(runtime, rounds=10):
    for _ in range(rounds):
        await runtime.tick()
        if runtime.active is not None:
            await runtime.active
        # The public pump observes completed work, but test time need not sleep.
        if all(
            row["disposition"] is not None for _, row in runtime.store.items("economic_candidates")
        ):
            return


async def runEconomicWorker(env):
    worker = DiscoveredWorker(env)
    config = economicConfig(worker.participant)
    config["overhead"]["gasAtoms"] = str(3 * env.market.maxGas * env.market.maxGasPriceWei)
    config["overhead"]["source"] = (
        "Three transactions at local gas/fee caps; validator paid by requester."
    )

    def compose():
        economics = EconomicRuntime(
            worker.participant,
            worker.policy,
            1,
            lambda permit: env.rig.sign("BidPermit", permit, "owner"),
            config,
            env.rig.now,
        )
        worker.runtime = DiscoveryRuntime(
            worker.participant,
            env.watcher,
            worker.policy,
            1,
            None,
            economics.signPermit,
            economics=economics,
        )
        return economics

    economics = compose()
    try:
        task = worker.create(budget=5 * 10**18, denominator=10**21)
        poor = worker.create()
        await worker.runtime.tick()
        await drainEconomics(economics)
        decisions = [row["decision"] for _, row in economics.store.items("economic_candidates")]
        decision = next(d for d in decisions if d["taskRef"] == task["taskRef"])
        assert decision["action"] == "BID", decisions
        assert next(d for d in decisions if d["taskRef"] == poor["taskRef"])["action"] == "ABSTAIN"
        assert economics.store.items("forecast_attempts") == []
        for intent in env.journal.nativeOperations():
            await env.applied(await env.market.readOperation(intent["operationId"]))
        await economics.close()
        await env.restart()
        worker.open()
        economics = compose()
        await worker.runtime.tick()
        await drainEconomics(economics)
        assert len(env.journal.nativeOperations()) == 1
        env.rig.advance(int(task["terms"]["biddingClose"]))
        env.command("allocateTask", {"taskRef": task["taskRef"]})
        for _ in range(10):
            env.finalize()
            await worker.runtime.tick()
            if env.journal.rows() and env.journal.rows()[0]["phase"] == "RESULT_RECORDED":
                break
        assert len(worker.calls) == 1 and env.journal.rows()[0]["phase"] == "RESULT_RECORDED"
        report, context = usageReport(
            task,
            config["runtime"],
            worker.participant.agentRef,
            config["operator"],
            decision["estimateId"],
            env.rig.now(),
        )
        assert await economics.history.recordUsage(report, context) == "STORED"
        assert await economics.history.recordUsage(report, context) == "UNCHANGED"
        evaluation = economics.history.evaluateForecasts([report["executionRef"]])
        assert evaluation["forecast"]["p80"] == {
            "covered": 1,
            "eligible": 1,
            "excluded": 0,
            "unavailableReason": None,
        }
        env.rig.advance(int(task["terms"]["validationBy"]))
        env.command("expireTask", {"taskRef": task["taskRef"]})
        settled = await env.market.readSettlement(task["taskRef"])
        await economics.history.attachSettlement(
            report["executionRef"], settled["receipt"], settled["stamp"]
        )
        await economics.history.attachSettlement(
            report["executionRef"], settled["receipt"], settled["stamp"]
        )
        assert len(economics.store.items("usage_receipts")) == 1
        assert settled["receipt"]["reason"] == "VALIDATOR_TIMEOUT"
        later = worker.create(budget=5 * 10**18, denominator=10**21)
        await worker.runtime.tick()
        await drainEconomics(economics)
        laterDecision = economics.store.get(
            "economic_candidates", economics.store.candidateKey(later["taskRef"])
        )["decision"]
        bundle = economics.store.get("economic_estimates", laterDecision["estimateId"])
        assert bundle["estimate"]["fallback"] == "HISTORY"
        frozen = deepcopy(bundle)
        await worker.runtime.tick()
        assert economics.store.get("economic_estimates", laterDecision["estimateId"]) == frozen
        return {
            "scope": "synthetic local-EVM economics, not live calibration",
            "decisions": decisions,
            "receipt": settled["receipt"],
            "evaluation": evaluation,
            "laterEstimate": bundle["estimate"],
            "workerInvocations": len(worker.calls),
            "paidForecastCalls": 0,
            "indexEnabled": False,
            "hintsEnabled": False,
            "transactionHashes": [op["transactionHash"] for op in env.journal.nativeOperations()],
        }
    finally:
        await economics.close()
        await worker.http.aclose()
