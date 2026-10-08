"""Separate validator/exporter process, with encrypted host-only signing keys."""

import argparse
import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from apps.reference_agent.chain import readKeystore
from apps.runtime_logging import configureLogging
from modules.adapters.a2a.profile import jsonBytes, strictJson
from modules.adapters.chain.codec import WireCodec
from modules.adapters.chain.market import MonadMarket
from modules.adapters.chain.rpc import ChainConnection, Web3Rpc
from modules.adapters.execution.docker import DockerExecutor
from modules.adapters.registry.feedback import FeedbackAdapter
from modules.adapters.storage.content import ContentStore, NetworkPolicy, createHttpClient
from modules.adapters.storage.ipfs import KuboPublisher
from modules.adapters.storage.journal import Journal
from modules.adapters.storage.validation import ValidationStore
from modules.agent_client.discovery import MarketWatcher
from modules.agent_client.ports import AdapterError, closed, ensure, recordCheck
from modules.agent_client.signing import contentDigest
from modules.validation.runtime import ValidatorRuntime

ROOT = Path(__file__).resolve().parents[2]
LOG = logging.getLogger(__name__)


def readProtocol():
    return [
        strictJson((ROOT / name).read_bytes())
        for name in (
            "specs/schemas/protocol.schema.json",
            "specs/catalog.json",
            "specs/protocol.abi.json",
        )
    ]


