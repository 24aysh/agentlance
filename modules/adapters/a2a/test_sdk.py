"""Probe the pinned official SDK before composing a worker."""

import asyncio

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import create_rest_routes
from a2a.types import AgentCard, GetTaskRequest, SendMessageRequest, Task
from a2a.utils.errors import UnsupportedOperationError
from fastapi import FastAPI

from modules.adapters.a2a.profile import HEADERS, toProto, wireDict


class ProbeHandler(RequestHandler):
    async def on_message_send(self, params, context):
        return Task(id="probe", context_id="context", status={"state": "TASK_STATE_COMPLETED"})

    async def on_get_task(self, params, context):
        return await self.on_message_send(params, context)

    async def unsupported(self, params, context):
        raise UnsupportedOperationError()

    on_list_tasks = on_cancel_task = on_message_send_stream = unsupported
    on_create_task_push_notification_config = on_get_task_push_notification_config = unsupported
    on_subscribe_to_task = on_list_task_push_notification_configs = unsupported
    on_delete_task_push_notification_config = on_get_extended_agent_card = unsupported


def testPinnedSdkHttpJson():
    async def run():
        app = FastAPI(routes=create_rest_routes(ProbeHandler(), path_prefix="/a2a"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://agent.example", headers=HEADERS
        ) as http:
            card = AgentCard(
                name="probe",
                description="probe",
                version="1",
                supported_interfaces=[
                    {
                        "url": "https://agent.example/a2a",
                        "protocol_binding": "HTTP+JSON",
                        "protocol_version": "1.0",
                    }
                ],
            )
            client = ClientFactory(
                ClientConfig(
                    streaming=False,
                    polling=True,
                    supported_protocol_bindings=["HTTP+JSON"],
                    httpx_client=http,
                )
            ).create(card)
            request = toProto(
                {
                    "message": {
                        "messageId": "m",
                        "role": "ROLE_USER",
                        "parts": [{"text": "probe"}],
                    },
                    "configuration": {"returnImmediately": True},
                },
                SendMessageRequest,
            )
            replies = [wireDict(reply) async for reply in client.send_message(request)]
            assert replies[0]["task"]["id"] == "probe"
            assert (await client.get_task(GetTaskRequest(id="probe"))).context_id == "context"
            response = await http.get("/a2a/tasks/probe")
            assert response.json()["status"]["state"] == "TASK_STATE_COMPLETED"

    asyncio.run(run())


def testClientCredentialScope(l2):
    import json

    from modules.adapters.a2a.client import AgentClient

    async def run():
        observed = []

        def record(request):
            observed.append(request)
            return httpx.Response(
                200,
                content=b"{}",
                headers={
                    "Content-Type": "application/json",
                    "A2A-Extensions": HEADERS["A2A-Extensions"],
                },
            )

        card = json.loads(l2.cardBytes)
        card["securitySchemes"] = {"auth": {"httpAuthSecurityScheme": {"scheme": "bearer"}}}
        card["securityRequirements"] = [{"schemes": {"auth": {"list": []}}}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as http:
            AgentClient(json.dumps(card).encode(), http, l2.schema, "fixture-secret")
            await http.get("https://localhost:8741/a2a/tasks/id")
            await http.get("https://elsewhere.example/bytes")
            assert observed[0].headers["Authorization"] == "Bearer fixture-secret"
            assert "Authorization" not in observed[1].headers

    asyncio.run(run())


def testStandardUnsupportedOperations(l2):
    async def run():
        await l2.award()
        row = await l2.participant.receiveHint(l2.message)
        taskId = row["correlation"]["a2aTaskId"]
        origin = l2.config["agentOrigin"] + "/a2a"
        for method, path in [
            ("GET", "/tasks/unknown"),
            ("POST", f"/tasks/{taskId}:cancel"),
            ("GET", "/tasks"),
            ("GET", f"/tasks/{taskId}?historyLength=-1"),
        ]:
            response = await l2.http.request(
                method, origin + path, headers=HEADERS, json={} if method == "POST" else None
            )
            assert response.status_code in {400, 404, 422, 501}, response.text
        missing = await l2.http.get(l2.config["agentOrigin"] + "/artifacts/" + "ff" * 32 + ".json")
        assert missing.status_code == 404
        assert not l2.calls

    asyncio.run(run())


def testLargeIdsOnWire(l2):
    from modules.adapters.a2a.client import AgentClient
    from modules.adapters.a2a.profile import URN, jsonBytes, strictJson
    from modules.domain.records import agentKey

    async def run():
        oldKey = agentKey(l2.config["agentRef"])
        l2.config["agentRef"]["agentId"] = str(2**256 - 1)
        l2.host.identities[agentKey(l2.config["agentRef"])] = l2.host.identities.pop(oldKey)
        registration = strictJson(l2.host.contents["registration.json"])
        registration["registrations"][0]["agentId"] = 2**256 - 1
        l2.host.contents["registration.json"] = jsonBytes(registration)
        l2.host.state.taskCount = 2**53
        l2.config["taskRef"]["taskId"] = str(2**53 + 1)
        await l2.award()
        client = AgentClient(l2.cardBytes, l2.http, l2.schema)
        task = await client.sendHint(l2.message)
        ext = task["metadata"][URN]
        assert ext["agentRef"]["agentId"] == str(2**256 - 1)
        assert ext["executionRef"]["taskRef"]["taskId"] == str(2**53 + 1)
        assert type(ext["schemaVersion"]) is int

    asyncio.run(run())


def testStrictClientResponses():
    import pytest

    from modules.adapters.a2a.client import checkSdkResponse
    from modules.agent_client.ports import AdapterError

    async def run():
        for raw, headers in [
            (b"{}", {"content-type": "application/json"}),
            (b'{"x":1.0}', {"content-type": "application/json", **HEADERS}),
            (b" " * 1048577, {"content-type": "application/json", **HEADERS}),
        ]:
            response = httpx.Response(200, stream=httpx.ByteStream(raw), headers=headers)
            with pytest.raises(AdapterError):
                await checkSdkResponse(response)

    asyncio.run(run())
