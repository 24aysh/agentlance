# Layer 7 operations

Layer 7 evaluates committed structured JSON, publishes exact evidence through Kubo, signs the existing EIP-712 verdict and relays it to the unchanged market. A separate immutable `FeedbackPublisher` exports each eligible canonical receipt once to the pinned ERC-8004 reputation implementation. The agent process joins its own receipts with the selected Layer 5 usage report. No model key is needed for validation.

## Local setup and acceptance

```sh
make setup
make setup-l3
make setup-l6
make setup-l7
make check-l7
make demo-l7
```

Docker must be running. `setup-l7` verifies the checksummed upstream sources in `contracts/layer7.lock.json`, pulls Kubo v0.43.1 by digest and builds the evaluator. Its context contains only the evaluator, entry point, domain record validator, canonical schema and a hash-locked subset of `uv.lock`. The immutable Docker image ID is saved in `.scratch/layer7/image-id`. OpenZeppelin upgradeable 5.4.0 is needed to test and qualify the pinned real identity/reputation implementations; production publisher code uses the existing non-upgradeable dependency.

`check-l7` runs `check-l6` and all its inherited gates, real pinned-registry Solidity tests, evaluator/golden/property tests, native transaction and journal recovery, real Docker resource enforcement, real Kubo add/readback/restart, private reconciliation and the process demo. It fails on missing tools or skipped required tests. Reports live in ignored `.scratch/layer7/`; they are not public deployment manifests.

The demo uses a local Monad EVM and separate validator/worker processes. Artifact HTTPS and model boundaries are isolated fixtures, the SDK/container/market/publisher/reputation implementation/Kubo boundaries are real. Worker fixture identities share test control; distinct addresses/processes do not prove independent operators. No live RPC, paid inference or public IPFS service is needed.

## Roles and configuration

Run the validator in a separate process and database. Its validator key must equal the market's immutable validator. Its transaction sender may use the same EOA or a separate explicitly funded EOA. The validator's journal binds one sender; never share it with a worker, copy an active nonce queue, or redeploy a publisher to recover an export. All market and publisher writes in a journal use the same native sender and nonce lock.

Use encrypted keystores, with passwords supplied only through the named environment variables in `.env.example`. Nothing passes a key, host mount or provider credential into the evaluator. Save this operator configuration under an ignored directory, replacing the angle-bracket values with qualified facts:

```json
{
  "manifestPath": ".scratch/deployment/manifest.json",
  "registryVerificationPath": ".scratch/deployment/registry-verification.json",
  "validatorVerificationPath": ".scratch/deployment/validator-verification.json",
  "genesisHash": "<qualified genesis block hash>",
  "rpcUrl": "<qualified HTTPS RPC from manifest>",
  "database": ".scratch/validator/state.sqlite",
  "validatorKeystore": {"path": ".scratch/keys/validator.json", "passwordEnv": "AGENTLANCE_VALIDATOR_KEYSTORE_PASSWORD"},
  "relayerKeystore": {"path": ".scratch/keys/relayer.json", "passwordEnv": "AGENTLANCE_RELAYER_KEYSTORE_PASSWORD"},
  "image": "<sha256 image ID from setup-l7>",
  "kuboRpcUrl": "http://127.0.0.1:5001",
  "kuboAuthorizationEnv": null,
  "ipfsGateway": "<public HTTPS gateway origin>",
  "maxFinalizedAgeSeconds": 60,
  "maxGas": 2000000,
  "maxGasPriceWei": "100000000000",
  "maxRows": 10000,
  "maxBytes": 33554432,
  "maxContentBytes": 67108864,
  "maxAttempts": 3,
  "concurrency": 1,
  "broadcast": true
}
```

```sh
uv run --locked --env-file .env python -m apps.validator.main
uv run --locked python -m apps.validator.main --config .scratch/validator/config.json --outcome 1
uv run --locked python -m apps.validator.main --config .scratch/validator/config.json --content 0x<artifact-digest>
```

The latter two commands read public retained artifacts and validation/receipt/export records through SQLite's read-only WAL connection, including the saved exportable attestation. They do not expose worker cost history. Library consumers can use `OutcomeObserver.readOutcome(taskRef)`. Signed evidence binds the digest, not its locator; the canonical receipt's URI is retained if another relayer supplied a different valid URI.

Kubo RPC is a trusted administrative endpoint, not an artifact URL. Only explicitly configured loopback HTTP or HTTPS is accepted. Keep it private; set `kuboAuthorizationEnv` to the environment variable containing the complete Authorization header when needed. The upload adapter fixes CIDv1, raw leaves, SHA-256, 262144-byte chunks and pinning. AgentLance separately hashes exact evidence bytes with Keccak. Before signing, both RPC cat and the configured safe artifact gateway must return the original bytes. Public task retrieval keeps the existing DNS/address, redirect, size and timeout controls.

## Recovery and limits

Jobs persist fetched content, evaluator profile, attempt counters, cumulative CPU reservations, evidence, typed payload, nonce, signature and native operation before the corresponding external effect. A pure interrupted evaluator can rerun only after its named container stops, within its retained allowance. Completed evidence is never reevaluated. The evaluator has no network, mounts or secrets, a read-only root, non-root UID, 128 MiB memory, 5 seconds cumulative process CPU and 10 seconds wall time. The reviewed worker does not spawn children. The separate 8 MiB framing ceiling does not raise Layer 6 limits.

