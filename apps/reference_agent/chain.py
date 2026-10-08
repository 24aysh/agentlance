"""Explicit Monad composition of the reference worker; no fixture authority bridge."""

import asyncio
import logging
import os
import time
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
from modules.agent_client.economics import EconomicRuntime
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
    def __init__(
        self, participant, watcher, policy, capacity, bidAtoms, signPermit, *, economics=None
    ):
        checkFilterPolicy(policy, watcher.schema)
        ensure(type(capacity) is int and capacity > 0, "Configured capacity")
        ensure((bidAtoms is None) == (economics is not None), "Select fixed bid or economics")
        if bidAtoms is not None:
            recordCheck(bidAtoms, "Uint96", watcher.schema)
        self.economics = economics
        self.reconciler = None
        if economics is not None:
            from modules.adapters.storage.validation import ValidationStore
            from modules.validation.reconciliation import Reconciler

            self.reconciler = Reconciler(
                economics.history, watcher, ValidationStore(participant.journal)
            )
        self.participant, self.watcher, self.policy = participant, watcher, policy
        self.capacity, self.bidAtoms, self.signPermit = capacity, bidAtoms, signPermit

    async def consider(self, task, stamp):
        if self.economics is not None:
            await self.economics.enqueueCandidate(task, stamp)
            return
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
            LOG.warning("event=registry_observation_paused error_kind=%s", error.kind)
        active = sum(
            row["phase"] not in {"RESULT_RECORDED", "STOPPED", "INTERRUPTED"}
            for row in self.participant.journal.rows()
        )
        if self.participant.coordinator is not None:
            await self.participant.coordinator.observeReservations()
            active = self.capacity - self.participant.coordinator.store.available()
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
        if self.economics is not None:
            await self.economics.tick()
            if self.reconciler is None:
                from modules.adapters.storage.validation import ValidationStore
                from modules.validation.reconciliation import Reconciler

                self.reconciler = Reconciler(
                    self.economics.history, self.watcher, ValidationStore(self.participant.journal)
                )
            await self.reconciler.tick()


async def main(config):
    """Live activation requires a complete manifest until the bootstrap decision is resolved."""
    economicConfig = config.get("economics")
    executionConfig = config.get("execution")
    closed(
        config,
        "manifestPath genesisHash registryVerificationPath rpcUrl database agentRef "
        "executionKeystore ownerKeystore bundleDir artifactOrigin ipfsGateway "
        "maxFinalizedAgeSeconds maxGas maxGasPriceWei bidAtoms capacity filter "
        "broadcast listenHost agentPort certificate tlsKey"
        + (" economics" if economicConfig is not None else "")
        + (" execution" if executionConfig is not None else ""),
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
    # Reference execution supports only the two reviewed structured-copy templates.
    expected = root / "specs/fixtures/layer-2"
    supported = [
        [
            contentDigest((expected / name).read_bytes())
            for name in ("output-schema.json", "policy.json")
        ]
    ]
    if executionConfig is not None:
        parentFixture = root / "specs/fixtures/layer-6"
        supported.append(
            [
                contentDigest((parentFixture / name).read_bytes())
                for name in ("output-schema.json", "policy.json")
            ]
        )
    ensure(
        digests in supported,
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
        from modules.adapters.registry.feedback import FeedbackAdapter

        feedbackEvidence = strictJson(rawEvidence)["feedbackVerification"]
        ensure(
            feedbackEvidence["reputationRegistry"] == facts["reputationRegistry"],
            "Feedback manifest binding",
        )
        registry.feedback = FeedbackAdapter(
            market,
            facts["feedbackPublisher"],
            strictJson((root / "specs/feedback-publisher.abi.json").read_bytes()),
            strictJson((root / "specs/registry.reputation.abi.json").read_bytes()),
            feedbackEvidence,
        )
        await registry.verifyDependencies(await chain.qualify())
        journal.storeContent(cardBytes)
        content = ContentStore(
            http, network, journal, config["artifactOrigin"], config["ipfsGateway"]
        )
        inputCheck = checkFixtureInput
        if executionConfig is not None:
            from apps.reference_agent.container_worker import transform

            def inputCheck(raw):
                transform({"kind": "SOLO", "input": strictJson(raw)})

        participant = Participant(
            config["agentRef"],
            account.address.lower(),
            market,
            registry,
            content,
            journal,
            schema,
            executeFixture,
            inputCheck,
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

        economics = None
        if economicConfig is not None:
            from modules.adapters.jev import JevPredictor

            ensure(config["bidAtoms"] is None, "Economics cannot coexist with a fixed bid")
            forecast = economicConfig["forecast"]
            predictor = None
            if forecast["enabled"]:
                predictor = JevPredictor(
                    http,
                    os.environ.get("TYPESAFE_API_KEY"),
                    forecast["model"],
                    forecast["requestVersion"],
                )
            economics = EconomicRuntime(
                participant,
                policy,
                config["capacity"],
                signPermit,
                economicConfig,
                time.time,
                predictor,
            )
        coordinator = None
        if executionConfig is not None:
            from modules.adapters.execution.docker import DockerExecutor
            from modules.adapters.execution.sdk import AgentsExecutor
            from modules.execution.coordinator import ExecutionCoordinator

            ensure(executionConfig["capacity"] == config["capacity"], "Capacity binding")
            ensure(
                [
                    executionConfig["runtime"]["outputSchemaDigest"],
                    executionConfig["runtime"]["validationPolicyDigest"],
                ]
                == digests,
                "Execution template binding",
            )
            if economics is not None:
                ensure(
                    all(
                        executionConfig[k] == economicConfig[k]
                        for k in ("runtime", "pricing", "operator")
                    ),
                    "Economic execution binding",
                )
            docker = DockerExecutor()
            coordinator = ExecutionCoordinator(
                participant,
                executionConfig,
                docker,
                AgentsExecutor(docker, apiKey=os.environ.get("OPENAI_API_KEY")),
                history=economics.history if economics else None,
            )
        runtime = DiscoveryRuntime(
            participant,
            MarketWatcher(chain, registry),
            policy,
            config["capacity"],
            config["bidAtoms"],
            signPermit,
            economics=economics,
        )
        failures = []

        async def observe():
            while True:
                try:
                    await runtime.tick()
                except AdapterError as error:
                    LOG.warning("event=agent_observation_paused error_kind=%s", error.kind)
                    if error.kind in {"FINALITY_CONFLICT", "INVALID_DATA"}:
                        failures.append(error)
                        server.should_exit = True
                        return
                except Exception as error:
                    LOG.error("event=agent_observation_halted error_type=%s", type(error).__name__)
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
                if coordinator is not None:
                    await coordinator.close()
                if economics is not None:
                    await economics.close()

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
        LOG.info(
            "event=agent_starting chain_id=%s market=%s agent_id=%s sender=%s "
            "economics=%s execution=%s",
            facts["chainId"],
            facts["market"],
            config["agentRef"]["agentId"],
            account.address.lower(),
            economics is not None,
            coordinator is not None,
        )
        await server.serve()
        if failures:
            raise RuntimeError("Chain observation halted") from failures[0]
    finally:
        await http.aclose()
        await transport.close()
        journal.close()
        LOG.info("event=agent_stopped")
