"""Read-only chain probes plus Kubo evidence publication; never deploys or funds roles."""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from layer3_deployment import validateDeploymentReport  # noqa: E402
from layer3_tools import toolPath, verifyExecutionProfile, verifyToolchain  # noqa: E402
from layer7_tools import buildImage, setupLayer7  # noqa: E402

from apps.reference_agent.chain import readKeystore  # noqa: E402
from apps.validator.main import readProtocol  # noqa: E402
from modules.adapters.a2a.profile import jsonBytes, strictJson  # noqa: E402
from modules.adapters.chain.codec import WireCodec, callData  # noqa: E402
from modules.adapters.chain.market import MonadMarket  # noqa: E402
from modules.adapters.chain.rpc import ChainConnection, RpcError, Web3Rpc  # noqa: E402
from modules.adapters.execution.docker import DockerExecutor  # noqa: E402
from modules.adapters.registry.chain import REFERENCE_REVISION, MonadRegistry  # noqa: E402
from modules.adapters.registry.feedback import (  # noqa: E402
    IMPLEMENTATION_SLOT,
    OWNER_SLOT,
    FeedbackAdapter,
)
from modules.adapters.storage.content import (  # noqa: E402
    ContentStore,
    NetworkPolicy,
    createHttpClient,
)
from modules.adapters.storage.ipfs import KuboPublisher  # noqa: E402
from modules.adapters.storage.journal import Journal  # noqa: E402
from modules.adapters.storage.validation import ValidationStore  # noqa: E402
from modules.agent_client.ports import ensure, recordCheck  # noqa: E402
from modules.agent_client.signing import contentDigest  # noqa: E402
from modules.economics.records import recordDigest  # noqa: E402
from modules.validation.qualification import (  # noqa: E402
    buildValidatorEvidence,
    verifyCompiledRuntime,
    verifyValidatorEvidence,
)
from modules.validation.runtime import evaluatorProfile  # noqa: E402


def read(name):
    return strictJson(Path(name).read_bytes())


