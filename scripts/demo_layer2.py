"""Two real HTTPS processes exercising the synthetic L2 boundary; no live funds."""

import argparse
import asyncio
import ipaddress
import os
import secrets
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from eth_account import Account  # noqa: E402

from modules.adapters.a2a.client import AgentClient  # noqa: E402
from modules.adapters.a2a.profile import DIAGNOSTIC, URN, jsonBytes, strictJson  # noqa: E402
from modules.adapters.fixtures import FixtureClient  # noqa: E402
from modules.adapters.storage.content import (  # noqa: E402
    ContentStore,
    NetworkPolicy,
    createHttpClient,
)
from modules.agent_client.signing import contentDigest  # noqa: E402


def address(number):
    return "0x" + f"{number:040x}"


def prepareSession(directory, marketPort=8740, agentPort=8741):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "agent.sqlite").exists():
        raise ValueError("Use a new work directory; existing execution journals are never reset")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AgentLance local fixture")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(private, hashes.SHA256())
    )
    certPath, keyPath = directory / "certificate.pem", directory / "tls.key"
    certPath.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyPath.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keyPath.chmod(0o600)
    ownerKey, executionKey = "0x" + "0" * 63 + "1", "0x" + "0" * 63 + "2"
    config = {
        "sessionId": str(uuid4()),
        "marketPort": marketPort,
        "agentPort": agentPort,
        "marketOrigin": f"https://localhost:{marketPort}",
        "agentOrigin": f"https://localhost:{agentPort}",
        "agentRef": {"chainId": "31337", "identityRegistry": address(101), "agentId": "7"},
        "taskRef": {"chainId": "31337", "market": address(100), "taskId": "1"},
        "ownerKey": ownerKey,
        "executionKey": executionKey,
        "owner": Account.from_key(ownerKey).address.lower(),
        "signer": Account.from_key(executionKey).address.lower(),
        "requester": address(200),
        "payout": address(201),
        "validator": address(202),
        "bidAtoms": "20",
        "readToken": secrets.token_urlsafe(32),
        "actorToken": secrets.token_urlsafe(32),
        "certificate": str(certPath),
        "tlsKey": str(keyPath),
        "database": str(directory / "agent.sqlite"),
        "invocationLog": str(directory / "invocations.log"),
        "bundleDir": str(directory / "bundle"),
    }
    bundle = Path(config["bundleDir"])
    bundle.mkdir(exist_ok=True)
    source = ROOT / "specs/fixtures/layer-2"
    for filename in ("input.json", "output-schema.json", "policy.json", "result.json"):
        (bundle / filename).write_bytes((source / filename).read_bytes())
    card = strictJson((source / "card.json").read_bytes())
    card["supportedInterfaces"][0]["url"] = config["agentOrigin"] + "/a2a"
    registration = strictJson((source / "registration.json").read_bytes())
    registration["services"][0]["endpoint"] = config["agentOrigin"] + "/.well-known/agent-card.json"
    for filename, value in (("card.json", card), ("registration.json", registration)):
        (bundle / filename).write_bytes(jsonBytes(value) + b"\n")
    configPath = directory / "session.json"
    configPath.write_bytes(jsonBytes(config))
    configPath.chmod(0o600)
    return config, configPath


def taskTerms(config):
    bundle = Path(config["bundleDir"])

    def ref(name):
        return {
            "uri": config["marketOrigin"] + "/fixture/content/" + name,
            "digest": contentDigest((bundle / name).read_bytes()),
        }

    return {
        "policyVersion": 1,
        "refundAddress": config["requester"],
        "asset": {"kind": "NATIVE", "symbol": "MON", "decimals": 18},
        "budgetAtoms": "100",
        "input": ref("input.json"),
        "outputSchema": ref("output-schema.json"),
        "taskFamily": "structured-output-v1",
        "alphaNum": "1",
        "alphaDen": "100",
        "biddingClose": "1200",
        "allocationBy": "1300",
        "acceptBy": "1500",
        "resultBy": "2500",
        "validationBy": "3000",
        "validator": config["validator"],
        "validationPolicy": ref("policy.json"),
        "delegation": {"maxDepth": 0, "maxChildren": 0},
        "retryOf": None,
    }


