"""Isolated local EVM wiring; never a verified public registry or deployment."""

from pathlib import Path

import httpx
from eth_account import Account

from apps.reference_agent.chain import DiscoveryRuntime
from apps.reference_agent.worker import checkFixtureInput, executeFixture
from modules.adapters.a2a.profile import jsonBytes
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.registry.chain import REFERENCE_REVISION, MonadRegistry
from modules.adapters.storage.content import ContentStore, NetworkPolicy
from modules.adapters.storage.journal import Journal
from modules.agent_client.discovery import MarketWatcher, deliverObserved
from modules.agent_client.participant import Participant
from modules.agent_client.signing import contentDigest
from tests.layer3_support import Layer3Rig, readJson


class Layer4Rig:
    def __init__(self, rpc, directory):
        self.rig = Layer3Rig(rpc)
        self.rpc, self.directory = rpc, directory
        self.path = directory / "agent.sqlite"
        # Mine past Anvil's finalized-tag lag; no public Monad qualification is inferred.
        self.rig.register(0)
        self.rig.external(
            self.rig.registry,
            "setURI",
            ["uint256", "string"],
            [0, "https://agent.example/registration.json"],
            caller="owner",
        )
        self.facts = {
            "chainId": "31337",
            "market": self.rig.market,
            "identityRegistry": self.rig.registry,
            "validator": self.rig.actors["validator"],
            "deploymentBlock": "2",
            "deploymentBlockHash": rpc.call("eth_getBlockByNumber", "0x2", False)["hash"],
            "marketCodeHash": contentDigest(
                bytes.fromhex(rpc.call("eth_getCode", self.rig.market, "latest")[2:])
            ),
            "identityCodeHash": contentDigest(
                bytes.fromhex(rpc.call("eth_getCode", self.rig.registry, "latest")[2:])
            ),
        }
        self.genesis = rpc.call("eth_getBlockByNumber", "0x0", False)["hash"]
        self.verification = {
            "sourceRevision": REFERENCE_REVISION,
            "validUntil": str(2**63),
            "deploymentBlock": "1",
            "deploymentBlockHash": rpc.call("eth_getBlockByNumber", "0x1", False)["hash"],
            "proxyKind": "none",
            "codeObservations": [],
            "storageObservations": [],
            "probes": {
                name: True
                for name in (
                    "transferClearsWallet",
                    "restoredWalletControl",
                    "contractOwner",
                    "registrationDiscovery",
                )
            },
        }
        self.finalize()
        self.open()

    def finalize(self):
        self.rpc.call("anvil_mine", "0x40")

    def open(self):
        self.journal = Journal(self.path, {"mode": "local-evm", "agentRef": self.rig.agentRef(0)})
        self.transport = Web3Rpc(str(self.rpc.client.base_url))
        self.chain = ChainConnection(
            self.transport,
            self.facts,
            self.genesis,
            self.journal,
            self.rig.codec,
            self.rig.readAbi,
            60,
            clock=self.rig.now,
        )
        self.registry = MonadRegistry(
            self.chain, readJson("specs/registry.identity.abi.json"), self.verification
        )
        self.market = MonadMarket(
            self.chain,
            Account.from_key(self.rig.keys["signer"]),
            self.registry,
            maxGas=5_000_000,
            clock=self.rig.now,
        )
        self.watcher = MarketWatcher(self.chain, self.registry)

    async def close(self):
        await self.transport.close()
        self.journal.close()

    async def restart(self):
        await self.close()
        self.open()

    def command(self, name, data, caller="requester", value=0):
        transaction = {
            "from": self.rig.actors[caller],
            "to": self.rig.market,
            "data": self.rig.codec.commandData(name, data),
            "value": hex(value),
            "gas": hex(5_000_000),
        }
        receipt = self.rpc.receipt(self.rpc.call("eth_sendTransaction", transaction))
        assert receipt["status"] == "0x1", receipt
        self.finalize()
        return receipt

    def create(self, **options):
        terms = self.rig.terms(**options)
        receipt = self.command("createTask", {"terms": terms}, value=int(terms["budgetAtoms"]))
        task = self.rig.codec.event(receipt["logs"][0])["payload"]["task"]
        return task

    async def applied(self, result):
        for _ in range(5):
            if result["state"] in {"APPLIED", "REJECTED"}:
                return result
            self.finalize()
            result = await self.market.readOperation(result["operationId"])
        raise AssertionError(result)


