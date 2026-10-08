"""One fixture-compatible agent process, with durable run recovery."""

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.reference_agent.worker import checkFixtureInput, executeFixture
from apps.runtime_logging import configureLogging
from modules.adapters.a2a.profile import strictJson
from modules.adapters.a2a.server import agentApp
from modules.adapters.fixtures import FixtureClient, controlLoop
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from modules.adapters.storage.journal import Journal
from modules.agent_client.participant import Participant
from modules.agent_client.ports import ensure
from modules.agent_client.signing import buildBidPermitTypedData, contentDigest


async def main(config):
    root = Path(__file__).resolve().parents[2]
    schema = strictJson((root / "specs/schemas/protocol.schema.json").read_bytes())
    types = strictJson((root / "specs/signing/types.json").read_bytes())
    bundle = Path(config["bundleDir"])
    cardBytes = (bundle / "card.json").read_bytes()
    signer = Account.from_key(config["executionKey"]).address.lower()
    ensure(signer == config["signer"], "Configured signer key mismatch")
    policy = NetworkPolicy([config["marketOrigin"], config["agentOrigin"]])
    http = createHttpClient(policy, config["certificate"])
    market = FixtureClient(
        http,
        config["marketOrigin"],
        config["sessionId"],
        config["readToken"],
        config["actorToken"],
        schema,
    )
    journal = Journal(
        config["database"],
        {
            "agentRef": config["agentRef"],
            "market": config["taskRef"]["market"],
            "sessionId": config["sessionId"],
        },
    )
    journal.storeContent(cardBytes)
    content = ContentStore(http, policy, journal, config["agentOrigin"])

    def execute(raw):
        # Test-operator evidence outside the A2A wire: append before entering the worker.
        with open(config["invocationLog"], "ab", buffering=0) as log:
            log.write(b"invoke\n")
            os.fsync(log.fileno())
        return executeFixture(raw)

    participant = Participant(
        config["agentRef"],
        signer,
        market,
        market,
        content,
        journal,
        schema,
        execute,
        checkFixtureInput,
        (
            contentDigest((bundle / "output-schema.json").read_bytes()),
            contentDigest((bundle / "policy.json").read_bytes()),
        ),
    )

    def sign(permit):
        domain = {
            "name": "AgentLance",
            "version": "1",
            "chainId": int(config["agentRef"]["chainId"]),
            "verifyingContract": config["taskRef"]["market"],
        }
        typed = buildBidPermitTypedData(permit, domain, types)
        return (
            "0x"
            + Account.sign_message(
                encode_typed_data(full_message=typed), config["ownerKey"]
            ).signature.hex()
        )

    async def control(value):
        if value == {"operation": "shutdown"}:
            server.should_exit = True
            return {"ok": True}
        ensure(value == {"operation": "bid"}, "Only explicit fixture bid control supported")
        return await participant.prepareBid(
            config["taskRef"], config["bidAtoms"], sign, "1", "1200"
        )

    backgroundErrors = []

    async def reconcile():
        try:
            while True:
                await participant.tick()
                await asyncio.sleep(2)
        except Exception as error:
            backgroundErrors.append(error)
            server.should_exit = True

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(reconcile()), asyncio.create_task(controlLoop(control))]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = agentApp(participant, cardBytes, lifespan)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config["agentPort"],
            ssl_certfile=config["certificate"],
            ssl_keyfile=config["tlsKey"],
            log_level="warning",
            timeout_graceful_shutdown=1,
            access_log=False,
        )
    )
    try:
        await server.serve()
        if backgroundErrors:
            raise RuntimeError("Agent reconciliation stopped unexpectedly") from backgroundErrors[0]
    finally:
        await http.aclose()
        journal.close()


if __name__ == "__main__":
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("AGENTLANCE_CONFIG"))
    parser.add_argument("--mode", choices=("fixture", "monad"), default="fixture")
    args = parser.parse_args()
    if not args.config:
        parser.error("--config or AGENTLANCE_CONFIG is required")
    if args.mode == "monad":
        from apps.reference_agent.chain import main as chainMain

        configureLogging()
        asyncio.run(chainMain(strictJson(Path(args.config).read_bytes())))
    else:
        asyncio.run(main(strictJson(Path(args.config).read_bytes())))