def awardMessage(config, terms):
    return {
        "messageId": str(uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": "Observe awarded task"}],
        "extensions": [URN],
        "metadata": {
            URN: {
                "schemaVersion": 1,
                "profileVersion": 1,
                "executionRef": {"taskRef": config["taskRef"], "awardId": 1},
                "agentRef": config["agentRef"],
                "inputDigest": terms["input"]["digest"],
                "validationPolicyDigest": terms["validationPolicy"]["digest"],
                "result": None,
            }
        },
    }


class ServiceProcess:
    def __init__(self, module, configPath, logPath):
        self.module, self.configPath, self.logPath = module, configPath, logPath
        self.process, self.log = None, None

    async def start(self):
        self.log = open(self.logPath, "ab")
        command = [sys.executable]
        if config := os.environ.get("AGENTLANCE_COVERAGE_CONFIG"):
            command += ["-m", "coverage", "run", "--rcfile=" + config, "--parallel-mode"]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            "-m",
            self.module,
            "--config",
            str(self.configPath),
            cwd=ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.log,
        )

    async def command(self, operation, **args):
        self.process.stdin.write(jsonBytes({"operation": operation, **args}) + b"\n")
        await self.process.stdin.drain()
        raw = await asyncio.wait_for(self.process.stdout.readline(), 10)
        if not raw:
            raise RuntimeError(f"{self.module} exited; see {self.logPath}")
        result = strictJson(raw)
        if "controlError" in result:
            raise RuntimeError(result["controlError"])
        return result

    async def stop(self):
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.command("shutdown"), 2)
                await asyncio.wait_for(self.process.wait(), 3)
            except (TimeoutError, BrokenPipeError, ConnectionResetError, RuntimeError):
                self.process.kill()
                await self.process.wait()
        if self.log:
            self.log.close()


async def waitFor(read, predicate, timeout=10, interval=0.1):
    async with asyncio.timeout(timeout):
        while True:
            value = await read()
            if predicate(value):
                return value
            await asyncio.sleep(interval)


async def ready(http, url):
    async def request():
        try:
            return (await http.get(url)).status_code
        except httpx.HTTPError:
            return None

    await waitFor(request, lambda status: status == 200)


