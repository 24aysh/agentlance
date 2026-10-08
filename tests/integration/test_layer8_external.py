"""Independent Node implementation, direct finalized RPC, real TLS and requester CLI."""

import asyncio

import httpx
import pytest

from modules.adapters.a2a.client import AgentClient
from modules.adapters.a2a.profile import awardHint, jsonBytes, strictJson
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from tests.layer3_support import localNode
from tests.layer4_support import Layer4Rig
from tests.layer7_support import ValidatorHarness, localKubo
from tests.layer8_support import (
    ExternalAgent,
    PublicTls,
    RpcDropProxy,
    appConfig,
    cli,
    cliWrite,
    currentRead,
)

pytestmark = pytest.mark.l3socket


async def independentScenario(rpc, directory, kubo=None):
    env = Layer4Rig(rpc, directory)
    public = PublicTls(directory, kubo)
    agent = ExternalAgent(env, directory, public)
    proxy = RpcDropProxy(str(env.rpc.client.base_url))
    agent.config["rpcUrl"] = proxy.url
    agent.path.write_bytes(jsonBytes(agent.config))
    http = createHttpClient(NetworkPolicy([public.origin, agent.origin]), str(public.certificate))
    validator = None
    feedback = None
    if kubo:

        def serve(request):
            import ssl

            response = httpx.get(
                str(request.url),
                verify=ssl.create_default_context(cafile=public.certificate),
                trust_env=False,
                timeout=10,
            )
            return httpx.Response(response.status_code, stream=httpx.ByteStream(response.content))

        validator = ValidatorHarness(env, directory, kubo, serve)
        feedback = validator.deployment[1]
    try:
        config = appConfig(
            env,
            directory,
            public,
            extraOrigins=[agent.origin],
            facts=env.facts | ({"feedbackPublisher": validator.deployment[0]} if validator else {}),
            feedback=feedback,
        )
        terms = public.terms(env.rig.terms())
        termsPath = directory / "terms.json"
        termsPath.write_bytes(jsonBytes(terms))
        await cli(
            config,
            "task",
            "validate",
            "--terms-file",
            str(termsPath),
            "--requester",
            env.rig.actors["requester"],
        )
        # Independent CLI processes on each call prove durable requester restart/replay.
        sent = await cli(
            config,
            "task",
            "create",
            "--terms-file",
            str(termsPath),
            "--request-id",
            "solo",
            "--broadcast",
        )
        env.finalize()
        created = await cli(
            config,
            "task",
            "create",
            "--terms-file",
            str(termsPath),
            "--request-id",
            "solo",
            "--broadcast",
        )
        assert sent["data"]["transactionHash"] == created["data"]["transactionHash"]
        ref = created["data"]["taskRef"]
        await agent.open()
        await agent.waitFor(ref, "BID")
        assert proxy.dropped.is_set()
        await agent.restart()
        env.rig.advance(int(terms["biddingClose"]))
        env.finalize()
        await cliWrite(
            env, config, "task", "allocate", "solo-allocate", "--task-ref", jsonBytes(ref).decode()
        )
        submitted = await agent.waitFor(ref, "SUBMITTED")
        await agent.restart()
        await agent.waitFor(ref, "SUBMITTED")
        card = await ContentStore(http, NetworkPolicy([agent.origin])).fetchBytes(
            agent.origin + "/.well-known/agent-card.json", 65536
        )
        client = AgentClient(card, http, env.chain.schema)
        message = awardHint(submitted, "after-completion")
        task = await client.sendHint(message)
        assert (
            await client.fetchArtifact(task, ContentStore(http, NetworkPolicy([agent.origin])))
            == b'{"value":7}\n'
        )
        assert await client.sendHint(message) == task
        assert len((directory / "external-invocations.jsonl").read_text().splitlines()) == 1
        inspected = await cli(
            config, "task", "show", "--task-ref", jsonBytes(ref).decode(), "--artifacts"
        )
        assert inspected["data"]["view"]["status"] == "SUBMITTED"
        assert inspected["data"]["auction"]["status"] == "MATCH"
        assert all(item["status"] == "VERIFIED" for item in inspected["data"]["artifacts"])
        settlement = withdrawal = None
        if validator:
            outcome = await validator.drain(ref)
            assert outcome["receipt"]["reason"] == "SUCCESS"
            index = outcome["feedback"]["publication"]["feedbackIndex"]
            await validator.restart()
            assert (await validator.drain(ref))["feedback"]["publication"]["feedbackIndex"] == index
            settlement = await currentRead(
                config, "task", "show", "--task-ref", jsonBytes(ref).decode(), "--artifacts"
            )
            assert settlement["data"]["feedback"]["status"] == "PUBLISHED"
            assert all(x["status"] == "VERIFIED" for x in settlement["data"]["artifacts"])
            payoutConfig = appConfig(
                env,
                directory,
                public,
                role="payout",
                facts=env.facts | {"feedbackPublisher": validator.deployment[0]},
                feedback=feedback,
                extraOrigins=[agent.origin],
            )
            credit = await currentRead(
                payoutConfig, "credit", "show", "--owner", env.rig.actors["payout"]
            )
            withdrawal = await cliWrite(
                env,
                payoutConfig,
                "credit",
                "withdraw",
                "payout",
                "--receiver",
                env.rig.actors["receiver"],
                "--amount-atoms",
                credit["data"]["credit"]["amountAtoms"],
            )
            assert withdrawal["data"]["confirmation"] == "FINALIZED_APPLIED"
            refund = await currentRead(
                config, "credit", "show", "--owner", env.rig.actors["requester"]
            )
            if int(refund["data"]["credit"]["amountAtoms"]):
                await cliWrite(
                    env,
                    config,
                    "credit",
                    "withdraw",
                    "refund",
                    "--receiver",
                    env.rig.actors["receiver"],
                    "--amount-atoms",
                    refund["data"]["credit"]["amountAtoms"],
                )
        return {
            "settlement": settlement,
            "withdrawal": withdrawal,
            "task": submitted["task"],
            "creation": created,
            "inspection": inspected,
            "journal": strictJson((directory / "external-journal.json").read_bytes()),
        }
    finally:
        if validator:
            await validator.close()
        await agent.close()
        await http.aclose()
        proxy.close()
        public.close()
        await env.close()


def testIndependentAgentDiscoversExecutesAndRestarts(tmp_path):
    with localNode(tmp_path) as rpc:
        result = asyncio.run(independentScenario(rpc, tmp_path))
    assert result["journal"]["halt"] is None
    assert len(result["journal"]["operations"]) == 3


def testIndependentAgentCanonicalSettlementAndWithdrawal(tmp_path):
    with localKubo() as kubo, localNode(tmp_path) as rpc:
        result = asyncio.run(independentScenario(rpc, tmp_path, kubo))
    assert result["settlement"]["data"]["view"]["receipt"]["reason"] == "SUCCESS"