async def run(config, retry=None):
    closed(
        config,
        "manifestPath registryVerificationPath validatorVerificationPath "
        "genesisHash rpcUrl database "
        "validatorKeystore relayerKeystore image kuboRpcUrl kuboAuthorizationEnv ipfsGateway "
        "maxFinalizedAgeSeconds maxGas maxGasPriceWei maxRows maxBytes maxContentBytes "
        "maxAttempts concurrency broadcast",
    )
    ensure(config["broadcast"] is True, "Explicit validator broadcast configuration")
    schema, catalog, abi = readProtocol()
    facts = strictJson(Path(config["manifestPath"]).read_bytes())
    recordCheck(facts, "DeploymentManifest", schema)
    ensure(config["rpcUrl"] in facts["rpcUrls"], "Unqualified validator RPC")
    evidenceBytes = Path(config["registryVerificationPath"]).read_bytes()
    ensure(
        contentDigest(evidenceBytes) == facts["registryVerification"]["digest"],
        "Registry qualification digest",
    )
    verification = strictJson(evidenceBytes)["feedbackVerification"]
    validatorRaw = Path(config["validatorVerificationPath"]).read_bytes()
    ensure(
        contentDigest(validatorRaw) == facts["validatorVerification"]["digest"],
        "Validator qualification digest",
    )
    from modules.validation.qualification import verifyValidatorEvidence

    verifyValidatorEvidence(strictJson(validatorRaw), facts, config["image"])
    validator, relayer = (
        readKeystore(config["validatorKeystore"]),
        readKeystore(config["relayerKeystore"]),
    )
    Path(config["database"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    journal = Journal(
        config["database"],
        {
            "mode": "monad",
            "role": "validator",
            "market": facts["market"],
            "validator": facts["validator"],
        },
        maxContentBytes=config["maxContentBytes"],
    )
    os.chmod(config["database"], 0o600)
    rpc = Web3Rpc(config["rpcUrl"])
    http = createHttpClient(NetworkPolicy())
    uploader, runtime = None, None
    try:
        codec = WireCodec(schema, catalog, abi)
        chain = ChainConnection(
            rpc,
            facts,
            config["genesisHash"],
            journal,
            codec,
            strictJson((ROOT / "specs/contracts.read.abi.json").read_bytes()),
            config["maxFinalizedAgeSeconds"],
        )
        market = MonadMarket(
            chain, relayer, maxGas=config["maxGas"], maxGasPriceWei=int(config["maxGasPriceWei"])
        )
        store = ValidationStore(journal, maxRows=config["maxRows"], maxBytes=config["maxBytes"])
        publisher = FeedbackAdapter(
            market,
            facts["feedbackPublisher"],
            strictJson((ROOT / "specs/feedback-publisher.abi.json").read_bytes()),
            strictJson((ROOT / "specs/registry.reputation.abi.json").read_bytes()),
            verification,
        )
        ensure(
            verification["reputationRegistry"] == facts["reputationRegistry"],
            "Manifest reputation binding",
        )
        hashes = {x["address"]: x["codeHash"] for x in verification["codeObservations"]}
        ensure(
            hashes[facts["feedbackPublisher"]] == facts["publisherCodeHash"]
            and hashes[facts["reputationRegistry"]] == facts["reputationCodeHash"],
            "Manifest feedback code binding",
        )
        headerName = config["kuboAuthorizationEnv"]
        authorization = os.environ.get(headerName) if headerName else None
        ensure(not headerName or authorization, "Missing Kubo authorization", "UNAVAILABLE")
        uploader = KuboPublisher(
            config["kuboRpcUrl"],
            store,
            headers={"Authorization": authorization} if authorization else None,
        )
        content = ContentStore(http, NetworkPolicy(), journal, ipfsGateway=config["ipfsGateway"])
        docker = DockerExecutor(namespace="validator:" + facts["market"])
        runtime = ValidatorRuntime(
            market,
            MarketWatcher(chain),
            store,
            content,
            uploader,
            docker,
            validator,
            strictJson((ROOT / "specs/signing/types.json").read_bytes()),
            config["image"],
            publisher=publisher,
            maxAttempts=config["maxAttempts"],
            concurrency=config["concurrency"],
        )
        if retry:
            kind, taskId = retry
            await runtime.grantRetry(
                {"chainId": facts["chainId"], "market": facts["market"], "taskId": taskId}, kind
            )
            return
        await docker.preflight(runtime.profile)
        LOG.info(
            "event=validator_started chain_id=%s market=%s sender=%s concurrency=%s",
            facts["chainId"],
            facts["market"],
            market.signer,
            config["concurrency"],
        )
        while True:
            try:
                await runtime.tick()
            except AdapterError as error:
                if error.kind in {"FINALITY_CONFLICT", "INVALID_DATA", "CONFLICT"}:
                    raise
                LOG.warning("event=validator_observation_paused error_kind=%s", error.kind)
            await asyncio.sleep(2)
    finally:
        if runtime:
            await runtime.close()
        if uploader:
            await uploader.close()
        await http.aclose()
        await rpc.close()
        journal.close()
        LOG.info("event=validator_stopped")


def inspect(config, taskId=None, digest=None):
    """Read only public artifacts/outcomes from WAL; never open another role's writer."""
    with sqlite3.connect(
        "file:" + str(Path(config["database"]).resolve()) + "?mode=ro", uri=True
    ) as db:
        settings = strictJson(db.execute("SELECT body FROM settings WHERE id=1").fetchone()[0])
        ensure(settings.get("role") == "validator", "Diagnostic requires validator journal")
        if digest:
            ensure(len(digest) == 66 and digest.startswith("0x"), "Content digest")
            row = db.execute("SELECT raw FROM content WHERE digest=?", (digest,)).fetchone()
            ensure(
                row is not None and contentDigest(row[0]) == digest, "Retained content unavailable"
            )
            return row[0]
        rows = [
            strictJson(raw)
            for (raw,) in db.execute(
                "SELECT body FROM validation_records WHERE kind IN ('job','receipt','export')"
            )
        ]
        selected = []
        for row in rows:
            ref = row.get("taskRef") or row.get("task", row.get("view", {}).get("task", {})).get(
                "taskRef"
            )
            if ref and ref["taskId"] == taskId:
                selected.append(row)
        return jsonBytes(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("AGENTLANCE_VALIDATOR_CONFIG"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--outcome", help="Read retained public job/receipt/export JSON by task ID")
    group.add_argument("--content", help="Read exact retained public artifact bytes by digest")
    group.add_argument("--retry-export", help="Stop the writer first; grant three export attempts")
    group.add_argument(
        "--retry-validation", help="Stop the writer first; grant three validation attempts"
    )
    args = parser.parse_args()
    if not args.config:
        parser.error("--config or AGENTLANCE_VALIDATOR_CONFIG is required")
    config = strictJson(Path(args.config).read_bytes())
    if args.outcome or args.content:
        import sys

        sys.stdout.buffer.write(inspect(config, args.outcome, args.content))
    else:
        configureLogging()
        retry = (
            ("export", args.retry_export)
            if args.retry_export
            else ("job", args.retry_validation)
            if args.retry_validation
            else None
        )
        asyncio.run(run(config, retry))


if __name__ == "__main__":
    main()
