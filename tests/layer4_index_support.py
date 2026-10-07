"""Isolated, disposable Postgres/Hasura and the actual pinned Envio process."""

import asyncio
import os
import signal
import socket
import subprocess
import time
from uuid import uuid4

import httpx

from modules.agent_client.ports import AdapterError
from tests.layer3_support import ROOT


def freePort():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalIndex:
    def __init__(self, env, directory):
        self.name = "agentlance-l4-" + uuid4().hex[:12]
        self.pg, self.hasura = self.name + "-pg", self.name + "-hasura"
        self.directory, self.process, self.log = directory, None, None
        pgPort, apiPort = freePort(), freePort()
        self.endpoint = f"http://127.0.0.1:{apiPort}/v1/graphql"
        self.environment = os.environ | {
            "ENVIO_CHAIN_ID": "31337",
            "ENVIO_START_BLOCK": env.facts["deploymentBlock"],
            "ENVIO_RPC_URL": str(env.rpc.client.base_url),
            "ENVIO_MARKET": env.rig.market,
            "ENVIO_PG_HOST": "127.0.0.1",
            "ENVIO_PG_PORT": str(pgPort),
            "ENVIO_PG_USER": "postgres",
            "ENVIO_PG_PASSWORD": "testing",
            "ENVIO_PG_DATABASE": "envio-dev",
            "ENVIO_PG_SCHEMA": "public",
            "ENVIO_PG_SSL_MODE": "false",
            "ENVIO_HASURA": "true",
            "HASURA_GRAPHQL_ENDPOINT": f"http://127.0.0.1:{apiPort}/v1/metadata",
            "HASURA_GRAPHQL_ADMIN_SECRET": "testing",
            "ENVIO_INDEXER_PORT": str(freePort()),
        }

    def docker(self, *args):
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=120
        ).stdout

    def __enter__(self):
        self.docker("info", "--format", "{{.ServerVersion}}")
        try:
            self.docker("network", "create", self.name)
            self.docker(
                "run",
                "-d",
                "--name",
                self.pg,
                "--network",
                self.name,
                "-p",
                f"127.0.0.1:{self.environment['ENVIO_PG_PORT']}:5432",
                "-e",
                "POSTGRES_PASSWORD=testing",
                "-e",
                "POSTGRES_DB=envio-dev",
                "postgres:18.3",
            )
            for _ in range(40):
                probe = subprocess.run(
                    ["docker", "exec", self.pg, "pg_isready", "-U", "postgres", "-d", "envio-dev"],
                    capture_output=True,
                    timeout=5,
                )
                if probe.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("Isolated Postgres did not become healthy")
            self.docker(
                "run",
                "-d",
                "--name",
                self.hasura,
                "--network",
                self.name,
                "-p",
                f"127.0.0.1:{self.endpoint.split(':')[2].split('/')[0]}:8080",
                "-e",
                f"HASURA_GRAPHQL_DATABASE_URL=postgres://postgres:testing@{self.pg}:5432/envio-dev",
                "-e",
                "HASURA_GRAPHQL_ADMIN_SECRET=testing",
                "-e",
                "HASURA_GRAPHQL_ENABLE_CONSOLE=false",
                "hasura/graphql-engine:v2.43.0",
            )
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    async def ready(self):
        async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
            for _ in range(120):
                try:
                    response = await http.get(self.endpoint.replace("/v1/graphql", "/healthz"))
                    if response.status_code == 200:
                        self.start()
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise AssertionError("Isolated Hasura did not become healthy")

    def start(self, rebuild=False):
        self.stop()
        self.log = (self.directory / "envio.log").open("a")
        self.logOffset = self.log.tell()
        self.process = subprocess.Popen(
            ["pnpm", "exec", "envio", "start", *(["--restart"] if rebuild else [])],
            cwd=ROOT / "apps/service",
            env=self.environment,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()
        self.process, self.log = None, None

    async def catchUp(self, index, height):
        last = None
        for _ in range(160):
            if self.process.poll() is not None:
                raise AssertionError((self.directory / "envio.log").read_text()[-12000:])
            recent = (self.directory / "envio.log").read_text()[self.logOffset :]
            if not any(
                message in recent
                for message in (
                    "The indexer storage is ready",
                    "Successfully resumed indexing state",
                )
            ):
                await asyncio.sleep(0.25)
                continue
            try:
                page = await index.listOpenTasks()
                if int(page["indexedThrough"]["blockNumber"]) >= height:
                    return page
            except (AdapterError, KeyError) as error:
                last = error
            await asyncio.sleep(0.25)
        raise AssertionError(
            f"Index did not catch up: {last}\n"
            + (self.directory / "envio.log").read_text()[-12000:]
        )

    def __exit__(self, *args):
        self.stop()
        for name in (self.hasura, self.pg):
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=30)
        subprocess.run(["docker", "network", "rm", self.name], capture_output=True, timeout=30)
