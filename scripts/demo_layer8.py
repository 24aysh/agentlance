"""CLI-driven non-frontend acceptance: independent agent, child markets and refunds."""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.adapters.a2a.profile import jsonBytes, strictJson  # noqa: E402
from modules.agent_client.signing import contentDigest  # noqa: E402
from tests.integration.test_layer8_external import independentScenario  # noqa: E402
from tests.layer3_support import localNode  # noqa: E402
from tests.layer4_support import DiscoveredWorker, Layer4Rig  # noqa: E402
from tests.layer6_market_support import runDelegation  # noqa: E402
from tests.layer7_support import ValidatorProcess, localKubo  # noqa: E402
from tests.layer8_support import PublicTls, appConfig, cli, cliWrite, currentRead  # noqa: E402


class DemoRequester:
    def __init__(self, directory, public):
        self.directory, self.public = directory, public
        self.config = None
        self.creation = None

    async def create(self, env, worker, **options):
        self.config = appConfig(env, self.directory, self.public)
        terms = env.rig.terms(**options)
        for field, filename in (
            ("input", "/input.json"),
            ("outputSchema", "/output-schema.json"),
            ("validationPolicy", "/policy.json"),
        ):
            raw = worker.blobs[filename]
            terms[field] = self.public.publish(raw)
            # Existing L6 workers use their explicitly labeled fixture transport.
            (self.directory / "public" / terms[field]["digest"]).write_bytes(raw)
        path = self.directory / "terms.json"
        path.write_bytes(jsonBytes(terms))
        self.creation = await cliWrite(
            env, self.config, "task", "create", "root", "--terms-file", str(path)
        )
        return (await env.market.readTask(self.creation["data"]["taskRef"]))["task"]

    async def allocate(self, env, task):
        env.finalize()
        await cliWrite(
            env,
            self.config,
            "task",
            "allocate",
            "allocate",
            "--task-ref",
            jsonBytes(task["taskRef"]).decode(),
        )

    async def inspect(self, env, task, validator):
        # The public read configuration acquires the verified fixture publisher facts.
        deployment = strictJson(validator.configPath.read_bytes())["deployment"]
        self.config = appConfig(
            env,
            self.directory,
            self.public,
            facts=env.facts | {"feedbackPublisher": deployment[0]},
            feedback=deployment[1],
        )
        ref = {"taskRef": task["taskRef"], "awardId": 1}
        tree = await currentRead(
            self.config, "task", "tree", "--task-ref", jsonBytes(task["taskRef"]).decode()
        )
        assert len(tree["data"]["nodes"]) == 3
        assert all(node["receipt"]["reason"] == "SUCCESS" for node in tree["data"]["nodes"])
        args = (
            "cost",
            "export",
            "--journal",
            str(env.path),
            "--execution-ref",
            jsonBytes(ref).decode(),
        )
        first = await cli(self.config, *args, "--output", str(self.directory / "disclosure.json"))
        second = await cli(self.config, *args)
        assert first | {"exportedAt": second["exportedAt"]} == second
        assert (
            first["ownReportedExecutionAtoms"] is not None
            and first["directChildPaidAtoms"] is not None
        )
        assert first["forecast"] is not None and first["savedRateActualAtoms"] is not None
        owners = sorted(
            {
                address
                for node in tree["data"]["nodes"]
                for address in (node["receipt"]["payout"], node["receipt"]["refundAddress"])
                if address
            }
        )
        credits = [
            await currentRead(self.config, "credit", "show", "--owner", owner) for owner in owners
        ]
        return {"creation": self.creation, "tree": tree, "cost": first, "credits": credits}


async def childrenScenario(rpc, directory, kubo):
    public = PublicTls(directory, kubo)
    requester = DemoRequester(directory, public)

    async def factory(env, files):
        return await ValidatorProcess().open(env, directory, files, kubo)

    try:
        report = await runDelegation(rpc, directory, validatorFactory=factory, requester=requester)
        return {
            "application": report["application"],
            "childDeposits": report["childDeposits"],
            "workerProcesses": report["independentProcesses"],
        }
    finally:
        public.close()