async def qualify(config, destination):
    verifyToolchain()
    setupLayer7()
    ensure(config["image"] == buildImage(), "Evaluator image differs from reviewed build")
    verifyExecutionProfile()
    subprocess.run([toolPath("forge"), "build", "--force", "--offline"], cwd=ROOT, check=True)
    schema, catalog, abi = readProtocol()
    lower = validateDeploymentReport(read(config["layer3ReportPath"]))
    facts = lower["manifestFacts"].copy()
    ensure(
        facts["network"] == "monad-testnet"
        and facts["chainId"] == "10143"
        and config["rpcUrl"] in facts["rpcUrls"],
        "Only previously qualified Monad testnet",
    )
    identity = read(config["identityVerificationPath"])["identityVerification"]
    ensure(
        identity["identityRegistry"] == facts["identityRegistry"]
        and identity["identityVersion"] == facts["identityVersion"],
        "Identity qualification binding",
    )
    destination.mkdir(parents=True, exist_ok=False)
    journal = Journal(
        destination / "qualification.sqlite",
        {"mode": "monad", "role": "qualification", "market": facts["market"]},
    )
    rpc, http = Web3Rpc(config["rpcUrl"]), createHttpClient(NetworkPolicy())
    token = (
        os.environ.get(config["kuboAuthorizationEnv"]) if config["kuboAuthorizationEnv"] else None
    )
    ensure(not config["kuboAuthorizationEnv"] or token, "Kubo authorization missing")
    uploader = KuboPublisher(
        config["kuboRpcUrl"],
        ValidationStore(journal),
        headers={"Authorization": token} if token else None,
    )
    try:
        chain = ChainConnection(
            rpc,
            facts,
            config["genesisHash"],
            journal,
            WireCodec(schema, catalog, abi),
            read(ROOT / "specs/contracts.read.abi.json"),
            60,
        )
        stamp = await chain.qualify()
        registry = MonadRegistry(chain, read(ROOT / "specs/registry.identity.abi.json"), identity)
        await registry.verifyDependencies(stamp)
        content = ContentStore(http, NetworkPolicy(), ipfsGateway=config["ipfsGateway"])
        # Retain/retrieve the original L3 RPC evidence; a schema-valid report is insufficient.
        await content.fetchBytes(
            lower["rpcVerification"]["uri"], 1048576, lower["rpcVerification"]["digest"]
        )
        publisherAbi, reputationAbi = (
            read(ROOT / "specs/feedback-publisher.abi.json"),
            read(ROOT / "specs/registry.reputation.abi.json"),
        )
        publisher, reputation = config["feedbackPublisher"], config["reputationRegistry"]
        code, storage, proxies = [], [], []
        for address, contract, proxy in [
            (facts["identityRegistry"], "IdentityRegistryUpgradeable", True),
            (reputation, "ReputationRegistryUpgradeable", True),
            (publisher, "FeedbackPublisher", False),
        ]:
            recordCheck(address, "Address", schema)
            raw = await rpc.call("eth_getCode", address, hex(int(stamp["blockNumber"])))
            code.append({"address": address, "codeHash": contentDigest(bytes.fromhex(raw[2:]))})
            targetRaw = raw
            if proxy:
                for slot in (IMPLEMENTATION_SLOT, OWNER_SLOT):
                    value = await rpc.call(
                        "eth_getStorageAt", address, slot, hex(int(stamp["blockNumber"]))
                    )
                    storage.append({"address": address, "slot": slot, "value": value})
                implementation = "0x" + storage[-2]["value"][-40:]
                ensure(
                    int(implementation, 16) != 0 and int(storage[-1]["value"], 16) != 0,
                    "Unqualified UUPS implementation/governance",
                )
                proxies.append(
                    {"address": address, "implementation": implementation, "kind": "uups"}
                )
                targetRaw = await rpc.call(
                    "eth_getCode", implementation, hex(int(stamp["blockNumber"]))
                )
                code.append(
                    {
                        "address": implementation,
                        "codeHash": contentDigest(bytes.fromhex(targetRaw[2:])),
                    }
                )
                verifyCompiledRuntime(
                    raw, read(ROOT / ".scratch/layer3/out/ERC1967Proxy.sol/ERC1967Proxy.json")
                )
            verifyCompiledRuntime(
                targetRaw, read(ROOT / f".scratch/layer3/out/{contract}.sol/{contract}.json")
            )
        feedback = {
            "sourceRevision": REFERENCE_REVISION,
            "market": facts["market"],
            "identityRegistry": facts["identityRegistry"],
            "reputationRegistry": reputation,
            "publisher": publisher,
            "publisherAbiDigest": recordDigest(publisherAbi),
            "reputationAbiDigest": recordDigest(reputationAbi),
            "validUntil": config["validUntil"],
            "deploymentBlock": config["publisherDeploymentBlock"],
            "deploymentBlockHash": (await chain.block(config["publisherDeploymentBlock"]))[
                "blockHash"
            ],
            "proxies": proxies,
            "codeObservations": code,
            "storageObservations": storage,
        }
        adapter = FeedbackAdapter(
            MonadMarket(chain), publisher, publisherAbi, reputationAbi, feedback
        )
        await adapter.verifyDependencies(stamp)
        agentRef = {
            "chainId": facts["chainId"],
            "identityRegistry": facts["identityRegistry"],
            "agentId": config["probeAgentId"],
        }
        owner = (await registry.readIdentity(agentRef))["owner"]
        entry = adapter.registry["giveFeedback"]
        data = callData(
            entry,
            [
                int(config["probeAgentId"]),
                1,
                0,
                "agentlance-v1",
                "structured-output-v1",
                "",
                "",
                bytes(32),
            ],
        )
        await rpc.call(
            "eth_call",
            {"from": publisher, "to": reputation, "data": data},
            hex(int(stamp["blockNumber"])),
        )
        try:
            await rpc.call(
                "eth_call",
                {"from": owner, "to": reputation, "data": data},
                hex(int(stamp["blockNumber"])),
            )
        except RpcError as error:
            ensure(
                isinstance(error.error, dict) and error.error.get("data"),
                "Self-feedback probe lacks revert evidence",
            )
        else:
            raise ValueError("Owner feedback unexpectedly accepted")
        validator, relayer = (
            readKeystore(config["validatorKeystore"]),
            readKeystore(config["relayerKeystore"]),
        )
        ensure(validator.address.lower() == facts["validator"], "Validator key namespace")
        recordCheck(config["minimumRelayerBalanceAtoms"], "Uint256", schema)
        ensure(int(config["minimumRelayerBalanceAtoms"]) > 0, "Positive relayer funding required")
        ensure(
            int(await rpc.call("eth_getBalance", relayer.address.lower(), "finalized"), 16)
            >= int(config["minimumRelayerBalanceAtoms"]),
            "Relayer funding insufficient",
        )
        await DockerExecutor().preflight(evaluatorProfile(config["image"]))
        now = int(time.time())
        ensure(
            now < int(config["validUntil"]) <= now + 86400, "Qualification validity at most one day"
        )
        validatorEvidence = buildValidatorEvidence(
            validator, facts, config["image"], config["validUntil"]
        )
        verifyValidatorEvidence(validatorEvidence, facts, config["image"])
        registryEvidence = {"identityVerification": identity, "feedbackVerification": feedback}
        refs = {}
        for name, value in [("registry", registryEvidence), ("validator", validatorEvidence)]:
            raw = jsonBytes(value)
            ref = await uploader.publishEvidence(raw, name + ":" + contentDigest(raw))
            await content.fetchBytes(ref["uri"], 1048576, ref["digest"])
            (destination / (name + "-verification.json")).write_bytes(raw)
            refs[name] = ref
        hashes = {item["address"]: item["codeHash"] for item in code}
        manifest = facts | {
            "reputationRegistry": reputation,
            "reputationCodeHash": hashes[reputation],
            "reputationVersion": REFERENCE_REVISION,
            "feedbackPublisher": publisher,
            "publisherCodeHash": hashes[publisher],
            "rpcVerification": lower["rpcVerification"],
            "registryVerification": refs["registry"],
            "validatorVerification": refs["validator"],
        }
        recordCheck(manifest, "DeploymentManifest", schema)
        await chain.checkCanonical(stamp)
        (destination / "manifest.json").write_bytes(jsonBytes(manifest))
        return manifest
    finally:
        await uploader.close()
        await http.aclose()
        await rpc.close()
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New ignored/private directory")
    args = parser.parse_args()
    asyncio.run(qualify(read(args.config), args.output))
    print("Qualified manifest saved. Proxy governance and validator availability remain trusted.")


if __name__ == "__main__":
    main()
