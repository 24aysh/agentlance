"""Optional external target: local operator control, assertions exclusively over HTTP."""

import asyncio
import json
from pathlib import Path

from modules.adapters.a2a.profile import URN, strictJson
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from modules.agent_client.ports import ensure


class ExternalTarget:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.config = {"agentOrigin": descriptor["agentOrigin"]}
        policy = NetworkPolicy(descriptor.get("fixtureOrigins", []))
        self.http = createHttpClient(policy, descriptor.get("caFile"))
        self.content = ContentStore(self.http, policy)
        self.schema = json.loads(Path("specs/schemas/protocol.schema.json").read_text())

    @property
    def calls(self):
        path = Path(self.descriptor["invocationLog"])
        return path.read_bytes().splitlines() if path.exists() else []

    async def prepare(self, stage):
        # Only an explicitly supplied local operator command runs; no metadata-sourced command.
        process = await asyncio.create_subprocess_exec(
            *self.descriptor["controlCommand"],
            stage,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            raw, errors = await asyncio.wait_for(process.communicate(), 30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        ensure(process.returncode == 0, errors.decode(errors="replace"))
        data = strictJson(raw)
        self.message = data["message"]
        self.extension = self.message["metadata"][URN]
        self.cardBytes = await self.content.fetchBytes(self.descriptor["cardUrl"], 65536)

    async def award(self):
        await self.prepare("award")

    async def complete(self):
        await self.prepare("complete")