class DiscoveredWorker:
    """Real participant/runtime; only the external HTTPS content boundary is isolated."""

    def __init__(self, env):
        self.env, self.calls, self.requests = env, [], []
        fixture = Path(__file__).resolve().parents[1] / "specs/fixtures/layer-2"
        card = readJson("specs/fixtures/layer-2/card.json")
        card["supportedInterfaces"][0]["url"] = "https://agent.example/a2a"
        registration = readJson("specs/fixtures/layer-2/registration.json")
        registration["services"][0]["endpoint"] = "https://agent.example/card.json"
        registration["registrations"] = [
            {"agentId": 0, "agentRegistry": f"eip155:31337:{env.rig.registry}"}
        ]
        self.blobs = {"/card.json": jsonBytes(card), "/registration.json": jsonBytes(registration)}
        self.blobs.update(
            {
                "/" + name: (fixture / name).read_bytes()
                for name in ("input.json", "output-schema.json", "policy.json")
            }
        )

        def serve(request):
            self.requests.append(str(request.url))
            return httpx.Response(200, stream=httpx.ByteStream(self.blobs[request.url.path]))

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        self.policy = {
            "version": "local-demo-1",
            "supportedDigests": [
                contentDigest(self.blobs[path]) for path in ("/output-schema.json", "/policy.json")
            ],
            "requiredSkills": ["structured-output-v1"],
            "advertisedSkills": ["structured-output-v1"],
            "minBudgetAtoms": "0",
            "maxBudgetAtoms": str(2**96 - 1),
            "leadTimeSeconds": 0,
            "allowRequesters": [],
            "denyRequesters": [],
        }
        self.open()

    def open(self):
        env = self.env
        content = ContentStore(self.http, NetworkPolicy(), env.journal, "https://agent.example")

        def execute(raw):
            self.calls.append(raw)
            return executeFixture(raw)

        self.participant = Participant(
            env.rig.agentRef(0),
            env.rig.actors["signer"],
            env.market,
            env.registry,
            content,
            env.journal,
            env.chain.schema,
            execute,
            checkFixtureInput,
            tuple(self.policy["supportedDigests"]),
        )
        self.runtime = DiscoveryRuntime(
            self.participant,
            env.watcher,
            self.policy,
            1,
            "20",
            lambda permit: env.rig.sign("BidPermit", permit, "owner"),
        )

    def create(self, **options):
        terms = self.env.rig.terms(**options)
        for field, path in (
            ("input", "/input.json"),
            ("outputSchema", "/output-schema.json"),
            ("validationPolicy", "/policy.json"),
        ):
            terms[field] = {
                "uri": "https://agent.example" + path,
                "digest": contentDigest(self.blobs[path]),
            }
        receipt = self.env.command("createTask", {"terms": terms}, value=int(terms["budgetAtoms"]))
        return self.env.rig.codec.event(receipt["logs"][0])["payload"]["task"]


async def runDiscoveredWorker(env):
    worker = DiscoveredWorker(env)
    try:
        task = worker.create()
        ref = task["taskRef"]
        await env.watcher.scanRegistry()
        assert worker.requests == [] and worker.calls == []
        env.rig.register(1)
        env.finalize()
        await env.watcher.scanRegistry()
        assert env.journal.db.execute("SELECT count(*) FROM discovered_agents").fetchone()[0] == 2
        await env.watcher.scanMarket()
        await deliverObserved(
            env.watcher, worker.participant, worker.policy, 0, worker.runtime.consider
        )
        worker.policy["denyRequesters"] = [task["requester"]]
        await worker.runtime.tick()
        assert worker.requests == [] and worker.calls == [] and env.journal.nativeOperations() == []
        worker.policy.update(version="local-demo-2", denyRequesters=[])
        # No TaskRef is passed to the runtime, no index or A2A notification is present.
        await worker.runtime.tick()
        for intent in env.journal.nativeOperations():
            assert (await env.applied(await env.market.readOperation(intent["operationId"])))[
                "state"
            ] == "APPLIED"
        env.rig.advance(int(task["terms"]["biddingClose"]))
        env.command("allocateTask", {"taskRef": ref})
        await worker.runtime.tick()
        assert worker.calls == []  # Receipt is still tentative.
        await env.restart()
        worker.open()
        for _ in range(10):
            env.finalize()
            await worker.runtime.tick()
            if env.journal.rows()[0]["phase"] == "RESULT_RECORDED":
                break
        row = env.journal.rows()[0]
        assert row["phase"] == "RESULT_RECORDED", row
        assert worker.calls == [worker.blobs["/input.json"]]
        view = await env.market.readTask(ref)
        assert view["status"] == "SUBMITTED"
        result = view["result"]
        await env.restart()
        worker.open()
        await worker.runtime.tick()
        assert worker.calls == [worker.blobs["/input.json"]]
        assert (await env.market.readTask(ref))["result"] == result
        env.rig.advance(int(task["terms"]["validationBy"]))
        env.command("expireTask", {"taskRef": ref})
        receipt = (await env.market.readSettlement(ref))["receipt"]
        assert receipt["reason"] == "VALIDATOR_TIMEOUT"
        return {
            "taskRef": ref,
            "result": result,
            "receipt": receipt,
            "workerInvocations": len(worker.calls),
            "indexEnabled": False,
            "hintsEnabled": False,
            "transactionHashes": [
                item["transactionHash"] for item in env.journal.nativeOperations()
            ],
            "indexedThrough": env.watcher.progress()["stamp"],
        }
    finally:
        await worker.http.aclose()
