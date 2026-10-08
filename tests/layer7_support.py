"""Real local EVM/Kubo/Docker; only public artifact transport is isolated."""

import asyncio
import json
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from modules.adapters.a2a.profile import jsonBytes
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.execution.docker import DockerExecutor
from modules.adapters.registry.chain import REFERENCE_REVISION
from modules.adapters.registry.feedback import IMPLEMENTATION_SLOT, OWNER_SLOT, FeedbackAdapter
from modules.adapters.storage.content import ContentStore, NetworkPolicy
from modules.adapters.storage.ipfs import KuboPublisher
from modules.adapters.storage.journal import Journal
from modules.adapters.storage.validation import ValidationStore
from modules.agent_client.discovery import MarketWatcher
from modules.agent_client.signing import contentDigest
from modules.economics.records import recordDigest
from modules.validation.runtime import ValidatorRuntime
from tests.layer3_support import readJson

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def localKubo():
    image = readJson("contracts/layer7.lock.json")["kuboImage"]
    name = "agentlance-kubo-" + uuid4().hex[:12]
    with socket.socket() as api, socket.socket() as gateway:
        api.bind(("127.0.0.1", 0))
        gateway.bind(("127.0.0.1", 0))
        apiPort, gatewayPort = api.getsockname()[1], gateway.getsockname()[1]
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{apiPort}:5001",
            "-p",
            f"127.0.0.1:{gatewayPort}:8080",
            "-e",
            "IPFS_PROFILE=test",
            image,
            "daemon",
            "--offline",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        info = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
        ports = info["NetworkSettings"]["Ports"]
        urls = {
            key: "http://127.0.0.1:" + ports[key + "/tcp"][0]["HostPort"]
            for key in ("5001", "8080")
        }
        for _ in range(100):
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    if client.post(urls["5001"] + "/api/v0/version").status_code == 200:
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("Local Kubo did not become ready")
        yield {"rpc": urls["5001"], "gateway": urls["8080"], "container": name}
    finally:
        subprocess.run(["docker", "rm", "-fv", name], check=True, stdout=subprocess.DEVNULL)


def encoded(signature, types, values):
    return keccak(text=signature)[:4] + encode(types, values)


def deployPublisher(env):
    rig = env.rig
    bootstrap = rig.deploy("HardhatMinimalUUPS")
    reputation = rig.deploy(
        "ERC1967Proxy",
        ["address", "bytes"],
        [bootstrap, encoded("initialize(address)", ["address"], [rig.registry])],
    )
    implementation = rig.deploy("ReputationRegistryUpgradeable")
    rig.external(
        reputation,
        "upgradeToAndCall",
        ["address", "bytes"],
        [implementation, encoded("initialize(address)", ["address"], [rig.registry])],
    )
    publisher = rig.deploy(
        "FeedbackPublisher",
        ["address", "address", "address"],
        [rig.market, rig.registry, reputation],
    )
    env.finalize()
    block = env.rpc.call("eth_getBlockByNumber", "finalized", False)
    publisherAbi = readJson("specs/feedback-publisher.abi.json")
    registryAbi = readJson("specs/registry.reputation.abi.json")
    verification = {
        "sourceRevision": REFERENCE_REVISION,
        "market": rig.market,
        "identityRegistry": rig.registry,
        "reputationRegistry": reputation,
        "publisher": publisher,
        "publisherAbiDigest": recordDigest(publisherAbi),
        "reputationAbiDigest": recordDigest(registryAbi),
        "validUntil": str(2**63),
        "deploymentBlock": str(int(block["number"], 16)),
        "deploymentBlockHash": block["hash"],
        "proxies": [{"address": reputation, "implementation": implementation, "kind": "uups"}],
        "codeObservations": [
            {
                "address": address,
                "codeHash": contentDigest(
                    bytes.fromhex(env.rpc.call("eth_getCode", address, "finalized")[2:])
                ),
            }
            for address in (rig.registry, publisher, reputation, implementation)
        ],
        "storageObservations": [
            {
                "address": reputation,
                "slot": slot,
                "value": env.rpc.call("eth_getStorageAt", reputation, slot, "finalized"),
            }
            for slot in (IMPLEMENTATION_SLOT, OWNER_SLOT)
        ],
    }
    return publisher, verification


