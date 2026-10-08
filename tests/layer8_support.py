"""Local TLS, encrypted fixture keys and CLI/process supervision; never live configuration."""

import asyncio
import os
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from eth_account import Account

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.application import validateOutput
from modules.agent_client.signing import contentDigest
from scripts.demo_layer2 import prepareSession

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "isolated-local-fixture"


def freePort():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class PublicTls:
    def __init__(self, directory, kubo=None):
        self.blobs, self.kubo = {}, kubo
        port = freePort()
        tls = directory / "tls"
        prepareSession(tls, freePort(), freePort())
        self.certificate, self.key = tls / "certificate.pem", tls / "tls.key"
        public = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw = public.blobs.get(self.path)
                if raw is None and public.kubo and self.path.startswith("/ipfs/"):
                    response = httpx.get(
                        public.kubo["gateway"] + self.path, timeout=10, trust_env=False
                    )
                    raw = response.content if response.status_code == 200 else None
                self.send_response(200 if raw is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if raw is not None:
                    self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, self.key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"https://127.0.0.1:{port}"

    def publish(self, raw):
        digest = contentDigest(raw)
        path = "/artifacts/" + digest[2:] + ".json"
        self.blobs[path] = raw
        return {"uri": self.origin + path, "digest": digest}

    def terms(self, terms, fixture="layer-2"):
        for name, filename in (
            ("input", "input.json"),
            ("outputSchema", "output-schema.json"),
            ("validationPolicy", "policy.json"),
        ):
            terms[name] = self.publish((ROOT / "specs/fixtures" / fixture / filename).read_bytes())
        return terms

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def encryptedKey(directory, name, key):
    target = directory / (name + "-keystore.json")
    target.write_bytes(jsonBytes(Account.encrypt(key, PASSWORD, kdf="pbkdf2", iterations=1000)))
    target.chmod(0o600)
    return {"path": str(target), "passwordEnv": "AGENTLANCE_FIXTURE_PASSWORD"}


def appConfig(
    env, directory, public, *, role="requester", facts=None, feedback=None, extraOrigins=()
):
    facts = facts or env.facts
    manifest = directory / "application-facts.json"
    manifest.write_bytes(jsonBytes(facts))
    verification = directory / "application-verification.json"
    verification.write_bytes(
        jsonBytes(
            {
                "identityVerification": env.verification,
                **({"feedbackVerification": feedback} if feedback else {}),
            }
        )
    )
    config = {
        "networkScope": "LOCAL_FIXTURE",
        "manifestPath": str(manifest),
        "registryVerificationPath": str(verification),
        "validatorVerificationPath": None,
        "rpcUrl": str(env.rpc.client.base_url),
        "genesisHash": env.genesis,
        "database": str(directory / (role + "-application.sqlite")),
        "keystore": encryptedKey(directory, role, env.rig.keys[role]),
        "maxFinalizedAgeSeconds": 86400,
        "maxGas": 5000000,
        "maxGasPriceWei": str(10**11),
        "ipfsGateway": public.origin,
        "fixtureOrigins": [public.origin, *extraOrigins],
        "caFile": str(public.certificate),
    }
    path = directory / (role + "-application.json")
    path.write_bytes(jsonBytes(config))
    return path


async def cli(config, *arguments, success=True):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "apps.cli.main",
        "--config",
        str(config),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ | {"AGENTLANCE_FIXTURE_PASSWORD": PASSWORD},
    )
    try:
        raw, errors = await asyncio.wait_for(process.communicate(), 90)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if not success:
        assert process.returncode != 0 and not raw
        return strictJson(errors)
    assert process.returncode == 0, errors.decode()
    value = strictJson(raw)
    validateOutput(value, "CostDisclosure" if arguments[0] == "cost" else "Envelope")
    return value


async def cliWrite(env, config, group, action, requestId, *args):
    value = await cli(config, group, action, "--request-id", requestId, "--broadcast", *args)
    for _ in range(5):
        if value["data"]["confirmation"] in {"FINALIZED_APPLIED", "REJECTED", "FINALIZED_REVERTED"}:
            return value
        env.finalize()
        value = await cli(config, "operation", "resume", "--request-id", requestId, "--broadcast")
    raise AssertionError(value)


async def currentRead(config, *args):
    """The operator explicitly repeats bounded reads while this fixture index catches up."""
    for _ in range(16):
        value = await cli(config, *args)
        if not any(x["component"] in {"history", "freshness"} for x in value["missing"]):
            return value
    raise AssertionError("Fixture history did not catch up within 16 bounded commands")


