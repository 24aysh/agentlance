"""Explicit Monad composition of the reference worker; no fixture authority bridge."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import randbits

import uvicorn
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.reference_agent.worker import checkFixtureInput, executeFixture
from modules.adapters.a2a.profile import strictJson
from modules.adapters.a2a.server import agentApp
from modules.adapters.chain.codec import WireCodec
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.registry.chain import MonadRegistry
from modules.adapters.registry.resolution import selectInterface
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from modules.adapters.storage.journal import Journal
from modules.agent_client.discovery import MarketWatcher, checkFilterPolicy, deliverObserved
from modules.agent_client.participant import Participant
from modules.agent_client.ports import AdapterError, closed, ensure, recordCheck
from modules.agent_client.signing import buildBidPermitTypedData, contentDigest

LOG = logging.getLogger(__name__)


def readKeystore(config):
    closed(config, "path passwordEnv")
    password = os.environ.get(config["passwordEnv"])
    ensure(password is not None, "Keystore password environment variable missing", "UNAVAILABLE")
    return Account.from_key(
        Account.decrypt(strictJson(Path(config["path"]).read_bytes()), password)
    )


class DiscoveryRuntime:
    def __init__(self, participant, watcher, policy, capacity, bidAtoms, signPermit):
        checkFilterPolicy(policy, watcher.schema)
        ensure(type(capacity) is int and capacity > 0, "Configured capacity")
        recordCheck(bidAtoms, "Uint96", watcher.schema)
        self.participant, self.watcher, self.policy = participant, watcher, policy
        self.capacity, self.bidAtoms, self.signPermit = capacity, bidAtoms, signPermit

    async def consider(self, task, stamp):
        if int(self.bidAtoms) > int(task["terms"]["budgetAtoms"]):
            return
        # A nonce is chosen only for a new intent; prepareBid reuses any retained command.
        await self.participant.prepareBid(
            task["taskRef"],
            self.bidAtoms,
            self.signPermit,
            str(randbits(256)),
            task["terms"]["biddingClose"],
        )

    async def tick(self):
        await self.watcher.scanMarket()
        try:
            await self.watcher.scanRegistry()
        except AdapterError as error:
            if error.kind == "FINALITY_CONFLICT":
                raise
            LOG.warning("Registry observation unavailable: %s", error.kind)
        active = sum(
            row["phase"] not in {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}
            for row in self.participant.journal.rows()
        )
        await deliverObserved(
            self.watcher,
            self.participant,
            self.policy,
            max(0, self.capacity - active),
            self.consider,
        )
        for intent in self.participant.journal.nativeOperations():
            if intent["transactionHash"] and intent["result"] is None:
                await self.participant.market.readOperation(intent["operationId"])
        await self.participant.tick()


async def main(config):
    """Live activation requires a complete manifest until the bootstrap decision is resolved."""
    closed(
        config,
        "manifestPath genesisHash registryVerificationPath rpcUrl database agentRef "
        "executionKeystore ownerKeystore bundleDir artifactOrigin ipfsGateway "
        "maxFinalizedAgeSeconds maxGas maxGasPriceWei bidAtoms capacity filter "
        "broadcast listenHost agentPort certificate tlsKey",
    )
    ensure(config["broadcast"] is True, "Explicit broadcast configuration required", "CONFLICT")
    root = Path(__file__).resolve().parents[2]
    schema = strictJson((root / "specs/schemas/protocol.schema.json").read_bytes())
    facts = strictJson(Path(config["manifestPath"]).read_bytes())
    recordCheck(facts, "DeploymentManifest", schema)
    ensure(facts["network"] == "monad-testnet", "Only qualified Monad testnet is supported")
    ensure(config["rpcUrl"] in facts["rpcUrls"], "RPC is not in qualified manifest")
    recordCheck(config["agentRef"], "AgentRef", schema)
    ensure(
        config["agentRef"]["chainId"] == facts["chainId"]
        and config["agentRef"]["identityRegistry"] == facts["identityRegistry"],
        "Agent deployment namespace",
    )
    rawEvidence = Path(config["registryVerificationPath"]).read_bytes()
    ensure(
        contentDigest(rawEvidence) == facts["registryVerification"]["digest"],
        "Registry evidence digest",
    )
    verification = strictJson(rawEvidence)["identityVerification"]
    ensure(
        verification["identityRegistry"] == facts["identityRegistry"]
        and verification["identityVersion"] == facts["identityVersion"],
        "Registry evidence binding",
    )
    account, owner = (
        readKeystore(config["executionKeystore"]),
        readKeystore(config["ownerKeystore"]),
    )
    bundle = Path(config["bundleDir"])
    cardBytes = (bundle / "card.json").read_bytes()
    card = strictJson(cardBytes)
    interface = selectInterface(card)
    ensure(
        card["supportedInterfaces"][interface]["url"].startswith(config["artifactOrigin"] + "/"),
        "Local card origin mismatch",
    )
    policy = config["filter"]
    ensure(
        policy["advertisedSkills"] == sorted(skill["id"] for skill in card["skills"]),
        "Filter skill claims differ from card",
    )
    digests = [
        contentDigest((bundle / name).read_bytes())
        for name in ("output-schema.json", "policy.json")
    ]
    ensure(policy["supportedDigests"] == digests, "Worker schema/policy mismatch")
    # This runnable worker deliberately remains the narrow L2 structured transform.
    expected = root / "specs/fixtures/layer-2"
    ensure(
        digests
        == [
            contentDigest((expected / name).read_bytes())
            for name in ("output-schema.json", "policy.json")
        ],
        "Reference worker does not implement this template",
        "UNSUPPORTED",
    )
    checkFilterPolicy(policy, schema)
    Path(config["database"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Journal(
        config["database"],
        {
            "mode": "monad",
            "agentRef": config["agentRef"],
            "market": facts["market"],
            "signer": account.address.lower(),
        },
    )
    os.chmod(config["database"], 0o600)
    transport = Web3Rpc(config["rpcUrl"])
    network = NetworkPolicy()
    http = createHttpClient(network)
    try:
        codec = WireCodec(
            schema,
            strictJson((root / "specs/catalog.json").read_bytes()),
            strictJson((root / "specs/protocol.abi.json").read_bytes()),
        )
        chain = ChainConnection(
            transport,
            facts,
            config["genesisHash"],
            journal,
            codec,
            strictJson((root / "specs/contracts.read.abi.json").read_bytes()),
            config["maxFinalizedAgeSeconds"],
        )
        registry = MonadRegistry(
            chain,
            strictJson((root / "specs/registry.identity.abi.json").read_bytes()),
            verification,
        )
        market = MonadMarket(
            chain,
            account,
            registry,
            maxGas=config["maxGas"],
            maxGasPriceWei=int(config["maxGasPriceWei"]),
        )
        await registry.verifyDependencies(await chain.qualify())
        journal.storeContent(cardBytes)
        content = ContentStore(
            http, network, journal, config["artifactOrigin"], config["ipfsGateway"]
        )
        participant = Participant(
            config["agentRef"],
            account.address.lower(),
            market,
            registry,
            content,
            journal,
            schema,
            executeFixture,
            checkFixtureInput,
            tuple(digests),
        )
        signingTypes = strictJson((root / "specs/signing/types.json").read_bytes())

        def signPermit(permit):
            ensure(
                permit["owner"] == owner.address.lower(),
                "Owner key does not control current identity",
                "CONFLICT",
            )
            domain = {
                "name": "AgentLance",
                "version": "1",
                "chainId": int(facts["chainId"]),
                "verifyingContract": facts["market"],
            }
            typed = buildBidPermitTypedData(permit, domain, signingTypes)
            return (
                "0x"
                + bytes(owner.sign_message(encode_typed_data(full_message=typed)).signature).hex()
            )

        runtime = DiscoveryRuntime(
            participant,
            MarketWatcher(chain, registry),
            policy,
            config["capacity"],
            config["bidAtoms"],
            signPermit,
        )
        failures = []

        async def observe():
            while True:
                try:
                    await runtime.tick()
                except AdapterError as error:
                    LOG.warning("Observation paused: %s", error.kind)
                    if error.kind in {"FINALITY_CONFLICT", "INVALID_DATA"}:
                        failures.append(error)
                        server.should_exit = True
                        return
                except Exception as error:
                    failures.append(error)
                    server.should_exit = True
                    return
                await asyncio.sleep(2)

        @asynccontextmanager
        async def lifespan(app):
            background = asyncio.create_task(observe())
            try:
                yield
            finally:
                background.cancel()
                await asyncio.gather(background, return_exceptions=True)

        server = uvicorn.Server(
            uvicorn.Config(
                agentApp(participant, cardBytes, lifespan),
                host=config["listenHost"],
                port=config["agentPort"],
                ssl_certfile=config["certificate"],
                ssl_keyfile=config["tlsKey"],
                log_level="warning",
                access_log=False,
                timeout_graceful_shutdown=1,
            )
        )
        await server.serve()
        if failures:
            raise RuntimeError("Chain observation halted") from failures[0]
    finally:
        await http.aclose()
        await transport.close()
        journal.close()
