"""Official A2A REST routes over one durable participant journal."""

import asyncio
import json
from copy import deepcopy

from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import create_rest_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import Task
from a2a.utils.error_handlers import build_rest_error_payload
from a2a.utils.errors import (
    A2AError,
    ExtensionSupportRequiredError,
    InternalError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from modules.adapters.a2a.profile import (
    DIAGNOSTIC,
    TERMINAL,
    URN,
    ProfileError,
    checkHint,
    jsonBytes,
    restoreIntegers,
    strictJson,
    toProto,
)
from modules.adapters.storage.journal import STATES
from modules.agent_client.ports import AdapterError, ensure


def taskProjection(row):
    correlation = row["correlation"]
    extension = deepcopy(row["extension"]) | {"result": row["result"]}
    metadata = {URN: extension}
    if row["diagnostic"] is not None:
        metadata[DIAGNOSTIC] = row["diagnostic"]
    status = {
        "state": "TASK_STATE_" + STATES[row["phase"]],
        "message": {
            "messageId": correlation["a2aTaskId"] + ":" + row["phase"],
            "role": "ROLE_AGENT",
            "parts": [{"text": row["detail"]}],
            "extensions": [URN],
            "metadata": metadata,
        },
    }
    value = {
        "id": correlation["a2aTaskId"],
        "contextId": correlation["a2aContextId"],
        "status": status,
        "metadata": metadata,
    }
    if row["result"] is not None:
        value["artifacts"] = [
            {
                "artifactId": row["artifactId"],
                "extensions": [URN],
                "metadata": {URN: extension},
                "parts": [{"url": row["result"]["uri"], "mediaType": "application/json"}],
            }
        ]
    return value


def a2aError(error):
    if isinstance(error, ProfileError):
        kind = VersionNotSupportedError if error.detail == "PROFILE_VERSION" else InvalidParamsError
        return kind(message=error.detail, data={DIAGNOSTIC: jsonBytes(error.diagnostic).decode()})
    if isinstance(error, AdapterError):
        kind = (
            InternalError
            if error.kind in {"UNAVAILABLE", "FINALITY_CONFLICT"}
            else InvalidParamsError
        )
        return kind(message=error.detail)
    return error


class ProfileContext(DefaultServerCallContextBuilder):
    def build(self, request):
        context = super().build(request)
        context.state["raw"] = request.scope.get("agentlanceRaw")
        return context


class ProfileHandler(RequestHandler):
    def __init__(self, participant):
        self.participant = participant

    async def on_message_send(self, params, context):
        try:
            ensure(not context.tenant, "Tenancy unsupported", "UNSUPPORTED")
            raw = context.state["raw"]
            message = raw["message"]
            extension = checkHint(message, self.participant.schema)
            existing = self.participant.journal.checkMessage(message, extension)
            if (
                existing
                and message.get("taskId")
                and taskProjection(existing)["status"]["state"] in TERMINAL
            ):
                raise UnsupportedOperationError(message="Terminal tasks accept no more messages")
            row = await self.participant.receiveHint(message)
            if not params.configuration.return_immediately:
                async with asyncio.timeout(9):
                    while row["phase"] in {"ACCEPT_PENDING", "READY", "STARTED"}:
                        await asyncio.sleep(0.05)
                        row = self.participant.journal.get(extension["executionRef"])
            return toProto(taskProjection(row), Task)
        except (AdapterError, TimeoutError) as error:
            if isinstance(error, TimeoutError):
                raise InternalError(message="Poll existing task after request timeout") from error
            raise a2aError(error) from error

    async def on_get_task(self, params, context):
        if params.HasField("history_length") and params.history_length < 0:
            raise InvalidParamsError(message="Negative history length")
        row = self.participant.journal.byTaskId(params.id)
        if row is None:
            raise TaskNotFoundError()
        return toProto(taskProjection(row), Task)

    async def on_cancel_task(self, params, context):
        if self.participant.journal.byTaskId(params.id) is None:
            raise TaskNotFoundError()
        raise TaskNotCancelableError()

    async def unsupported(self, params, context):
        raise UnsupportedOperationError()

    async def pushUnsupported(self, params, context):
        raise PushNotificationNotSupportedError()

    async def on_message_send_stream(self, params, context):
        # SDK requires an async iterator even for a capability rejection.
        yield await self.unsupported(params, context)

    on_subscribe_to_task = on_message_send_stream
    on_list_tasks = on_get_extended_agent_card = unsupported
    on_create_task_push_notification_config = on_get_task_push_notification_config = pushUnsupported
    on_list_task_push_notification_configs = on_delete_task_push_notification_config = (
        pushUnsupported
    )


def agentApp(participant, cardBytes, lifespan=None):
    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def wireBoundary(request: Request, callNext):
        if not request.url.path.startswith("/a2a"):
            if (
                request.url.path != "/.well-known/agent-card.json"
                and not request.url.path.startswith("/artifacts/")
            ):
                return Response(status_code=404)
            return await callNext(request)
        try:
            if request.headers.get("A2A-Version") != "1.0":
                raise VersionNotSupportedError(
                    data={
                        DIAGNOSTIC: jsonBytes(
                            {"reason": "PROFILE_VERSION", "taskStatus": None, "receipt": None}
                        ).decode()
                    }
                )
            extensions = {
                token.strip() for token in request.headers.get("A2A-Extensions", "").split(",")
            }
            if URN not in extensions:
                raise ExtensionSupportRequiredError(
                    data={
                        DIAGNOSTIC: jsonBytes(
                            {"reason": "PROFILE_VERSION", "taskStatus": None, "receipt": None}
                        ).decode()
                    }
                )
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 65536:
                    return Response(status_code=413)
                raw.extend(chunk)
            request._body = bytes(raw)
            request.scope["agentlanceRaw"] = strictJson(bytes(raw)) if raw else None
            if request.url.path == "/a2a/message:send":
                value = request.scope["agentlanceRaw"]
                ensure(
                    isinstance(value, dict) and isinstance(value.get("message"), dict),
                    "Missing message",
                )
                checkHint(value["message"], participant.schema)
            response = await callNext(request)
            # SDK Struct emits 1.0 for profile constants. Restore SDK-owned metadata only.
            data = b"".join([chunk async for chunk in response.body_iterator])
            if "json" in response.headers.get("content-type", "") and data:
                data = jsonBytes(restoreIntegers(json.loads(data)))
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers["A2A-Extensions"] = URN
            return Response(data, status_code=response.status_code, headers=headers)
        except (AdapterError, A2AError) as error:
            payload = build_rest_error_payload(a2aError(error))
            return JSONResponse(
                payload, status_code=payload["error"]["code"], headers={"A2A-Extensions": URN}
            )

    @app.get("/.well-known/agent-card.json")
    async def card():
        return Response(cardBytes, media_type="application/json")

    @app.get("/artifacts/{digest}.json")
    async def artifact(digest: str):
        try:
            raw = participant.journal.readContent("0x" + digest)
        except AdapterError:
            return Response(status_code=404)
        return Response(raw, media_type="application/json", headers={"Cache-Control": "immutable"})

    # The SDK includes a tenant catch-all mount; register byte routes before it.
    app.router.routes.extend(
        create_rest_routes(
            ProfileHandler(participant),
            context_builder=ProfileContext(),
            path_prefix="/a2a",
            enable_v0_3_compat=False,
        )
    )
    return app