async def refundScenario(rpc, directory, kubo, timeout=False):
    env = Layer4Rig(rpc, directory)
    worker = DiscoveredWorker(env)
    public = PublicTls(directory, kubo)
    files = directory / "public"
    files.mkdir()
    for path, raw in worker.blobs.items():
        (files / contentDigest(("https://agent.example" + path).encode())).write_bytes(raw)
    validator = await ValidatorProcess().open(env, directory, files, kubo)
    requester = DemoRequester(directory, public)
    try:
        task = await requester.create(env, worker)
        ref = task["taskRef"]
        env.command("submitBid", env.rig.bid(int(ref["taskId"])), caller="owner")
        env.rig.advance(int(task["terms"]["biddingClose"]))
        await requester.allocate(env, task)
        execution = {"taskRef": ref, "awardId": 1}
        env.command(
            "acceptAward", {"executionRef": execution, "ownWorkReserveAtoms": "0"}, caller="signer"
        )
        raw = b'{"value":7}\n' if timeout else b'{"value":8}\n'
        artifact = public.publish(raw)
        (files / artifact["digest"]).write_bytes(raw)
        env.command(
            "submitResult", {"executionRef": execution, "artifact": artifact}, caller="signer"
        )
        if timeout:
            env.rig.advance(int(task["terms"]["validationBy"]))
            env.finalize()
            await cliWrite(
                env,
                requester.config,
                "task",
                "expire",
                "timeout",
                "--task-ref",
                jsonBytes(ref).decode(),
            )
        outcome = await validator.settle(task)
        assert outcome["receipt"]["reason"] == (
            "VALIDATOR_TIMEOUT" if timeout else "VALIDATION_FAILED"
        )
        deployment = strictJson(validator.configPath.read_bytes())["deployment"]
        config = appConfig(
            env,
            directory,
            public,
            facts=env.facts | {"feedbackPublisher": deployment[0]},
            feedback=deployment[1],
        )
        details = await currentRead(
            config, "task", "show", "--task-ref", jsonBytes(ref).decode(), "--artifacts"
        )
        assert details["data"]["feedback"]["status"] == (
            "NOT_APPLICABLE" if timeout else "PUBLISHED"
        )
        refund = await currentRead(config, "credit", "show", "--owner", env.rig.actors["requester"])
        withdrawal = await cliWrite(
            env,
            config,
            "credit",
            "withdraw",
            "refund",
            "--receiver",
            env.rig.actors["receiver"],
            "--amount-atoms",
            refund["data"]["credit"]["amountAtoms"],
        )
        return {"creation": requester.creation, "task": details, "withdrawal": withdrawal}
    finally:
        await validator.close()
        await worker.http.aclose()
        public.close()
        await env.close()


def runDemo(directory):
    directory.mkdir(parents=True, exist_ok=True)
    reports = []
    with localKubo() as kubo:
        for name in ("independent", "children", "fail", "timeout"):
            work = directory / name
            work.mkdir()
            with localNode(work) as rpc:
                if name == "independent":
                    value = asyncio.run(independentScenario(rpc, work, kubo))
                    journal = value.pop("journal")
                    value["externalTransactions"] = [
                        {
                            "command": op["name"],
                            "transactionHash": op["hash"],
                            "status": op["status"],
                        }
                        for op in journal["operations"].values()
                    ]
                elif name == "children":
                    value = asyncio.run(childrenScenario(rpc, work, kubo))
                else:
                    value = asyncio.run(refundScenario(rpc, work, kubo, name == "timeout"))
            reports.append({"name": name, "result": value})
    report = {
        "layer": "L8",
        "networkScope": "LOCAL_FIXTURE",
        "sourceRevision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "frontend": "deferred",
        "liveQualification": "pending",
        "paidModelCalls": 0,
        "ownership": (
            "Shared fixture ownership; independent Node implementation and OS process; "
            "no third-party operator"
        ),
        "infrastructure": (
            "Local Monad EVM, TLS, Docker, Kubo and pinned publisher/reputation. "
            "Explicit fixture clock and deterministic model transport."
        ),
        "economics": (
            "Success credits only; children funded independently. Validator trusted; "
            "identities do not prevent Sybil/common control."
        ),
        "research": (
            "Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private "
            "Information, Liu et al., supplied arXiv:2608.23867v1. See README attribution."
        ),
        "scenarios": reports,
    }
    output = directory / "demo.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    return output


def main():
    from scripts.layer7_tools import setupLayer7

    setupLayer7()
    scratch = ROOT / ".scratch/layer8"
    scratch.mkdir(parents=True, exist_ok=True)
    output = runDemo(Path(tempfile.mkdtemp(prefix="demo-", dir=scratch)))
    print("L8 non-frontend local evidence: " + str(output))


if __name__ == "__main__":
    main()
