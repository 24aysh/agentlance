"""Requester commands and public inspection. Writes require explicit --broadcast."""

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from apps.reference_agent.chain import readKeystore
from apps.runtime_logging import configureLogging
from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.chain.codec import WireCodec
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.registry.chain import MonadRegistry
from modules.adapters.registry.feedback import FeedbackAdapter
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from modules.adapters.storage.journal import Journal
from modules.agent_client.application import ROOT, validateOutput
from modules.agent_client.discovery import MarketWatcher
from modules.agent_client.inspection import Inspector
from modules.agent_client.ports import AdapterError, closed, ensure, recordCheck
from modules.agent_client.requester import Requester
from modules.agent_client.signing import contentDigest
from modules.domain.records import ProtocolViolation
from modules.validation.disclosure import exportCost
from modules.validation.qualification import verifyValidatorEvidence


def readJson(path):
    with Path(path).open("rb") as source:
        raw = source.read(1048577)
    ensure(len(raw) <= 1048576, "JSON file byte limit")
    return strictJson(raw)


@asynccontextmanager
async def application(config, *, signing=False):
    closed(
        config,
        "networkScope manifestPath registryVerificationPath validatorVerificationPath "
        "rpcUrl genesisHash database keystore maxFinalizedAgeSeconds maxGas maxGasPriceWei "
        "ipfsGateway fixtureOrigins caFile",
    )
    schema = readJson(ROOT / "specs/schemas/protocol.schema.json")
    facts = readJson(config["manifestPath"])
    scope = config["networkScope"]
    ensure(scope in {"LOCAL_FIXTURE", "QUALIFIED_TESTNET"}, "Network scope")
    verification = (
        readJson(config["registryVerificationPath"]) if config["registryVerificationPath"] else {}
    )
    if scope == "LOCAL_FIXTURE":
        rpcUrl = urlsplit(config["rpcUrl"])
        ensure(
            facts["chainId"] == "31337"
            and rpcUrl.hostname in {"localhost", "127.0.0.1", "::1"}
            and rpcUrl.scheme in {"http", "https"},
            "Fixture requires isolated local RPC",
        )
    else:
        recordCheck(facts, "DeploymentManifest", schema)
        ensure(
            config["rpcUrl"] in facts["rpcUrls"]
            and not config["fixtureOrigins"]
            and config["caFile"] is None,
            "Qualified network configuration",
        )
        identity = verification.get("identityVerification")
        ensure(
            isinstance(identity, dict)
            and identity.get("identityRegistry") == facts["identityRegistry"]
            and identity.get("identityVersion") == facts["identityVersion"]
            and bool(verification.get("feedbackVerification")),
            "Complete registry qualification binding",
        )
        for field in ("registryVerification", "validatorVerification"):
            path = config[field + "Path"]
            ensure(
                path is not None
                and contentDigest(Path(path).read_bytes()) == facts[field]["digest"],
                "Qualification document digest",
            )
        validatorEvidence = readJson(config["validatorVerificationPath"])
        verifyValidatorEvidence(validatorEvidence, facts, validatorEvidence["payload"]["image"])
    account = readKeystore(config["keystore"]) if signing and config["keystore"] else None
    ensure(
        not signing or account is not None, "Encrypted requester keystore required", "UNAVAILABLE"
    )
    Path(config["database"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Journal(
        config["database"], {"role": "application", "mode": scope, "market": facts["market"]}
    )
    os.chmod(config["database"], 0o600)
    rpc = Web3Rpc(config["rpcUrl"])
    network = NetworkPolicy(config["fixtureOrigins"])
    http = createHttpClient(network, config["caFile"])
    try:
        codec = WireCodec(
            schema,
            readJson(ROOT / "specs/catalog.json"),
            readJson(ROOT / "specs/protocol.abi.json"),
        )
        # Isolated fixtures explicitly use their advanced EVM clock; live reads use wall time.
        fixtureNow = (
            int((await rpc.call("eth_getBlockByNumber", "latest", False))["timestamp"], 16)
            if scope == "LOCAL_FIXTURE"
            else None
        )
        chain = ChainConnection(
            rpc,
            facts,
            config["genesisHash"],
            journal,
            codec,
            readJson(ROOT / "specs/contracts.read.abi.json"),
            config["maxFinalizedAgeSeconds"],
            clock=(lambda: fixtureNow) if fixtureNow is not None else time.time,
        )
        registry = (
            MonadRegistry(
                chain,
                readJson(ROOT / "specs/registry.identity.abi.json"),
                verification["identityVerification"],
            )
            if verification.get("identityVerification")
            else None
        )
        market = MonadMarket(
            chain,
            account,
            registry,
            maxGas=config["maxGas"],
            maxGasPriceWei=int(config["maxGasPriceWei"]),
        )
        publisher = None
        if verification.get("feedbackVerification"):
            evidence = verification["feedbackVerification"]
            if scope == "QUALIFIED_TESTNET":
                ensure(
                    evidence["reputationRegistry"] == facts["reputationRegistry"],
                    "Reputation binding",
                )
                hashes = {x["address"]: x["codeHash"] for x in evidence["codeObservations"]}
                ensure(
                    hashes[facts["feedbackPublisher"]] == facts["publisherCodeHash"]
                    and hashes[facts["reputationRegistry"]] == facts["reputationCodeHash"],
                    "Feedback code binding",
                )
            publisher = FeedbackAdapter(
                market,
                facts["feedbackPublisher"],
                readJson(ROOT / "specs/feedback-publisher.abi.json"),
                readJson(ROOT / "specs/registry.reputation.abi.json"),
                evidence,
            )
        content = ContentStore(http, network, journal, ipfsGateway=config["ipfsGateway"])
        inspector = Inspector(
            market,
            MarketWatcher(chain, registry),
            content,
            networkScope=scope,
            publisher=publisher,
            signingTypes=readJson(ROOT / "specs/signing/types.json"),
        )
        yield Requester(market, content), inspector
    finally:
        await http.aclose()
        await rpc.close()
        journal.close()


def jsonArgument(value):
    return strictJson(value.encode())


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("AGENTLANCE_APP_CONFIG"))
    groups = parser.add_subparsers(dest="group", required=True)
    for name, commands in {
        "task": ["validate", "create", "allocate", "expire", "cancel", "show", "bids", "tree"],
        "tasks": ["list"],
        "credit": ["show", "withdraw"],
        "operation": ["show", "resume"],
        "agent": ["show"],
        "cost": ["export"],
    }.items():
        sub = groups.add_parser(name).add_subparsers(dest="action", required=True)
        for action in commands:
            command = sub.add_parser(action)
            if name == "task" and action in {"validate", "create"}:
                command.add_argument("--terms-file", required=True)
                if action == "validate":
                    command.add_argument("--requester", required=True)
            elif name == "task":
                command.add_argument("--task-ref", type=jsonArgument, required=True)
            if name == "agent":
                command.add_argument("--agent-ref", type=jsonArgument, required=True)
                command.add_argument("--task-ref", type=jsonArgument)
            if name == "tasks":
                command.add_argument("--kind", choices=["ALL", "ROOT", "CHILD"], default="ALL")
                command.add_argument("--parent-ref", type=jsonArgument)
            if name == "credit":
                if action == "show":
                    command.add_argument("--owner", required=True)
                else:
                    command.add_argument("--receiver", required=True)
                    command.add_argument("--amount-atoms", required=True)
            if (
                name == "operation"
                or (name == "task" and action in {"create", "allocate", "expire", "cancel"})
                or action == "withdraw"
            ):
                command.add_argument("--request-id", required=True)
            write = (
                name == "task" and action in {"create", "allocate", "expire", "cancel"}
            ) or action in {"resume", "withdraw"}
            if write:
                command.add_argument("--broadcast", action="store_true", required=True)
            if name == "operation" and action == "show":
                command.add_argument("--sender")
            if (
                (name == "task" and action in {"show", "bids"})
                or name == "tasks"
                or (name == "credit" and action == "show")
            ):
                command.add_argument("--limit", type=int, default=20)
                command.add_argument("--cursor")
            if name == "task" and action == "show":
                command.add_argument("--artifacts", action="store_true")
            if name == "cost":
                command.add_argument("--journal", required=True)
                command.add_argument("--execution-ref", type=jsonArgument, required=True)
                command.add_argument(
                    "--output", help="Create a new local file; existing files are preserved"
                )
            command.set_defaults(signing=write)
    return parser.parse_args()


async def run(args):
    if args.group == "cost":
        result = exportCost(
            args.journal, args.execution_ref, readJson(ROOT / "specs/schemas/protocol.schema.json")
        )
        if args.output:
            with Path(args.output).open("xb") as target:
                os.chmod(args.output, 0o600)
                target.write(jsonBytes(result) + b"\n")
        return result
    ensure(args.config, "--config or AGENTLANCE_APP_CONFIG required", "UNAVAILABLE")
    async with application(readJson(args.config), signing=args.signing) as (requester, inspector):
        group, action = args.group, args.action
        if group == "operation":
            data = (
                await requester.resume(args.request_id)
                if action == "resume"
                else await requester.readOperation(args.request_id, args.sender)
            )
            return inspector.envelope("operation", data, data["stamp"])
        if args.signing:
            name = {
                "create": "createTask",
                "cancel": "cancelTask",
                "expire": "expireTask",
                "allocate": "allocateTask",
                "withdraw": "withdrawCredit",
            }[action]
            data = (
                {"terms": readJson(args.terms_file)}
                if action == "create"
                else {"receiver": args.receiver, "amountAtoms": args.amount_atoms}
                if action == "withdraw"
                else {"taskRef": args.task_ref}
            )
            data = await requester.submit(
                {"schemaVersion": 1, "command": name, "input": data}, args.request_id
            )
            return inspector.envelope("operation", data, data["stamp"])
        if group == "tasks":
            return await inspector.listOpenTasks(
                args.kind, args.parent_ref, args.limit, args.cursor
            )
        if group == "agent":
            return await inspector.readAgentDetails(args.agent_ref, args.task_ref)
        if group == "credit":
            return await inspector.readCredit(args.owner, args.limit, args.cursor)
        if action == "validate":
            data = await requester.validateTaskRequest(readJson(args.terms_file), args.requester)
            return inspector.envelope("validation", data, data["stamp"])
        if action == "tree":
            return await inspector.readTaskTree(args.task_ref)
        if action == "bids":
            return await inspector.listTaskBids(args.task_ref, args.limit, args.cursor)
        return await inspector.readTaskDetails(
            args.task_ref, artifacts=args.artifacts, limit=args.limit, cursor=args.cursor
        )


def main():
    configureLogging()
    try:
        args = arguments()
        result = asyncio.run(run(args))
        validateOutput(result, "CostDisclosure" if args.group == "cost" else "Envelope")
        sys.stdout.buffer.write(jsonBytes(result) + b"\n")
    except (
        AdapterError,
        ProtocolViolation,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
    ) as error:
        # Do not echo provider payloads, paths, secrets or a keystore exception to stdout.
        kind = error.kind if isinstance(error, AdapterError) else "INVALID_DATA"
        diagnostic = {"version": 1, "error": kind}
        if isinstance(error, AdapterError):
            diagnostic["detail"] = error.detail[:256]
        sys.stderr.write(jsonBytes(diagnostic).decode() + "\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