async def runDemo(directory, marketPort=8740, agentPort=8741):
    config, configPath = prepareSession(directory, marketPort, agentPort)
    directory = Path(directory)
    host = ServiceProcess("apps.fixture_market", configPath, directory / "market.log")
    agent = ServiceProcess("apps.reference_agent.main", configPath, directory / "agent.log")
    network = NetworkPolicy([config["marketOrigin"], config["agentOrigin"]])
    http, a2aHttp = (createHttpClient(network, config["certificate"]) for _ in range(2))
    schema = strictJson((ROOT / "specs/schemas/protocol.schema.json").read_bytes())
    market = FixtureClient(
        http,
        config["marketOrigin"],
        config["sessionId"],
        config["readToken"],
        config["actorToken"],
        schema,
    )
    terms = taskTerms(config)
    message = awardMessage(config, terms)
    extension = message["metadata"][URN]

    def calls():
        path = Path(config["invocationLog"])
        return len(path.read_bytes().splitlines()) if path.exists() else 0

    try:
        async with asyncio.timeout(60):
            await host.start()
            await ready(http, config["marketOrigin"] + "/fixture/content/input.json")
            created = await host.command("create", operationId="create-1", terms=terms)
            assert created["state"] == "APPLIED", created
            await agent.start()
            await ready(http, config["agentOrigin"] + "/.well-known/agent-card.json")
            client = AgentClient(
                (Path(config["bundleDir"]) / "card.json").read_bytes(), a2aHttp, schema
            )
            try:
                await client.sendHint(message)
                raise AssertionError("Forged OPEN award accepted")
            except InvalidParamsError as error:
                assert (
                    strictJson(error.data[DIAGNOSTIC].encode())["reason"]
                    == "PROFILE_AWARD_MISMATCH"
                )
            assert calls() == 0
            await host.command("clock", now=1100)
            bid = await agent.command("bid")
            assert bid["state"] == "APPLIED", bid
            await host.command("clock", now=1200)
            await host.command("pending", commands=["allocateTask", "acceptAward"])
            allocated = await host.command(
                "allocateTask", operationId="award-1", taskRef=config["taskRef"]
            )
            assert allocated["state"] == "APPLIED"
            task = await client.sendHint(message)
            assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and calls() == 0
            await host.command("clock", now=1400)
            duplicates = [message | {"messageId": str(uuid4())} for _ in range(8)]
            returned = await asyncio.gather(*(client.sendHint(value) for value in duplicates))
            assert all(value["id"] == task["id"] for value in returned)
            view = await waitFor(
                lambda: market.observeAward(extension["executionRef"]),
                lambda value: value["status"] == "RUNNING",
                interval=2,
            )
            assert view["stamp"]["finality"] == "PENDING" and calls() == 0
            assert view["allocation"]["reservedAtoms"] == "50"
            await host.command("clock", now=1600)
            completed = await waitFor(
                lambda: client.getTask(task["id"], extension),
                lambda value: value["status"]["state"] == "TASK_STATE_COMPLETED",
                timeout=15,
                interval=2,
            )
            artifact = await client.fetchArtifact(completed, ContentStore(http, network))
            assert artifact == b'{"value":7}\n' and calls() == 1
            await waitFor(
                lambda: market.readTask(config["taskRef"]),
                lambda value: value["status"] == "SUBMITTED",
                interval=2,
            )
            before = await host.command("inspect", taskRef=config["taskRef"])
            assert before["credits"] == {} and before["view"]["receipt"] is None
            firstPid = agent.process.pid
            await agent.stop()
            await agent.start()
            await ready(http, config["agentOrigin"] + "/.well-known/agent-card.json")
            replay = await client.sendHint(message | {"messageId": str(uuid4())})
            assert (replay["id"], replay["contextId"], replay["artifacts"]) == (
                completed["id"],
                completed["contextId"],
                completed["artifacts"],
            )
            try:
                await client.sendHint(
                    message | {"taskId": task["id"], "contextId": task["contextId"]}
                )
                raise AssertionError("Terminal task accepted new message")
            except UnsupportedOperationError:
                pass
            for field in ("inputDigest", "validationPolicyDigest"):
                forged = deepcopy(message)
                forged["messageId"] = str(uuid4())
                forged["metadata"][URN][field] = "0x" + "ff" * 32
                try:
                    await client.sendHint(forged)
                    raise AssertionError("Conflicting hint accepted")
                except InvalidParamsError:
                    pass
            await host.command("clock", now=3000)
            settled = await host.command(
                "expireTask", operationId="expire-1", taskRef=config["taskRef"]
            )
            assert settled["state"] == "APPLIED"
            receipt = (await market.readSettlement(config["taskRef"]))["receipt"]
            assert (
                receipt["reason"],
                receipt["paidAtoms"],
                receipt["refundAtoms"],
                receipt["counterEffect"],
            ) == ("VALIDATOR_TIMEOUT", "0", "100", "NONE")
            await client.sendHint(message | {"messageId": str(uuid4())})
            final = await host.command("inspect", taskRef=config["taskRef"])
            eventCounts = dict(Counter(event["name"] for event in final["events"]))
            assert eventCounts == {
                "TaskCreated": 1,
                "BidAccepted": 1,
                "TaskAwarded": 1,
                "AwardAccepted": 1,
                "ResultSubmitted": 1,
                "TaskSettled": 1,
            }
            assert final["credits"] == {config["requester"]: 100} and calls() == 1
            report = {
                "synthetic": True,
                "sessionId": config["sessionId"],
                "passed": True,
                "servicePids": [host.process.pid, firstPid, agent.process.pid],
                "invocations": calls(),
                "correlation": {
                    "schemaVersion": 1,
                    "executionRef": extension["executionRef"],
                    "a2aTaskId": completed["id"],
                    "a2aContextId": completed["contextId"],
                },
                "artifactDigest": contentDigest(artifact),
                "events": eventCounts,
                "receipt": receipt,
                "cases": [
                    "forged-open",
                    "pending-award",
                    "pending-acceptance",
                    "concurrent-duplicate",
                    "artifact",
                    "restart",
                    "terminal-message",
                    "conflicting-hint",
                    "stale-replay",
                    "timeout-refund",
                ],
            }
            (directory / "report.json").write_bytes(jsonBytes(report) + b"\n")
            return report
    finally:
        await agent.stop()
        await host.stop()
        await http.aclose()
        await a2aHttp.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--market-port", type=int, default=8740)
    parser.add_argument("--agent-port", type=int, default=8741)
    args = parser.parse_args()
    result = asyncio.run(runDemo(args.work_dir, args.market_port, args.agent_port))
    print(
        f"L2 fixture demo passed: {result['invocations']} invocation; "
        "correlated artifact; validator-timeout refund."
    )
