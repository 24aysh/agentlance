"""No host task-code execution, mounts, network or secret-bearing container environment."""

import asyncio

from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.agent_client.ports import AdapterError, ensure
from modules.economics.records import recordDigest


async def command(args, *, raw=None, timeout=10, maximum=65536):
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        raise AdapterError("UNAVAILABLE", "Docker CLI unavailable") from error

    async def readBounded(stream):
        result = bytearray()
        while chunk := await stream.read(8192):
            ensure(len(result) + len(chunk) <= maximum, "Process output/log limit", "LIMIT")
            result.extend(chunk)
        return bytes(result)

    async def send():
        try:
            process.stdin.write(raw or b"")
            await process.stdin.drain()
        finally:
            process.stdin.close()

    tasks = [
        asyncio.create_task(send()),
        asyncio.create_task(readBounded(process.stdout)),
        asyncio.create_task(readBounded(process.stderr)),
    ]
    try:
        async with asyncio.timeout(timeout):
            _, output, error = await asyncio.gather(*tasks)
            code = await process.wait()
        return code, output, error
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            process.kill()

        async def discard(stream):
            while await stream.read(8192):
                pass

        # A killed CLI can still have a paused/full pipe: drain without retaining bytes.
        await asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())


class DockerExecutor:
    def __init__(self, namespace="standalone"):
        self.namespace = namespace

    def containerName(self, ref, stepId):
        return "agentlance-" + recordDigest([self.namespace, ref, stepId])[2:50]

    async def preflight(self, profile):
        code, output, _ = await command(["docker", "info", "--format", "{{json .}}"])
        ensure(code == 0, "Docker daemon unavailable", "UNAVAILABLE")
        info = strictJson(output)
        ensure(
            info["OSType"] == "linux"
            and info["MemoryLimit"]
            and info["PidsLimit"]
            and info["CpuCfsQuota"],
            "Docker resource enforcement unavailable",
            "UNAVAILABLE",
        )
        code, output, _ = await command(
            ["docker", "image", "inspect", profile["executor"]["image"]]
        )
        ensure(
            code == 0 and strictJson(output)[0]["Id"] == profile["executor"]["image"],
            "Pinned prebuilt image unavailable",
            "UNAVAILABLE",
        )

    async def stop(self, ref, stepId):
        name = self.containerName(ref, stepId)
        code, output, _ = await command(["docker", "ps", "-aq", "--filter", "name=^/" + name + "$"])
        ensure(code == 0, "Cannot confirm container stopped", "UNAVAILABLE")
        if output.strip():
            code, _, _ = await command(["docker", "rm", "--force", name])
            ensure(code == 0, "Cannot stop execution container", "UNAVAILABLE")

    async def run(self, ref, stepId, request, profile):
        limits = profile["limits"]
        raw = jsonBytes(request)
        ensure(len(raw) <= limits["requestBytes"], "Container input limit", "LIMIT")
        name = self.containerName(ref, stepId)
        args = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "agentlance.step=" + name,
            "--pull=never",
            "--interactive",
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--cpus=" + str(limits["cpuMillis"] / 1000),
            "--memory=" + str(limits["memoryBytes"]),
            "--memory-swap=" + str(limits["memoryBytes"]),
            "--pids-limit=" + str(limits["pids"]),
            "--ulimit=nofile=64:64",
            "--tmpfs=/scratch:rw,noexec,nosuid,nodev,size=" + str(limits["workspaceBytes"]),
            "--log-driver=none",
            "--workdir=/scratch",
            profile["executor"]["image"],
        ]
        if "cpuSeconds" in limits:
            ensure(
                type(limits["cpuSeconds"]) is int and 1 <= limits["cpuSeconds"] <= 5, "CPU budget"
            )
            args[-1:-1] = ["--ulimit=cpu={0}:{0}".format(limits["cpuSeconds"])]
        code, _, diagnostic = await command(args)
        ensure(
            code == 0,
            "Container creation failed; existing step is never replaced: "
            + diagnostic.decode(errors="replace")[:240],
            "UNKNOWN",
        )
        try:
            code, output, _ = await command(
                ["docker", "start", "--attach", "--interactive", name],
                raw=raw,
                timeout=limits["requestSeconds"],
                maximum=min(limits["outputBytes"], limits["logBytes"]),
            )
            ensure(code == 0, "Container failed", "EXECUTOR_FAILED")
            return output
        except BaseException:
            await asyncio.shield(self.stop(ref, stepId))
            raise
