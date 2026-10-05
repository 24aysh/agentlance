"""Black-box HTTP assertions. Fixtures supply setup; assertions import no reference worker."""

import asyncio
from copy import deepcopy

import pytest

from modules.adapters.a2a.client import AgentClient
from modules.adapters.a2a.profile import HEADERS, URN
from modules.agent_client.ports import AdapterError


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "duplicate",
        "version",
        "extension",
        "unknown",
        "float",
        "duplicate-key",
        "context",
        "metadata",
        "large",
        "tenant",
        "unknown-task",
    ],
)
def testHttpProfile(target, case):
    async def run():
        await target.award()
        message = deepcopy(target.message)
        headers = dict(HEADERS)
        payload = {"message": message, "configuration": {"returnImmediately": True}}
        if case == "version":
            headers["A2A-Version"] = "0.3"
        if case == "extension":
            headers.pop("A2A-Extensions")
        if case == "unknown":
            message["metadata"][URN]["extra"] = True
        if case == "float":
            message["metadata"][URN]["profileVersion"] = 1.0
        if case == "context":
            message["contextId"] = "unknown"
        if case == "metadata":
            message["metadata"] = []
        url = target.config["agentOrigin"] + "/a2a/message:send"
        if case == "tenant":
            response = await target.http.get(
                target.config["agentOrigin"] + "/tenant/a2a/tasks/unknown", headers=headers
            )
        elif case == "unknown-task":
            response = await target.http.get(
                target.config["agentOrigin"] + "/a2a/tasks/unknown", headers=headers
            )
        elif case == "duplicate-key":
            response = await target.http.post(
                url, content=b'{"message":{},"message":{}}', headers=headers
            )
        elif case == "large":
            response = await target.http.post(url, content=b" " * 65537, headers=headers)
        else:
            response = await target.http.post(url, json=payload, headers=headers)
        if case in {"valid", "duplicate"}:
            assert response.status_code == 200, response.text
            task = response.json()["task"]
            assert task["metadata"][URN] == message["metadata"][URN]
            assert type(task["metadata"][URN]["profileVersion"]) is int
            assert task["metadata"][URN]["result"] is None
            assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
            assert task["id"] != message["metadata"][URN]["executionRef"]["taskRef"]["taskId"]
            if case == "duplicate":
                message["messageId"] = "another"
                duplicate = await target.http.post(url, json=payload, headers=headers)
                assert duplicate.json()["task"] == task
            polled = await target.http.get(
                target.config["agentOrigin"] + "/a2a/tasks/" + task["id"], headers=headers
            )
            assert polled.json() == task
        else:
            assert response.status_code in {400, 404, 413, 422}, response.text
        assert not target.calls

    asyncio.run(run())


def testSdkArtifactAndOrdering(target):
    async def run():
        await target.complete()
        client = AgentClient(target.cardBytes, target.http, target.schema)
        task = await client.sendHint(target.message)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert await client.fetchArtifact(task, target.content) == b'{"value":7}\n'
        assert await client.getTask(task["id"], target.extension) == task
        older = deepcopy(task)
        older["status"]["state"] = "TASK_STATE_WORKING"
        older["metadata"][URN]["result"] = None
        older.pop("artifacts")
        assert client.correlate(older, target.extension) == task
        for field in ("id", "contextId"):
            bad = deepcopy(task)
            bad[field] = "changed"
            with pytest.raises(AdapterError):
                client.correlate(bad, target.extension)
        bad = deepcopy(task)
        bad["metadata"][URN]["result"]["digest"] = "0x" + "ff" * 32
        with pytest.raises(AdapterError):
            client.correlate(bad, target.extension)
        explicit = deepcopy(target.message)
        explicit.update(taskId=task["id"], contextId=task["contextId"])
        response = await target.http.post(
            target.config["agentOrigin"] + "/a2a/message:send",
            headers=HEADERS,
            json={"message": explicit},
        )
        assert response.status_code == 400
        assert len(target.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("change", ["context", "task", "message-id", "input", "result"])
def testCorrelationConflicts(target, change):
    async def run():
        await target.award()
        url = target.config["agentOrigin"] + "/a2a/message:send"
        first = await target.http.post(url, headers=HEADERS, json={"message": target.message})
        task = first.json()["task"]
        bad = deepcopy(target.message)
        if change == "context":
            bad.update(taskId=task["id"], contextId="wrong")
        if change == "task":
            bad["taskId"] = "wrong"
        if change == "message-id":
            bad["metadata"][URN]["executionRef"]["taskRef"]["taskId"] = "2"
        if change == "input":
            bad["metadata"][URN]["inputDigest"] = "0x" + "ab" * 32
        if change == "result":
            bad["metadata"][URN]["result"] = {
                "uri": "https://example.com/result",
                "digest": "0x" + "ab" * 32,
            }
        response = await target.http.post(url, headers=HEADERS, json={"message": bad})
        assert response.status_code == 400
        unchanged = await target.http.get(
            target.config["agentOrigin"] + "/a2a/tasks/" + task["id"], headers=HEADERS
        )
        assert unchanged.json() == task and not target.calls

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["agent", "input", "policy", "artifact-binding", "artifact-id", "artifact-url"]
)
def testUntrustedResultBinding(target, change):
    async def run():
        await target.complete()
        client = AgentClient(target.cardBytes, target.http, target.schema)
        task = await client.sendHint(target.message)
        bad = deepcopy(task)
        ext = bad["metadata"][URN]
        if change == "agent":
            ext["agentRef"]["agentId"] = "8"
        if change == "input":
            ext["inputDigest"] = "0x" + "ff" * 32
        if change == "policy":
            ext["validationPolicyDigest"] = "0x" + "ff" * 32
        if change == "artifact-binding":
            bad["artifacts"][0]["metadata"][URN]["inputDigest"] = "0x" + "ff" * 32
        if change == "artifact-id":
            bad["artifacts"][0]["artifactId"] = "replacement"
        if change == "artifact-url":
            bad["artifacts"][0]["parts"][0]["url"] = "https://elsewhere.example/forged"
        with pytest.raises(AdapterError):
            client.correlate(bad, target.extension)
        assert await client.getTask(task["id"], target.extension) == task

    asyncio.run(run())
