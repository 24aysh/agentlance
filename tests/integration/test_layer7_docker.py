import asyncio
import base64
from pathlib import Path

import pytest

from modules.adapters.a2a.profile import strictJson
from modules.adapters.execution.docker import DockerExecutor, command
from modules.agent_client.ports import AdapterError
from modules.validation.runtime import evaluatorProfile
from modules.validation.test_evaluator import evaluation
from tests.integration.test_layer6_docker import hostileImage as hostileImage


@pytest.mark.parametrize("kind", ["cpu", "memory", "hang", "output", "logs"])
def testEvaluatorOperationalLimitsProduceNoEvidence(hostileImage, kind, tmp_path):
    async def run():
        docker = DockerExecutor(namespace=str(tmp_path))
        profile = evaluatorProfile(hostileImage)
        profile["limits"]["cpuSeconds"] = 1
        if kind != "cpu":
            profile["limits"]["requestSeconds"] = 2
        with pytest.raises((AdapterError, TimeoutError)):
            await docker.run({"fixture": kind}, "limit", {"kind": kind}, profile)
        code, output, _ = await command(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "name=^/" + docker.containerName({"fixture": kind}, "limit") + "$",
            ]
        )
        assert code == 0 and output == b""

    asyncio.run(run())


def testEvaluatorLargerFrameAndIsolation(tmp_path):
    async def run():
        docker = DockerExecutor(namespace=str(tmp_path))
        image = Path(".scratch/layer7/image-id").read_text().strip()
        profile = evaluatorProfile(image)
        task, result, *raws = evaluation(b" " * 1048577)
        request = {"task": task, "result": result} | {
            field: base64.b64encode(raw).decode()
            for field, raw in zip(("input", "shape", "policy", "artifact"), raws, strict=True)
        }
        await docker.preflight(profile)
        try:
            evidence = strictJson(await docker.run(task["taskRef"], "oversize", request, profile))
            assert evidence["reason"] == "RESULT_LIMIT"
            _, raw, _ = await command(
                ["docker", "inspect", docker.containerName(task["taskRef"], "oversize")]
            )
            container = strictJson(raw)[0]
            config = container["HostConfig"]
            assert config["Memory"] == 128 * 1048576 and config["ReadonlyRootfs"]
            assert config["NetworkMode"] == "none" and config["CapDrop"] == ["ALL"]
            assert {"Name": "cpu", "Hard": 5, "Soft": 5} in config["Ulimits"]
            assert container["Config"]["User"] == "65532:65532" and not container["Mounts"]
        finally:
            await docker.stop(task["taskRef"], "oversize")

    asyncio.run(run())
