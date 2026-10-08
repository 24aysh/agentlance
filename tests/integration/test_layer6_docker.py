import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from modules.adapters.execution.docker import DockerExecutor, command
from modules.agent_client.ports import AdapterError
from tests.layer6_support import executionProfile


@pytest.fixture(scope="module")
def hostileImage(tmp_path_factory):
    directory = tmp_path_factory.mktemp("bounded-image")
    source = """import json, os, subprocess, sys, time
r=json.load(sys.stdin)
k=r["kind"]
if k=="cpu":
    while True: pass
elif k=="hang": time.sleep(30)
elif k=="memory":
    x=[]
    while True: x.append(bytearray(1024*1024))
elif k=="output": sys.stdout.write("x"*1000000)
elif k=="logs": sys.stderr.write("x"*1000000)
elif k=="pids":
    children=[]
    try:
        while True: children.append(subprocess.Popen(["sleep","30"]))
    except OSError: print(json.dumps({"count":len(children)}))
elif k=="isolation":
    blocked=[]
    for path in ("/worker.py","/etc/probe"):
        try: open(path,"w").write("bad")
        except OSError: blocked.append(path)
    print(json.dumps({"uid":os.getuid(),"blocked":blocked,"env":list(os.environ)}))
"""
    (directory / "worker.py").write_text(source)
    (directory / "Dockerfile").write_text(
        "FROM python:3.12.12-alpine3.22@sha256:"
        "848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32\n"
        'COPY worker.py /worker.py\nENTRYPOINT ["python","-I","-B","/worker.py"]\n'
    )
    subprocess.run(
        ["docker", "build", "-q", "-t", "agentlance-execution:test-limits", str(directory)],
        check=True,
        capture_output=True,
    )
    image = subprocess.check_output(
        ["docker", "image", "inspect", "agentlance-execution:test-limits", "--format", "{{.Id}}"],
        text=True,
    ).strip()
    return image


def profileFor(image):
    profile = executionProfile(
        SimpleNamespace(
            signer="0x" + "01" * 20, supportedDigests=("0x" + "01" * 32, "0x" + "02" * 32)
        )
    )
    profile["executor"]["image"] = image
    profile["limits"].update(requestSeconds=1, pids=16)
    return profile


@pytest.mark.parametrize("kind", ["hang", "memory", "output", "logs"])
def testContainerLimitsKillAndNoRedispatch(hostileImage, kind, tmp_path):
    async def scenario():
        docker = DockerExecutor()
        profile = profileFor(hostileImage)
        with pytest.raises((AdapterError, TimeoutError)):
            await docker.run(
                {"fixture": str(tmp_path) + kind}, "adversarial", {"kind": kind}, profile
            )
        code, raw, _ = await command(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                "name=" + docker.containerName({"fixture": str(tmp_path) + kind}, "adversarial"),
            ]
        )
        assert code == 0 and not raw.strip()

    asyncio.run(scenario())


def testContainerPrivilegeFilesystemAndPidLimits(hostileImage):
    async def scenario():
        from modules.adapters.a2a.profile import strictJson

        docker = DockerExecutor()
        profile = profileFor(hostileImage)
        # This case measures isolation and PID enforcement, not the one-second
        # timeout exercised above. Leave enough time for cold Docker startup.
        profile["limits"]["requestSeconds"] = 5
        await docker.preflight(profile)
        try:
            result = strictJson(
                await docker.run({"fixture": "isolation"}, "step", {"kind": "isolation"}, profile)
            )
            assert result["uid"] == 65532 and len(result["blocked"]) == 2
            assert not {"OPENAI_API_KEY", "TYPESAFE_API_KEY"}.intersection(result["env"])
            code, raw, _ = await command(
                ["docker", "inspect", docker.containerName({"fixture": "isolation"}, "step")]
            )
            assert code == 0
            config = strictJson(raw)[0]
            assert config["HostConfig"]["NetworkMode"] == "none"
            assert (
                config["HostConfig"]["ReadonlyRootfs"] and config["HostConfig"]["PidsLimit"] == 16
            )
            assert config["HostConfig"]["Memory"] == 67108864
            assert config["HostConfig"]["NanoCpus"] == 500000000
            assert config["HostConfig"]["CapDrop"] == ["ALL"] and config["Mounts"] == []
            result = strictJson(
                await docker.run({"fixture": "pids"}, "step", {"kind": "pids"}, profile)
            )
            assert 0 < result["count"] < 16
        finally:
            await docker.stop({"fixture": "isolation"}, "step")
            await docker.stop({"fixture": "pids"}, "step")

    asyncio.run(scenario())
