"""Shared L2 test composition. No expected outcome is calculated here."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from apps.fixture_market import loadMarket
from apps.reference_agent.worker import checkFixtureInput, executeFixture
from modules.adapters.a2a.profile import URN
from modules.adapters.a2a.server import agentApp
from modules.adapters.fixtures import FixtureClient, commandRecord, fixtureApp
from modules.adapters.storage.content import ContentStore, NetworkPolicy
from modules.adapters.storage.journal import Journal
from modules.agent_client.participant import Participant
from modules.agent_client.signing import buildBidPermitTypedData, contentDigest
from scripts.demo_layer2 import awardMessage, prepareSession, taskTerms


class LocalTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.routes = {}

    async def handle_async_request(self, request):
        return await self.routes[request.url.port].handle_async_request(request)


class Layer2Rig:
    def __init__(self, path):
        self.config, _ = prepareSession(path)
        self.host = loadMarket(self.config)
        self.schema, self.types = self.host.schema, self.host.types
        self.transport = LocalTransport()
        self.http = httpx.AsyncClient(transport=self.transport, trust_env=False)
        actors = {
            self.config["actorToken"]: {
                "address": self.config["signer"],
                "commands": ["submitBid", "acceptAward", "createChildTask", "submitResult"],
            }
        }
        self.hostApp = fixtureApp(self.host, self.config["readToken"], actors)
        self.transport.routes[8740] = httpx.ASGITransport(self.hostApp)
        self.market = FixtureClient(
            self.http,
            self.config["marketOrigin"],
            self.config["sessionId"],
            self.config["readToken"],
            self.config["actorToken"],
            self.schema,
        )
        self.settings = {
            "agentRef": self.config["agentRef"],
            "market": self.config["taskRef"]["market"],
            "sessionId": self.config["sessionId"],
        }
        self.journal = Journal(self.config["database"], self.settings)
        bundle = Path(self.config["bundleDir"])
        self.cardBytes = (bundle / "card.json").read_bytes()
        self.network = NetworkPolicy([self.config["marketOrigin"], self.config["agentOrigin"]])
        self.content = ContentStore(
            self.http, self.network, self.journal, self.config["agentOrigin"]
        )
        self.calls = []
        self.digests = tuple(
            contentDigest((bundle / name).read_bytes())
            for name in ("output-schema.json", "policy.json")
        )
        self.compose()
        self.terms = taskTerms(self.config)
        self.message = awardMessage(self.config, self.terms)
        self.extension = self.message["metadata"][URN]
        self.ref = self.extension["executionRef"]

    def compose(self):
        def execute(raw):
            self.calls.append(raw)
            return executeFixture(raw)

        self.journal.storeContent(self.cardBytes)
        self.participant = Participant(
            self.config["agentRef"],
            self.config["signer"],
            self.market,
            self.market,
            self.content,
            self.journal,
            self.schema,
            execute,
            checkFixtureInput,
            self.digests,
        )
        self.app = agentApp(self.participant, self.cardBytes)
        self.transport.routes[8741] = httpx.ASGITransport(self.app)

    def restart(self):
        self.journal.close()
        self.journal = Journal(self.config["database"], self.settings)
        self.content.journal = self.journal
        self.compose()

    def sign(self, permit):
        domain = {
            "name": "AgentLance",
            "version": "1",
            "chainId": 31337,
            "verifyingContract": self.config["taskRef"]["market"],
        }
        typed = buildBidPermitTypedData(permit, domain, self.types)
        return (
            "0x"
            + Account.sign_message(
                encode_typed_data(full_message=typed), self.config["ownerKey"]
            ).signature.hex()
        )

    async def create(self):
        return await self.host.submit(
            self.config["requester"],
            str(uuid4()),
            commandRecord("createTask", terms=self.terms),
            self.terms["budgetAtoms"],
        )

    async def award(self, pending=False):
        await self.create()
        self.host.setClock(1100)
        result = await self.participant.prepareBid(
            self.config["taskRef"], "20", self.sign, "1", "1200"
        )
        assert result["state"] == "APPLIED", result
        self.host.setClock(1200)
        if pending:
            self.host.pendingCommands.add("allocateTask")
        return await self.host.submit(
            self.config["requester"],
            str(uuid4()),
            commandRecord("allocateTask", taskRef=self.config["taskRef"]),
        )

    async def running(self):
        await self.award()
        await self.participant.receiveHint(deepcopy(self.message))
        self.host.setClock(1400)
        await self.participant.advance(self.ref)
        assert self.host.view(self.config["taskRef"])["status"] == "RUNNING"

    async def complete(self):
        await self.running()
        for _ in range(3):
            await self.participant.advance(self.ref)
        assert self.journal.get(self.ref)["phase"] == "RESULT_RECORDED"

    async def close(self):
        self.journal.close()
        await self.http.aclose()