class ExternalAgent:
    def __init__(self, env, directory, public, *, observeOnly=False, origin=None):
        self.env, self.directory, self.public = env, directory, public
        self.origin = origin or f"https://127.0.0.1:{freePort()}"
        card = strictJson((ROOT / "specs/fixtures/layer-2/card.json").read_bytes())
        card["name"] = "Independent Node AgentLance example"
        card["supportedInterfaces"][0]["url"] = self.origin + "/a2a"
        cardPath = directory / "external-card.json"
        cardPath.write_bytes(jsonBytes(card))
        registration = {
            "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
            "name": "Independent Node example",
            "description": "Local deterministic integer copy",
            "active": True,
            "services": [
                {
                    "name": "A2A",
                    "endpoint": self.origin + "/.well-known/agent-card.json",
                    "version": "1.0",
                }
            ],
            "registrations": [{"agentId": 0, "agentRegistry": f"eip155:31337:{env.rig.registry}"}],
        }
        registrationPath = directory / "external-registration.json"
        registrationPath.write_bytes(jsonBytes(registration))
        env.rig.external(
            env.rig.registry,
            "setURI",
            ["uint256", "string"],
            [0, self.origin + "/registration.json"],
            caller="owner",
        )
        env.finalize()
        manifest = directory / "external-facts.json"
        manifest.write_bytes(jsonBytes(env.facts))
        self.config = {
            "networkScope": "LOCAL_FIXTURE",
            "manifestPath": str(manifest),
            "rpcUrl": str(env.rpc.client.base_url),
            "genesisHash": env.genesis,
            "agentRef": env.rig.agentRef(0),
            "payout": env.rig.actors["payout"],
            "origin": self.origin,
            "cardPath": str(cardPath),
            "registrationPath": str(registrationPath),
            "certificate": str(public.certificate),
            "tlsKey": str(public.key),
            "fixtureOrigins": [public.origin, self.origin],
            "keystore": encryptedKey(directory, "external", env.rig.keys["owner"]),
            "journal": str(directory / "external-journal.json"),
            "invocationLog": str(directory / "external-invocations.jsonl"),
            "maxGas": 5000000,
            "maxGasPriceWei": str(10**11),
            "maxFinalizedAgeSeconds": 86400,
            "bidAtoms": "20",
            "observeOnly": observeOnly,
        }
        self.path = directory / "external-config.json"
        self.path.write_bytes(jsonBytes(self.config))
        self.process = None

    async def open(self):
        self.log = (self.directory / "external-process.log").open("ab")
        self.process = await asyncio.create_subprocess_exec(
            "node",
            str(ROOT / "examples/external-agent/main.mjs"),
            str(self.path),
            stdout=self.log,
            stderr=self.log,
            env=os.environ | {"AGENTLANCE_FIXTURE_PASSWORD": PASSWORD},
        )
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=self.public.certificate), trust_env=False
        ) as http:
            for _ in range(100):
                assert self.process.returncode is None, (
                    self.directory / "external-process.log"
                ).read_text()
                try:
                    response = await http.get(self.origin + "/.well-known/agent-card.json")
                    if response.status_code == 200:
                        return self
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise AssertionError("External agent readiness timeout")

    async def close(self, kill=False):
        if self.process and self.process.returncode is None:
            self.process.kill() if kill else self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 15)
        if hasattr(self, "log"):
            self.log.close()

    async def restart(self):
        await self.close(kill=True)
        return await self.open()

    async def waitFor(self, ref, status):
        for _ in range(80):
            assert self.process.returncode is None, (
                self.directory / "external-process.log"
            ).read_text()
            self.env.finalize()
            view = await self.env.market.readTask(ref)
            if (
                status == "BID"
                and (await self.env.market.readBid(ref, self.env.rig.agentRef(0)))["bid"]
            ) or view["status"] == status:
                return view
            await asyncio.sleep(0.25)
        raise AssertionError((status, (self.directory / "external-process.log").read_text()))


class RpcDropProxy:
    """One local fault after the real RPC accepts signed bytes; never fabricates chain data."""

    def __init__(self, url):
        self.dropped = threading.Event()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                request = strictJson(raw)
                response = httpx.post(
                    url,
                    content=raw,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                    trust_env=False,
                )
                if request["method"] == "eth_sendRawTransaction" and not proxy.dropped.is_set():
                    proxy.dropped.set()
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(response.status_code)
                self.end_headers()
                try:
                    self.wfile.write(response.content)
                except BrokenPipeError:
                    pass  # The requesting process may be deliberately killed in this fixture.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