Missing or unverified artifacts, malformed prerequisites, operational failures or transport overflow produce no verdict. A complete hash-verified result between 1 MiB and 2 MiB can produce `RESULT_LIMIT`; an incomplete larger response cannot. Unsupported work eventually follows the existing validation timeout. The validator observes submitted tasks and can relay their expiry; root allocation and other root keepers remain separately operated.

Automatic stage attempts are capped at three, with at least two seconds between attempts; restart does not refill them. Native rebroadcast reuses the same signed bytes. A finalized rejected publisher transaction may use a new operation within the delivery budget; unknown transactions retain their original operation and block a fresh nonce. Per-operation gas and gas-price caps plus remaining delivery attempts bound export exposure; the frozen journal binding retains those caps. Qualification outages leave feedback pending without changing settlement.

After stopping the writer, an operator can explicitly grant three more attempts to one retained record:

```sh
uv run --locked --env-file .env python -m apps.validator.main --retry-export 1
uv run --locked --env-file .env python -m apps.validator.main --retry-validation 1
```

These commands retain attempt history, signed bytes and attestations. Validation grants require a still-submitted, unexpired task and reserve retention capacity; they cannot change a decision. Resume the normal application afterward. A finality conflict is a persistent halt: investigate the RPC/history and preserve the database instead of clearing flags. Storage exhaustion preserves old records and stops admission; there is no automatic eviction or perpetual pinning guarantee.

## Private reconciliation

`DiscoveryRuntime` composes `Reconciler` when economics is enabled, using the same agent journal and selected reports. An independent finalized-log cursor atomically retains each receipt and advances progress. `readReconciliation(executionRef)` returns the active immutable version, including actual credited payout/refund, direct-child payments (unknown until all are discovered and settled), own reported execution cost, saved-rate forecast comparison and missing reasons. Receipt-before-usage and usage-before-receipt both converge. A late child settlement can refresh a failed parent's accounting.

Corrections go through `ExecutionHistory.recordUsage(report, context, supersedesReportId)`: unchanged expense IDs retain identical contents; changed expenses need new IDs. Do not edit the Layer 6 outbox or add replacement reports to their predecessors. COMPLETE still describes own execution inventory. Whole-agent profit, validator cost, gas, withdrawals and system utility are not inferred from these narrower reports. L1's full-scope analytics remain available to callers that actually possess complete attributed subtree inputs.

## Live qualification

No public deployment is selected by this implementation. A complete canonical `DeploymentManifest`, qualified registries, the single canonical publisher, funded roles and accessible evidence are required to run against Monad testnet. External UUPS governance and the immutable validator remain trusted.

`uv run --locked python scripts/qualify_layer7.py CONFIG --output NEW_DIRECTORY` extends the existing L3/L4 qualification workflow. It never deploys, transfers funds or changes registry state. It reads the finalized chain, compares deployed publisher/proxy/implementation code to locally compiled pinned artifacts, retains UUPS implementation and owner storage observations, verifies identity binding and publisher configuration, simulates allowed and owner-rejected feedback, checks relayer funding, checks the pinned evaluator image and signs a domain-specific key-control challenge. It publishes and retrieves the qualification evidence through Kubo and the safe gateway, then writes a complete schema-validated manifest. Incomplete checks leave no manifest.

The qualification JSON supplies `layer3ReportPath`, `identityVerificationPath`, `rpcUrl`, `genesisHash`, `feedbackPublisher`, `reputationRegistry`, `publisherDeploymentBlock`, `probeAgentId` (an existing live identity), `validUntil` (at most 24 hours ahead), `validatorKeystore`, `relayerKeystore`, `minimumRelayerBalanceAtoms` (a positive decimal string), `image`, `kuboRpcUrl`, `kuboAuthorizationEnv` and `ipfsGateway`. Qualification rebuilds the reviewed evaluator and requires its image ID to match the configured image. The identity verification must already contain the existing L4 transfer/wallet/operator evidence. The tool supports the pinned UUPS deployment pattern; other proxy/governance designs require review. Report paths are operator-owned, and their content references are digest-bound in the manifest.

Unexpected implementation, owner storage, code or qualification expiry suspends registry-dependent admission and exports. Existing market settlement/refunds continue to use their frozen obligations. Off-chain qualification cannot prevent arbitrary third parties from calling a publisher after an external proxy upgrade. Requalification is an explicit operator action; backup the original journal and investigate changes before choosing new evidence. The live facts and operational ownership questions in `specs/layer-7.md` remain open until this workflow is run on the intended deployment.

Deploy the publisher once, after the market and both registries have been chosen and verified. `contracts/script/DeployFeedbackPublisher.s.sol` reads `AGENTLANCE_MARKET`, `AGENTLANCE_IDENTITY_REGISTRY` and `AGENTLANCE_REPUTATION_REGISTRY`; Foundry supplies signing through an operator keystore. Run the pinned Forge script without `--broadcast` for a simulation, then use its normal explicit broadcast workflow when deployment is authorized. Record the resulting address/block for qualification. This repository has not run a public deployment.
