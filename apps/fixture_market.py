"""Local fixture host. Administrative control uses its parent's stdin, never A2A/HTTP."""

import argparse
import asyncio
from pathlib import Path

import uvicorn

from modules.adapters.a2a.profile import strictJson
from modules.adapters.fixtures import FixtureMarket, commandRecord, fixtureApp
from modules.agent_client.ports import ensure
from modules.domain.records import agentKey
from modules.market_core.state import CorePolicy


def loadMarket(config):
    root = Path(__file__).resolve().parents[1]
    schema = strictJson((root / "specs/schemas/protocol.schema.json").read_bytes())
    types = strictJson((root / "specs/signing/types.json").read_bytes())
    policy = CorePolicy(
        int(config["agentRef"]["chainId"]),
        config["taskRef"]["market"],
        config["agentRef"]["identityRegistry"],
        config["validator"],
    )
    identity = {
        "owner": config["owner"],
        "verifiedWallet": config["payout"],
        "registrationUri": config["marketOrigin"] + "/fixture/content/registration.json",
        "ownerHasCode": False,
    }
    contents = {
        name: (Path(config["bundleDir"]) / name).read_bytes()
        for name in ("registration.json", "input.json", "output-schema.json", "policy.json")
    }
    return FixtureMarket(
        policy,
        schema,
        types,
        config["sessionId"],
        {agentKey(config["agentRef"]): identity},
        contents,
    )


async def main(config):
    market = loadMarket(config)
    actors = {
        config["actorToken"]: {
            "address": config["signer"],
            "commands": ["submitBid", "acceptAward", "createChildTask", "submitResult"],
        }
    }
    app = fixtureApp(market, config["readToken"], actors)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config["marketPort"],
            ssl_certfile=config["certificate"],
            ssl_keyfile=config["tlsKey"],
            log_level="warning",
            timeout_graceful_shutdown=1,
            access_log=False,
        )
    )

    async def control(value):
        operation = value["operation"]
        if operation == "shutdown":
            ensure(value == {"operation": "shutdown"}, "Shutdown arguments")
            server.should_exit = True
            return {"ok": True}
        if operation == "create":
            return await market.submit(
                config["requester"],
                value["operationId"],
                commandRecord("createTask", terms=value["terms"]),
                value["terms"]["budgetAtoms"],
            )
        if operation == "clock":
            market.setClock(value["now"])
        elif operation == "pending":
            market.pendingCommands = set(value["commands"])
        elif operation == "finalize":
            market.finalize()
        elif operation in {"allocateTask", "expireTask"}:
            return await market.submit(
                config["requester"],
                value["operationId"],
                commandRecord(operation, taskRef=value["taskRef"]),
            )
        elif operation == "inspect":
            return {
                "events": market.events,
                "view": market.view(value["taskRef"]),
                "credits": market.state.credits,
                "depositedAtoms": str(market.state.depositedAtoms),
            }
        else:
            ensure(operation in {"clock", "pending", "finalize"}, "Unknown local control")
        return {"ok": True}

    from modules.adapters.fixtures import controlLoop

    runner = asyncio.create_task(controlLoop(control))
    try:
        await server.serve()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    asyncio.run(main(strictJson(Path(args.config).read_bytes())))
