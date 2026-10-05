"""Official SDK HTTP+JSON client with strict profile correlation."""

import json
from copy import deepcopy

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import AgentCard, GetTaskRequest, SendMessageRequest

from modules.adapters.a2a.profile import (
    HEADERS,
    TERMINAL,
    URN,
    ProfileError,
    jsonBytes,
    readExtension,
    restoreIntegers,
    strictJson,
    toProto,
    wireDict,
)
from modules.adapters.registry.resolution import selectInterface
from modules.adapters.storage.journal import executionKey
from modules.agent_client.ports import ensure


async def encodeSdkRequest(request):
    if request.url.path.endswith("/message:send"):
        value = json.loads(request.content)
        request._content = jsonBytes(restoreIntegers(value))
        request.stream = httpx.ByteStream(request._content)
        request.headers["Content-Length"] = str(len(request._content))


async def checkSdkResponse(response):
    if not response.is_success:
        return
    ensure(
        response.headers.get("content-type", "").split(";")[0]
        in {"application/a2a+json", "application/json"},
        "A2A JSON response required",
    )
    ensure(
        URN in {token.strip() for token in response.headers.get("A2A-Extensions", "").split(",")},
        "Profile extension not acknowledged",
    )
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        ensure(len(raw) + len(chunk) <= 1048576, "A2A response limit")
        raw.extend(chunk)
    response._content = bytes(raw)
    strictJson(bytes(raw))


class AgentClient:
    def __init__(self, cardBytes, http, schema, credential=None):
        card = strictJson(cardBytes)
        selected = selectInterface(card)
        security = card.get("securityRequirements", [])
        ensure(
            not security or credential is not None, "HTTP authentication required", "UNAVAILABLE"
        )
        if security:
            ensure(
                len(security) == 1 and len(security[0].get("schemes", {})) == 1,
                "Authentication scheme",
                "UNSUPPORTED",
            )
            name = next(iter(security[0]["schemes"]))
            scheme = card.get("securitySchemes", {}).get(name, {})
            ensure(
                scheme.get("httpAuthSecurityScheme", {}).get("scheme", "").lower() == "bearer",
                "Only HTTP bearer supported",
                "UNSUPPORTED",
            )
        self.http, self.schema, self.last = http, schema, {}
        self.correlations = {}
        endpoint = httpx.URL(card["supportedInterfaces"][selected]["url"])
        origin = (endpoint.scheme, endpoint.host, endpoint.port)

        async def scopeHeaders(request):
            target = (request.url.scheme, request.url.host, request.url.port)
            if target == origin:
                request.headers.update(HEADERS)
                if credential:
                    request.headers["Authorization"] = "Bearer " + credential
            elif request.headers.get("Authorization") == "Bearer " + str(credential):
                del request.headers["Authorization"]

        http.follow_redirects = False
        http.event_hooks["request"].append(scopeHeaders)
        http.event_hooks["request"].append(encodeSdkRequest)

        async def checkResponse(response):
            url = response.request.url
            if (url.scheme, url.host, url.port) == origin and url.path.startswith(
                endpoint.path.rstrip("/") + "/"
            ):
                await checkSdkResponse(response)

        http.event_hooks["response"].append(checkResponse)
        selectedCard = deepcopy(card)
        selectedCard["supportedInterfaces"] = [card["supportedInterfaces"][selected]]
        self.client = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=True,
                httpx_client=http,
                supported_protocol_bindings=["HTTP+JSON"],
                accepted_output_modes=["application/json"],
            )
        ).create(toProto(selectedCard, AgentCard))

    def correlate(self, task, extension):
        ensure(
            isinstance(task.get("id"), str) and isinstance(task.get("contextId"), str), "Task IDs"
        )
        binding = deepcopy(task.get("metadata", {}).get(URN))
        record = readExtension({"extensions": [URN], "metadata": {URN: binding}}, self.schema)
        if (record | {"result": None}) != extension:
            raise ProfileError("PROFILE_CONFLICT")
        key = executionKey(extension["executionRef"])
        correlation = (task["id"], task["contextId"])
        ensure(
            key not in self.correlations or self.correlations[key] == correlation,
            "Execution correlation changed",
            "CONFLICT",
        )
        previous = self.last.get(task["id"])
        if previous:
            ensure(previous["contextId"] == task["contextId"], "Context changed", "CONFLICT")
            oldResult = previous["metadata"][URN]["result"]
            if oldResult is not None and record["result"] not in (None, oldResult):
                raise ProfileError("PROFILE_CONFLICT")
        if record["result"] is not None:
            artifacts = task.get("artifacts", [])
            ensure(len(artifacts) == 1, "One final artifact required")
            artifact = artifacts[0]
            if readExtension(artifact, self.schema) != record:
                raise ProfileError("PROFILE_CONFLICT")
            ensure(
                artifact.get("parts")
                == [{"url": record["result"]["uri"], "mediaType": "application/json"}],
                "Artifact locator mismatch",
            )
        if previous:
            if record["result"] is not None and previous["metadata"][URN]["result"] is not None:
                ensure(
                    previous.get("artifacts") == task.get("artifacts"),
                    "Artifact changed",
                    "CONFLICT",
                )
            if previous["status"]["state"] in TERMINAL:
                return deepcopy(previous)
        self.correlations[key] = correlation
        self.last[task["id"]] = deepcopy(task)
        return task

    async def sendHint(self, message):
        extension = readExtension(message, self.schema)
        request = toProto(
            {"message": message, "configuration": {"returnImmediately": True}}, SendMessageRequest
        )
        responses = [wireDict(value) async for value in self.client.send_message(request)]
        ensure(len(responses) == 1 and "task" in responses[0], "Task response required")
        return self.correlate(responses[0]["task"], extension)

    async def getTask(self, taskId, extension):
        task = wireDict(await self.client.get_task(GetTaskRequest(id=taskId, history_length=0)))
        return self.correlate(task, extension)

    async def fetchArtifact(self, task, content):
        ref = task["metadata"][URN]["result"]
        ensure(ref is not None, "No result available")
        return await content.fetchBytes(ref["uri"], 1048576, ref["digest"])