class ValidatorHarness:
    def __init__(self, env, directory, kubo, serve, *, deployment=None):
        self.env, self.directory, self.kubo, self.serve = env, directory, kubo, serve
        self.deployment = deployment or deployPublisher(env)
        self.open()

    def open(self):
        env = self.env
        self.journal = Journal(
            self.directory / "validator.sqlite",
            {"mode": "local-evm", "role": "validator", "market": env.rig.market},
        )
        self.store = ValidationStore(self.journal)
        self.rpc = Web3Rpc(str(env.rpc.client.base_url))
        self.chain = ChainConnection(
            self.rpc,
            env.facts,
            env.genesis,
            self.journal,
            env.rig.codec,
            env.rig.readAbi,
            60,
            clock=env.rig.now,
        )
        self.market = MonadMarket(
            self.chain,
            Account.from_key(env.rig.keys["validator"]),
            maxGas=5_000_000,
            clock=env.rig.now,
        )
        self.uploader = KuboPublisher(self.kubo["rpc"], self.store)
        self.gateway = httpx.AsyncClient(timeout=10, trust_env=False)

        async def transport(request):
            if request.url.host == "ipfs.example":
                response = await self.gateway.get(self.kubo["gateway"] + request.url.path)
                return httpx.Response(
                    response.status_code, stream=httpx.ByteStream(response.content)
                )
            return self.serve(request)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        self.content = ContentStore(
            self.http, NetworkPolicy(), self.journal, ipfsGateway="https://ipfs.example"
        )
        self.publisher = FeedbackAdapter(
            self.market,
            self.deployment[0],
            readJson("specs/feedback-publisher.abi.json"),
            readJson("specs/registry.reputation.abi.json"),
            self.deployment[1],
        )
        self.runtime = ValidatorRuntime(
            self.market,
            MarketWatcher(self.chain),
            self.store,
            self.content,
            self.uploader,
            # Separate local chains reuse deterministic addresses; the fixture's
            # durable directory distinguishes their containers across restarts.
            DockerExecutor(namespace="l7-test:" + str(self.directory.resolve())),
            Account.from_key(env.rig.keys["validator"]),
            readJson("specs/signing/types.json"),
            (ROOT / ".scratch/layer7/image-id").read_text().strip(),
            publisher=self.publisher,
            clock=env.rig.now,
        )

    async def tick(self):
        self.env.rig.advance(self.env.rig.now() + 2)
        self.env.finalize()
        await self.runtime.tick()
        if self.runtime.pending:
            await asyncio.wait_for(asyncio.gather(*self.runtime.pending.values()), 20)

    async def drain(self, taskRef, *, feedback=True):
        for _ in range(30):
            await self.tick()
            view = await self.market.readTask(taskRef)
            outcome = self.runtime.observer.readOutcome(taskRef)
            if view["receipt"] and (
                not feedback
                or outcome["feedback"]
                and outcome["feedback"]["state"] in {"PUBLISHED", "NOT_APPLICABLE"}
            ):
                return outcome
        raise AssertionError(self.store.rows("job") + self.store.rows("export"))

    async def close(self):
        await self.runtime.close()
        await self.uploader.close()
        await self.http.aclose()
        await self.gateway.aclose()
        await self.rpc.close()
        self.journal.close()

    async def restart(self):
        await self.close()
        self.open()


class ValidatorProcess:
    async def open(self, env, directory, public, kubo):
        import sys

        from modules.adapters.a2a.profile import jsonBytes, strictJson

        self.env = env
        config = {
            "rpc": str(env.rpc.client.base_url),
            "facts": env.facts,
            "genesis": env.genesis,
            "validatorKey": "0x" + env.rig.keys["validator"].hex(),
            "directory": str(directory),
            "public": str(public),
            "kubo": kubo,
            "deployment": deployPublisher(env),
        }
        path = directory / "validator-config.json"
        path.write_bytes(jsonBytes(config))
        self.configPath, self.logPath = path, directory / "validator-process.log"
        self.restartOnTransactions, self.restarted = False, set()
        self.log = self.logPath.open("ab")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.layer7_process",
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
            limit=4 * 1048576,
        )
        assert strictJson(await asyncio.wait_for(self.process.stdout.readline(), 20))["ready"]
        self.snapshot = None
        return self

    async def tick(self, command="tick"):
        from modules.adapters.a2a.profile import jsonBytes, strictJson

        self.process.stdin.write(jsonBytes({"command": command}) + b"\n")
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), 60)
        assert raw, "Validator process stopped; inspect validator-process.log"
        self.snapshot = strictJson(raw)
        return self.snapshot

    async def restartProcess(self):
        import sys

        from modules.adapters.a2a.profile import strictJson

        self.process.kill()
        await self.process.wait()
        self.log.close()
        self.log = self.logPath.open("ab")
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.layer7_process",
            str(self.configPath),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
            limit=4 * 1048576,
        )
        assert strictJson(await asyncio.wait_for(self.process.stdout.readline(), 20))["ready"]

    async def settle(self, task):
        for _ in range(100):
            result = await self.tick()
            if self.restartOnTransactions:
                for operation in result["transactions"]:
                    if operation["transactionHash"] and operation["kind"] not in self.restarted:
                        self.restarted.add(operation["kind"])
                        await self.restartProcess()
                        break
            for outcome in result["outcomes"]:
                if (
                    outcome["taskRef"] == task["taskRef"]
                    and outcome["feedback"]
                    and outcome["feedback"]["state"] in {"PUBLISHED", "NOT_APPLICABLE"}
                ):
                    return outcome
        raise AssertionError(self.snapshot)

    async def close(self):
        from modules.adapters.a2a.profile import jsonBytes

        if self.process.returncode is None:
            self.process.stdin.write(jsonBytes({"command": "close"}) + b"\n")
            await self.process.stdin.drain()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.log.close()


def submit(env, worker, answer=7):
    task = worker.create(budget=100, denominator=100)
    env.command("submitBid", env.rig.bid(int(task["taskRef"]["taskId"]), amount=20), caller="owner")
    env.rig.advance(int(task["terms"]["biddingClose"]))
    env.command("allocateTask", {"taskRef": task["taskRef"]})
    ref = {"taskRef": task["taskRef"], "awardId": 1}
    env.command("acceptAward", {"executionRef": ref, "ownWorkReserveAtoms": "0"}, caller="signer")
    raw = jsonBytes({"value": answer})
    uri = "/result-" + task["taskRef"]["taskId"] + ".json"
    worker.blobs[uri] = raw
    env.command(
        "submitResult",
        {
            "executionRef": ref,
            "artifact": {"uri": "https://agent.example" + uri, "digest": contentDigest(raw)},
        },
        caller="signer",
    )
    return task
