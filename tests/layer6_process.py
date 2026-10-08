"""Isolated local-EVM worker process. stdin pumps observation, never assigns a task."""

import asyncio
import sys
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.reference_agent.chain import DiscoveryRuntime
from apps.reference_agent.worker import checkFixtureInput, executeFixture
from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.registry.chain import MonadRegistry
from modules.adapters.storage.content import ContentStore, NetworkPolicy
from modules.adapters.storage.journal import Journal
from modules.agent_client.discovery import MarketWatcher
from modules.agent_client.economics import EconomicRuntime
from modules.agent_client.participant import Participant
from modules.agent_client.signing import buildBidPermitTypedData, contentDigest
from tests.layer3_support import Rpc, WireCodec, readJson
from tests.layer5_support import drainEconomics, economicConfig, filterPolicy
from tests.layer6_support import composeExecution, drainExecution


class PublicContent(ContentStore):
    def __init__(self, *args, directory, **kwargs):
        super().__init__(*args, **kwargs)
        self.directory = directory

    def publishContent(self, raw):
        ref = super().publishContent(raw)
        (self.directory / ref["digest"]).write_bytes(raw)
        return ref

    def publishResult(self, executionRef, raw):
        ref = super().publishResult(executionRef, raw)
        (self.directory / ref["digest"]).write_bytes(raw)
        return ref


def publicTransport(directory):
    def serve(request):
        uri = str(request.url)
        name = (
            "0x" + request.url.path.split("/")[-1].removesuffix(".json")
            if request.url.path.startswith("/artifacts/")
            else contentDigest(uri.encode())
        )
        path = directory / name
        return (
            httpx.Response(200, stream=httpx.ByteStream(path.read_bytes()))
            if path.is_file()
            else httpx.Response(404)
        )

    return httpx.MockTransport(serve)


async def worker(config):
    directory = Path(config["public"])
    rpc, codec = Rpc(config["rpc"]), WireCodec()

    def clock():
        return int(rpc.call("eth_getBlockByNumber", "latest", False)["timestamp"], 16)

    journal = Journal(config["database"], {"mode": "local-evm", "agentRef": config["agentRef"]})
    transport = Web3Rpc(config["rpc"])
    chain = ChainConnection(
        transport,
        config["facts"],
        config["genesis"],
        journal,
        codec,
        readJson("specs/contracts.read.abi.json"),
        60,
        clock=clock,
    )
    registry = MonadRegistry(
        chain, readJson("specs/registry.identity.abi.json"), config["verification"]
    )
    account = Account.from_key(config["signerKey"])
    owner = Account.from_key(config["ownerKey"])
    market = MonadMarket(chain, account, registry, maxGas=2_000_000, clock=clock)
    http = httpx.AsyncClient(transport=publicTransport(directory))
    origin = "https://agent-" + config["agentRef"]["agentId"] + ".example"
    content = PublicContent(http, NetworkPolicy(), journal, origin, directory=directory)
    fixture = Path("specs/fixtures/layer-2")
    digests = tuple(
        contentDigest((fixture / f).read_bytes()) for f in ("output-schema.json", "policy.json")
    )
    participant = Participant(
        config["agentRef"],
        account.address.lower(),
        market,
        registry,
        content,
        journal,
        codec.schema,
        executeFixture,
        checkFixtureInput,
        digests,
    )
    coordinator, model = composeExecution(participant, clock, sdk=True)
    economicsConfig = economicConfig(participant)
    economicsConfig.update(
        runtime=coordinator.profile["runtime"], pricing=coordinator.profile["pricing"]
    )
    economicsConfig["overhead"]["gasAtoms"] = str(3 * market.maxGas * market.maxGasPriceWei)
    policy = filterPolicy(participant)

    def sign(permit):
        domain = {
            "name": "AgentLance",
            "version": "1",
            "chainId": 31337,
            "verifyingContract": config["facts"]["market"],
        }
        return (
            "0x"
            + bytes(
                owner.sign_message(
                    encode_typed_data(
                        full_message=buildBidPermitTypedData(
                            permit, domain, readJson("specs/signing/types.json")
                        )
                    )
                ).signature
            ).hex()
        )

    economics = EconomicRuntime(
        participant, policy, coordinator.profile["capacity"], sign, economicsConfig, clock
    )
    coordinator.history = economics.history
    runtime = DiscoveryRuntime(
        participant,
        MarketWatcher(chain, registry),
        policy,
        coordinator.profile["capacity"],
        None,
        sign,
        economics=economics,
    )
    consider = runtime.consider

    async def prefer(task, stamp):
        raw = await participant.supportTask(task)
        value = strictJson(raw)["value"]
        # Independent local strategy: parity, not a task ID or manager-assigned worker.
        if config["preference"] is None or value % 4 == config["preference"]:
            await consider(task, stamp)

    runtime.consider = prefer
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        print(jsonBytes({"ready": True}).decode(), flush=True)
        while raw := await reader.readline():
            command = strictJson(raw)["command"]
            if command == "close":
                break
            if command != "tick":
                raise ValueError("Only observe ticks are allowed")
            await runtime.tick()
            await drainEconomics(economics)
            await drainExecution(coordinator)
            print(
                jsonBytes(
                    {
                        "rows": journal.rows(),
                        "steps": coordinator.store.rows("step"),
                        "outbox": coordinator.store.rows("outbox"),
                        "modelCalls": model.calls,
                        "operations": journal.nativeOperations(),
                        "decisions": [
                            r["decision"] for _, r in economics.store.items("economic_candidates")
                        ],
                    }
                ).decode(),
                flush=True,
            )
    finally:
        await coordinator.close()
        await economics.close()
        await http.aclose()
        await transport.close()
        journal.close()
        rpc.close()
        pipe.close()


if __name__ == "__main__":
    asyncio.run(worker(strictJson(Path(sys.argv[1]).read_bytes())))
