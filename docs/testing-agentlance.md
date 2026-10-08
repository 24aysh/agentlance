# Testing AgentLance: local acceptance, Monad testnet, and live inference

This is the developer runbook for the implemented L0–L8 backend/CLI. Start from the repository root. Commands marked **broadcast** spend testnet MON; commands marked **paid** can incur provider charges. Reading this guide does not deploy anything. Use a dedicated test campaign and retain its evidence.

Local acceptance is the starting point. **A public deployment is not yet qualified by the repository.** Before a first live run, someone must supply and verify the registries, market, publisher, identities, RPC, hosting, prices and funded keys below. There is no single command that provisions all of them. The frontend is outside this testing path.

## 1. Choose the test you are running

| Stage | What it proves | External requirements | Completion evidence |
|---|---|---|---|
| A. Full local acceptance | Protocol, recovery, execution, validation, interoperability and application composition | Docker and downloaded pinned tools; no keys or paid APIs | `make check-l8`, `.scratch/layer8/gate.json` says `passed` |
| B. Deterministic testnet canary | Actual network, identity, task market, HTTPS, validator, feedback and withdrawal | Qualified deployment, testnet MON, public HTTPS/IPFS, encrypted keys | Finalized SUCCESS, verified feedback, actual credit withdrawal |
| C. Economics and delegation | Private pricing/history, public child markets and cost reconciliation | B plus explicit resource prices/limits, separately funded child bidders | Parent/child receipts, conservation, selected cost disclosure |
| D. Live providers | Real Jev forecasts and/or Agents SDK model execution | B/C plus separately bounded provider credentials and pricing | Retained model/version/usage, decision/execution records and provider billing comparison |

Complete B with Jev and model execution disabled before adding providers. A failure in D should not leave uncertainty about whether chain/TLS/identity setup works. The independent Node example in `examples/external-agent/` is **LOCAL_FIXTURE only**; use the Python reference runtime or a separately qualified external agent on testnet. Envio and award hints are optional; use direct RPC discovery first.

## 2. Prepare the workstation and retain the baseline

Use Python 3.12.12, Node 22.15+, pnpm 11.19.0, `uv`, Git, Docker and OpenSSL. The pinned toolchain supports the platforms checked by [layer3_tools.py](../scripts/layer3_tools.py). Match those platforms rather than substituting an arbitrary global compiler. Docker must support the isolation/CPU/PID limits exercised by the tests.

```sh
git status --short
git rev-parse HEAD
make setup
make setup-l3
make setup-l6
make setup-l7
make check-l8
```

Run gates serially; `check-l8` already includes every lower gate and the L8 demo. To rerun just the complete application demonstration, use `make demo-l8`. An isolated demo is never a public deployment manifest. Check `.scratch/layer8/{gate.json,pytest.log,node-tests.log,demo.log}` and the inherited `.scratch/layer7/gate.json`; a zero test count, skipped required case or missing infrastructure is not a pass.

Prepare private working directories, preserving an existing `.env`:

```sh
umask 077
mkdir -p .scratch/live/keys .scratch/live/evidence .scratch/live/logs \
  .scratch/live/bundle .scratch/live/public .scratch/live/requester \
  .scratch/live/agent .scratch/live/validator .scratch/live/payout
test -e .env || cp .env.example .env
chmod 600 .env
git rev-parse HEAD > .scratch/live/evidence/source-commit.txt
git diff --binary > .scratch/live/evidence/working-tree.patch
git status --short > .scratch/live/evidence/working-tree-status.txt
```

Retain any untracked source files separately; `git diff` does not include them. Record the execution/evaluator image IDs from `.scratch/layer6/image-id` and `.scratch/layer7/image-id`, lockfiles and gate reports. Run from this checkout throughout qualification: the L3 driver compares build evidence including source commit, and L7 validator evidence binds evaluator source bytes.

## 3. Inventory accounts, services and budgets

Do not reuse Anvil fixture keys. Import/generate encrypted Ethereum keystores with your wallet tooling. A configured key file uses `{ "path": "...", "passwordEnv": "..." }`; passwords are environment values, never JSON fields. Load `.env` explicitly with `uv run --locked --env-file .env ...`.

| Role | Authority/funding | Process/database |
|---|---|---|
| Deployer | Deploys market/publisher after review; gas | Foundry encrypted account; deployment evidence |
| Requester / root progress caller | Root budget plus create/allocate/expire/cancel gas | L8 CLI, requester application database |
| Agent identity owner | Controls ERC-8004 NFT; signs bid permits; registration/maintenance gas | Owner keystore; no second active transaction queue for the execution signer |
| Agent execution signer | Bid/accept/result gas; **fresh child deposits** and child progress gas if delegating | One reference process and journal per signer |
| Agent payout / refund owner | Receives settlement credit; needs gas and control to withdraw | Separate L8 application config for each credited address |
| Validator | Immutable market-bound EOA signs objective verdicts; keep distinct from participant roles | Validator signing keystore |
| Validator relayer | Pays verdict, expiry and feedback-publication gas | One validator process/journal/native nonce queue |
| Registry governance | External proxy implementation/admin authority | Explicitly documented trust assumption, not an AgentLance role |

For the simplest canary, keep the identity owner as its verified payout wallet and use a separate execution signer. The pinned registry initializes a newly registered identity's wallet to its owner. Verify that fact on the selected deployment. Changing payout requires the registry's wallet-control proof; merely writing a payout into AgentLance config cannot change it. Keep requester, execution signer and validator relayer distinct. The strict L3 scenario driver has additional distinct-role requirements in [its guide](layer-3-testnet.md).

One sender must have one nonce queue. Do not use an active worker/validator sender in a second CLI database or send unrelated wallet transactions from it. Prefer payout owners that do not send runtime transactions. A journal is bound to its sender/chain; changing a key is not a recovery procedure.

Budget root escrow, every independently funded child escrow and gas separately. Credit is not spendable wallet balance until withdrawn. `budgetAtoms`/bids are uint96; aggregate credits/withdrawals are uint256. All are decimal **MON atoms**, with 18 decimals. Provider spend has its own currency/unit/conversion and allowance. Choose explicit caps for the campaign; the fixture's tiny atom amounts and synthetic zero gas costs are not live economics.

Required infrastructure:

- An RPC with reliable `finalized` blocks, genesis, historical `eth_call`/code/storage, log ranges, receipts, simulation, estimation and transaction submission. `eth_chainId` alone is insufficient.
- Public HTTPS for each agent's card, A2A endpoint and retained result artifacts; public HTTPS or IPFS for requester inputs/schema/policy and registry metadata. Use trusted TLS certificates. Live content fetching rejects localhost/private destinations, including redirects/DNS rebinding.
- A persistent private Kubo RPC and publicly reachable HTTPS gateway. The validator must retrieve exactly what it publishes before signing. `setup-l7` downloads Kubo's image; it does **not** start a lasting public IPFS service. Provision its storage, RPC access control, public gateway/reverse proxy and retention separately. Use the image digest from `contracts/layer7.lock.json`. Never expose an unauthenticated Kubo admin API publicly.
- A host with enough Docker capacity for workers and validators, synchronized UTC time, durable disks, backup and log rotation.

## 4. Qualify the network and registries before deploying

AgentLance's deployment scripts target Monad testnet **chain 10143**. Select the current endpoint and faucet from [official testnet information](https://docs.monad.xyz/developer-essentials/testnet), then independently query the actual endpoint. The faucet linked there is [faucet.monad.xyz](https://faucet.monad.xyz). Do not use a mainnet endpoint because it appears first in the docs.

**Compatibility checkpoint, checked 2026-10-07:** the official page retrieved for this guide labels testnet `MONAD_NINE` / `v0.15.2`; this repository's Foundry execution profile pins `monad:MonadTen`. This discrepancy is unresolved by the local tests. Establish the actual network revision, relevant code-size/opcode/gas behavior and build compatibility before deployment. Retain that evidence; do not rename a revision in a report or alter the lockfile just to pass a check. The public page also records a testnet genesis reset: chain ID alone cannot identify the intended history. [Official source](https://docs.monad.xyz/developer-essentials/testnet).

Use an operator RPC URL in `AGENTLANCE_TESTNET_RPC` for the examples below; this is a guide/deployment input, not an implicit runtime override. Avoid publishing provider-secret URLs. A minimal **read-only** probe, not a qualification substitute:

```sh
uv run --locked --env-file .env python - <<'PY'
import os
from modules.adapters.chain.rpc import Web3Rpc
import asyncio

async def probe():
    rpc = Web3Rpc(os.environ['AGENTLANCE_TESTNET_RPC'])
    try:
        assert int(await rpc.call('eth_chainId'), 16) == 10143
        genesis = await rpc.call('eth_getBlockByNumber', '0x0', False)
        head = await rpc.call('eth_getBlockByNumber', 'finalized', False)
        assert genesis and head
        print({'genesisHash': genesis['hash'], 'finalizedNumber': head['number'],
               'finalizedHash': head['hash'], 'finalizedTimestamp': head['timestamp'],
               'clientVersion': await rpc.call('web3_clientVersion')})
    finally:
        await rpc.close()
asyncio.run(probe())
PY
```

Compare timestamps with UTC and repeat at least two seconds apart to establish advancement. Probe historical reads and log coverage at the registry and market deployment blocks, not just the head. The optional L3 `malicious` scenario also needs mined revert tracing (`debug_traceTransaction` / `callTracer`); not every provider supports it.

Select actual identity and reputation deployments matching **`b9e466c250744a7e06b13dff9d3c2844ed64f825`**, the revision pinned in `contracts/layer7.lock.json`. Current public ERC-8004 branding or an explorer's verified source label does not establish compatibility. The qualifier compares compiled implementation and ERC1967 proxy runtime bytes, UUPS implementation slots, owner storage and identity/reputation relationships. A different deployment implementation requires compatibility work before this runbook can proceed.

Retain identity qualification under `.scratch/live/evidence/identity-verification.json` with a top-level `identityVerification` object. Its fields and probes are specified in [Layer 4's registry section](layer-4.md#registry-pin-and-verification-record): identity/version, source revision, deployment block/hash, validity, proxy kind, code/storage observations and backed probes for transfer clearing/restoration, contract owners and registration discovery. Exercise destructive transfer probes only on dedicated controlled probe identities. AgentLance has no registry deployment/onboarding/identity-qualification CLI; this is an explicit operator task using the pinned upstream implementation, not a reason to substitute the local registry double.

## 5. Deploy the market and publisher, then assemble evidence

Skip deployments only when reusing an already reviewed deployment with matching immutable validator/registries and retained transaction/build evidence. Decide the validator address **before** deploying the market; it cannot be rotated for existing tasks.

The following are **broadcast** commands, to run only after §4. Replace account names with your encrypted Foundry accounts and set all addresses/hashes from actual qualification. Never pass a raw private key on the command line.

```sh
# Environment read by DeployMarket:
# AGENTLANCE_L3_IDENTITY_REGISTRY, AGENTLANCE_L3_IDENTITY_CODE_HASH,
# AGENTLANCE_L3_VALIDATOR, AGENTLANCE_TESTNET_RPC
.scratch/layer3/toolchain/bin/forge script \
  contracts/script/DeployMarket.s.sol:DeployMarket \
  --rpc-url "$AGENTLANCE_TESTNET_RPC" --account agentlance-deployer --broadcast

# Environment read by DeployFeedbackPublisher:
# AGENTLANCE_MARKET, AGENTLANCE_IDENTITY_REGISTRY, AGENTLANCE_REPUTATION_REGISTRY
.scratch/layer3/toolchain/bin/forge script \
  contracts/script/DeployFeedbackPublisher.s.sol:DeployFeedbackPublisher \
  --rpc-url "$AGENTLANCE_TESTNET_RPC" --account agentlance-deployer --broadcast
```

Export the required deployment values through your chosen secret/environment manager first, including `AGENTLANCE_TESTNET_RPC` in the invoking shell: the shell expands that argument before Foundry starts. `.env.example` does not supply qualified deployment values. Preserve the deployment transaction/input/constructor arguments, successful receipt, finalized block/hash, runtime hashes and `readPolicy`/publisher getter results. Deploy one canonical publisher for this market; redeploying it is not a feedback-retry mechanism.

An operator must assemble an actual [L3 verification report](../specs/schemas/layer-3-verification.schema.json). There is no command here that automatically discovers and proves every field:

| Report section | Required work |
|---|---|
| `manifestFacts` | Actual chain/market/identity/validator, deployment/runtime hashes, native asset, qualified RPCs and frozen policy limits; exact closed schema |
| `genesisHash`, `deploymentTxHash`, `executionRevision` | Query and retain actual history/revision; establish §4 compatibility |
| `build` | Recompile pinned sources/settings; use `buildEvidence` in `scripts/layer3_deployment.py`; compare deployed creation/runtime bytes including immutable constructor bindings |
| `identityVerification`, `rpcVerification`, `validatorVerification`, `scenarioEvidence` | Publish/retain actual evidence bytes and their URI/Keccak digest; no invented ContentRefs or probe booleans |
| `pendingManifestFields` | Preserve the schema's exact six-field L7 list for the L3 input to `qualify_layer7.py`; do not label a partial report a complete manifest |

`scenarioEvidence` is retained deployment-qualification evidence; it is not silently generated by this structure checker. The independent qualification work must supply it. The later scenario driver is additional evidence and consumes an already qualified report.

```sh
uv run --locked python scripts/layer3_deployment.py \
  .scratch/live/evidence/layer-3-verification.json
```

This validates **structure only**. [The L3 testnet guide](layer-3-testnet.md) specifies exact evidence shapes and the signed scenario driver's stronger bytecode/receipt/dependency checks. Its fixture-attested scenarios are optional protocol probes, not substitutes for a real L7 validator run. In particular, do not run its fixture-verdict scenarios concurrently with an automatic validator on the same tasks. Use its separately documented actor inventory/identities; no driver `--resume` or live `all` mode exists.

### Produce the full manifest

Create `.scratch/live/qualification.json` with all fields below. Angle brackets require actual values; this is a shape, not a valid deployment:

```json
{
  "layer3ReportPath": ".scratch/live/evidence/layer-3-verification.json",
  "identityVerificationPath": ".scratch/live/evidence/identity-verification.json",
  "rpcUrl": "<qualified RPC>",
  "genesisHash": "<actual genesis digest>",
  "feedbackPublisher": "<canonical lowercase publisher address>",
  "reputationRegistry": "<qualified lowercase reputation proxy>",
  "publisherDeploymentBlock": "<decimal block number>",
  "probeAgentId": "<controlled existing identity ID>",
  "validatorKeystore": {"path": ".scratch/live/keys/validator.json", "passwordEnv": "AGENTLANCE_VALIDATOR_KEYSTORE_PASSWORD"},
  "relayerKeystore": {"path": ".scratch/live/keys/relayer.json", "passwordEnv": "AGENTLANCE_RELAYER_KEYSTORE_PASSWORD"},
  "minimumRelayerBalanceAtoms": "<positive explicit funding floor>",
  "image": "<immutable evaluator sha256 ID>",
  "kuboRpcUrl": "http://127.0.0.1:5001",
  "kuboAuthorizationEnv": null,
  "ipfsGateway": "<public HTTPS gateway origin>",
  "validUntil": "<Unix seconds, future and at most 24 hours away>"
}
```

```sh
uv run --locked --env-file .env python scripts/qualify_layer7.py \
  .scratch/live/qualification.json --output .scratch/live/qualified-1
```

The output directory **must not exist**. The qualifier rebuilds/verifies sources and image, reads finalized dependencies, simulates publisher feedback and owner rejection, checks relayer funding, signs a validator key-control statement, publishes registry/validator evidence to Kubo and verifies gateway readback. It does not deploy or send chain transactions, but **does publish evidence**. Expect `manifest.json`, `registry-verification.json` and `validator-verification.json`. Keep their exact bytes and any prior qualification outputs. It does not recreate the missing L3 qualification work for you.

Schedule renewal before the earliest evidence expiry. Renew into a new directory, update each process's file paths to the same manifest/evidence set and restart deliberately. Existing validator configuration binds image, publisher and send caps in its journal; renewal cannot silently change those bindings. Changing evaluator source bytes invalidates the old source-bound proof. Rebuild/requalify rather than editing a signed record.

## 6. Register the agent and publish the exact bytes

For a deterministic solo canary, use the reviewed L2 integer-copy input/output-shape/policy:

```sh
cp specs/fixtures/layer-2/input.json .scratch/live/public/input.json
cp specs/fixtures/layer-2/output-schema.json .scratch/live/public/output-schema.json
cp specs/fixtures/layer-2/policy.json .scratch/live/public/policy.json
cp specs/fixtures/layer-2/card.json .scratch/live/bundle/card.json
cp specs/fixtures/layer-2/output-schema.json .scratch/live/bundle/output-schema.json
cp specs/fixtures/layer-2/policy.json .scratch/live/bundle/policy.json
```

Edit the card's name/description and `supportedInterfaces[0].url` to the real `https://<agent-host>/a2a`. Preserve the required `urn:agentlance:a2a:1` extension and HTTP+JSON 1.0 interface. Keep `filter.advertisedSkills` equal to the card's skill IDs. Serve those exact card bytes at `/.well-known/agent-card.json`. Do not rewrite admitted card bytes at the same endpoint while bids/obligations depend on them; retain the old bytes and hosting until they can be retired.

Host requester content separately at public HTTPS URLs (or IPFS) and calculate Keccak-256 over the exact served bytes, not a URL, pretty-printed JSON or SHA-256. If editing the output schema, update the policy's `outputSchemaDigest` before computing its digest. The reference runtime accepts only its reviewed L2 solo and L6 parent template digests; a valid arbitrary schema is not automatically executable by it.

```sh
uv run --locked python - <<'PY'
from pathlib import Path
from modules.agent_client.signing import contentDigest

for name in ('input.json', 'output-schema.json', 'policy.json'):
    print(name, contentDigest((Path('.scratch/live/public') / name).read_bytes()))
PY
```

Use these digests in `TaskTerms` and the schema/policy filter, then confirm that public downloads match the local bytes. The requester validates these bindings before creating a task.

Register with the **qualified** identity registry's `register(string)` using upstream wallet tooling. Obtain the ID from the finalized `Registered` event, then publish a registration document and set its URI using `setAgentURI` as needed. The registration must bind the actual ID:

```json
{
  "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
  "name": "AgentLance testnet canary",
  "description": "Controlled deterministic integer-copy test agent",
  "active": true,
  "services": [{"name": "A2A", "endpoint": "https://<agent-host>/.well-known/agent-card.json", "version": "1.0"}],
  "registrations": [{"agentId": 123, "agentRegistry": "eip155:10143:<identity-registry-address>"}]
}
```

`123` is illustrative, not an identity to use. Registry metadata uses a JSON integer `agentId`; protocol `AgentRef.agentId` is a decimal string. The reference server serves card/A2A/result routes, **not** your registration document or requester input files. Provision their static hosting yourself. Read `ownerOf`, `getAgentWallet` and `tokenURI` after finality; confirm the owner key and payout wallet. Complete wallet-control proof if changing payout. Never use the fixture-only `setWallet`/`setURI` methods on a live registry.

## 7. Configure and start the live processes

Paths below resolve from the process working directory; use absolute paths for supervisors. Set the keystore password variables in [.env.example](../.env.example). Create the database parent directories before starting the validator. Each process needs its own journal; keep backups of the SQLite database and WAL coherently.

### Agent: smallest canary configuration

Save `.scratch/live/agent/config.json`. All angle brackets must be replaced. Fixed bidding with no `economics` or `execution` block uses the deterministic reference transform and makes **no model calls**. It tests B, not L5/L6 resource enforcement; add those in §9.

```json
{
  "manifestPath": ".scratch/live/qualified-1/manifest.json",
  "registryVerificationPath": ".scratch/live/qualified-1/registry-verification.json",
  "genesisHash": "<qualified genesis digest>",
  "rpcUrl": "<RPC present in manifest>",
  "database": ".scratch/live/agent/state.sqlite",
  "agentRef": {"chainId": "10143", "identityRegistry": "<registry>", "agentId": "<ID>"},
  "executionKeystore": {"path": ".scratch/live/keys/execution.json", "passwordEnv": "AGENTLANCE_KEYSTORE_PASSWORD"},
  "ownerKeystore": {"path": ".scratch/live/keys/owner.json", "passwordEnv": "AGENTLANCE_OWNER_KEYSTORE_PASSWORD"},
  "bundleDir": ".scratch/live/bundle",
  "artifactOrigin": "https://<agent-host>",
  "ipfsGateway": "https://<gateway-host>",
  "maxFinalizedAgeSeconds": 60,
  "maxGas": 2000000,
  "maxGasPriceWei": "100000000000",
  "bidAtoms": "<explicit uint96 amount>",
  "capacity": 1,
  "filter": {
    "version": "canary-1",
    "supportedDigests": ["<schema digest>", "<policy digest>"],
    "requiredSkills": ["structured-output-v1"],
    "advertisedSkills": ["structured-output-v1"],
    "minBudgetAtoms": "1",
    "maxBudgetAtoms": "<campaign budget ceiling>",
    "leadTimeSeconds": 120,
    "denyRequesters": [],
    "allowRequesters": ["<your requester address>"]
  },
  "broadcast": true,
  "listenHost": "0.0.0.0",
  "agentPort": 8443,
  "certificate": "<TLS certificate chain path>",
  "tlsKey": "<TLS key path>"
}
```

The numeric gas/freshness values show the existing interface, not proof that they suit today's network; select caps from simulation and your allowance. Map public HTTPS port 443 to the configured TLS listener or use a reviewed TLS reverse proxy without changing artifact bytes, paths or A2A headers. Reach the public hostname from the requester/validator hosts; a localhost-only success does not test live transport.

### Validator and requester

Create `.scratch/live/validator/config.json` from the **complete** [Layer 7 config](layer-7.md#roles-and-configuration), replacing all paths with this campaign's paths and all facts with the qualified ones. Use the immutable image ID, correct validator/relayer keys, private Kubo endpoint and public gateway. `broadcast` must be true. The validator is separate from the worker; it observes submissions, evaluates in Docker and publishes feedback. It does not allocate roots.

Create `.scratch/live/requester/config.json` from the **complete** [Layer 8 config](layer-8.md#configuration-and-signer-ownership). Set `networkScope=QUALIFIED_TESTNET`, the same manifest/evidence/genesis/RPC, `fixtureOrigins=[]`, `caFile=null`, its own database and requester keystore. Create a separate payout configuration for the credited owner, changing database and keystore. For public readers, use a separate database and `keystore=null`. No live process accepts fixture TLS exceptions or a partial L3 report as a full manifest.

Start long-lived processes in separate terminals or under a supervisor with the same working directory/environment:

```sh
# Terminal 1 — broadcasts verdict/export transactions after eligible observations
uv run --locked --env-file .env python -m apps.validator.main \
  --config .scratch/live/validator/config.json \
  > .scratch/live/logs/validator.stdout 2> .scratch/live/logs/validator.log

# Terminal 2 — discovers, bids and executes; explicitly select monad mode
uv run --locked --env-file .env python -m apps.reference_agent.main --mode monad \
  --config .scratch/live/agent/config.json \
  > .scratch/live/logs/agent.stdout 2> .scratch/live/logs/agent.log
```

The default reference-agent mode is `fixture`; omitting `--mode monad` is wrong for this guide. Confirm TLS/card reachability and public `agent show` profile/identity binding before creating work. `agent_starting` confirms composition, not that the listener is reachable or a job has succeeded. A profile's `UNPROBED` endpoint is not a health failure; card retrieval does not execute work.

## 8. Run one funded canary from create through withdrawal

For convenience define these shell functions in a third terminal:

```sh
requester() {
  uv run --locked --env-file .env python -m apps.cli.main \
    --config .scratch/live/requester/config.json "$@"
}
payout() {
  uv run --locked --env-file .env python -m apps.cli.main \
    --config .scratch/live/payout/config.json "$@"
}
```

Author `.scratch/live/terms.json` as a canonical `TaskTerms`. It has **no** top-level `schemaVersion` or caller-assigned task ID. This example is a template; timestamps are decimal Unix seconds:

```json
{
  "policyVersion": 1,
  "refundAddress": "<controlled refund owner>",
  "asset": {"kind": "NATIVE", "symbol": "MON", "decimals": 18},
  "budgetAtoms": "<funded positive uint96 budget>",
  "input": {"uri": "https://<assets-host>/input.json", "digest": "<Keccak digest>"},
  "outputSchema": {"uri": "https://<assets-host>/output-schema.json", "digest": "<Keccak digest>"},
  "taskFamily": "structured-output-v1",
  "alphaNum": "1",
  "alphaDen": "<positive integer coprime with alphaNum>",
  "biddingClose": "<T plus 5 minutes>",
  "allocationBy": "<T plus 10 minutes>",
  "acceptBy": "<T plus 15 minutes>",
  "resultBy": "<T plus 30 minutes>",
  "validationBy": "<T plus 45 minutes>",
  "validator": "<immutable validator address>",
  "validationPolicy": {"uri": "https://<assets-host>/policy.json", "digest": "<Keccak digest>"},
  "delegation": {"maxDepth": 0, "maxChildren": 0},
  "retryOf": null
}
```

Choose T from a freshly observed finalized block, with enough remaining lead time for authoring, content fetches and inclusion. The example windows are a manual-testing schedule, not runtime defaults. The acceptance gap must be at least 120 seconds. Never use local clock advancement on testnet.

For a virgin agent `p=500000`; winning requires **strictly positive** `p × alphaDen − 1000000 × alphaNum × bidAtoms`. For example, bid 10 and alpha 1/100 yield a positive score and a single-bid critical price 50 atoms, capped by the budget. This is an arithmetic example, not a realistic resource/gas price. Choose an affordable budget and alpha that admit the intended canary, or the correct outcome may be UNALLOCATED. Contract selection remains authoritative.

```sh
requester task validate --terms-file .scratch/live/terms.json --requester '<requester-address>'
requester task create --terms-file .scratch/live/terms.json \
  --request-id canary-001 --broadcast > .scratch/live/evidence/create.json
requester operation show --request-id canary-001
# Explicit recovery/confirmation of the same durable intent; repeat no faster than 2 seconds.
requester operation resume --request-id canary-001 --broadcast
```

Wait for `confirmation=FINALIZED_APPLIED` and the returned `data.taskRef`. Save the complete reference, never guess task ID 1. Set `TASK_REF` to its JSON representation, e.g. `'{"chainId":"10143","market":"<actual-market>","taskId":"<actual-ID>"}'`. Then:

```sh
requester task show --task-ref "$TASK_REF" --artifacts
requester task bids --task-ref "$TASK_REF"
# At finalized chain time >= biddingClose and < allocationBy:
requester task allocate --task-ref "$TASK_REF" --request-id canary-001-allocate --broadcast
requester operation resume --request-id canary-001-allocate --broadcast
requester task show --task-ref "$TASK_REF" --artifacts
requester task tree --task-ref "$TASK_REF"
```

Expected progression is OPEN → AWARDED → RUNNING → SUBMITTED → SETTLED/SUCCESS. The agent discovers the task and award itself; do not send it a winner instruction. RUNNING must be finalized before execution. The validator must produce a canonical receipt, and `feedback.status=PUBLISHED` requires verified registry feedback. A2A COMPLETED or `native_broadcast` does not establish any of these final outcomes.

Read `observedAt`, `finalizedHead`, `complete`, `missing` and cursors. One read scans a bounded prefix; a fresh database may require repeated commands to catch up. Do not interpret a stale zero credit or old OPEN status as current. Bid pages share a full-auction calculation; follow `nextCursor` for all displayed records.

```sh
payout credit show --owner '<actual-payout-owner>'
# Explicit positive amount copied from a current finalized credit observation:
payout credit withdraw --receiver '<controlled-receiver>' --amount-atoms '<amount>' \
  --request-id canary-001-payout --broadcast
payout operation resume --request-id canary-001-payout --broadcast
requester credit show --owner '<refund-owner>'
```

Withdraw any positive refund using that **refund owner's** configuration. The requester config only controls its own sender. Recheck remaining credits and finalized `CreditWithdrawn` events. Paid/refund receipts create credits; the withdrawal is a separate actual transfer. Never dynamically change a retained withdrawal amount to the new balance.

After a lost response, repeat the same command/request ID or use `operation resume`. Changed contents under a retained ID conflict. Keep the terms file unchanged, even if its deadlines later pass. `operation show` never rebroadcasts. An UNKNOWN send is not permission to start a new request or delete a journal. Only an intentional new economic task receives a new ID; a protocol retry also needs legal `retryOf` and new funding.

## 9. Enable L5/L6, child markets and live providers incrementally

### Resource enforcement and economics

Add the reviewed [execution configuration](layer-6.md#configuration). Use `executor.kind=DETERMINISTIC` first with the actual execution image. The [execution fixture](../specs/fixtures/layer-6/execution.json) is a field inventory only: replace operator, image, resource scenarios/envelope, prices, validity, charge caps and delegation allowances. Do not label synthetic fixture prices as live measurement.

To enable economics, set `bidAtoms=null` and add the full [economics configuration](layer-5.md#configuration-and-credentials). Supply explicit rates, units, price validity, overhead and policy. Keep `forecast.enabled=false` first. Unknown/stale price/overhead or unbounded tail can legitimately cause ABSTAIN. Persisted decisions are not reopened when you edit a policy.

After editing execution settings, recompute and bind the common records before writing your config:

```python
from modules.execution.rules import executionDigest

config["execution"]["runtime"]["configDigest"] = executionDigest(config["execution"])
for key in ("runtime", "pricing", "operator"):
    config["economics"][key] = config["execution"][key]
```

Capacity, supported template digests and runtime/model/resource inventory must match. Fresh real usage plus settlement should produce private reconciliation. Export only the chosen execution:

```sh
requester cost export --journal .scratch/live/agent/state.sqlite \
  --execution-ref '{"taskRef":<full TaskRef object>,"awardId":1}' \
  --output .scratch/live/evidence/cost-001.json
```

Replace the embedded object placeholder before running. This is a local read-only export, not publication. It is operator-reported and unauthenticated; missing cost is null, not zero, and own execution cost is not whole-agent profit. Existing output files are preserved.

### Parent and children

Use the L6 parent input/shape/policy, a parent-capable runtime and at least two leaf agents with different identities/signers/journals. Start all watchers before root creation. Child workers support the L2 solo template. Allowlist the manager's **execution signer** as a child requester, not just the root requester. Use independently selected bids and let each public market choose its winner.

Fund the manager execution wallet with fresh MON for both child deposits plus its gas allowance. The parent's reserved payment cannot finance those deposits. Set parent terms/delegation and the execution profile consistently; the reference manager publishes `left` and `right` tasks. Its child windows are 120 seconds bidding, another 120 seconds allocation and another 120 seconds acceptance; child result/validation deadlines are parent result time minus 300/180 seconds. Provide ample parent result time, publication/synthesis margins and remaining validation time. L1 envelope checks remain final authority.

Inspect `task tree` for separate child deposits/receipts, `activeChildren=0` before parent submission, and independently credited child payouts. Test fallback by allowing a child market to remain unallocated. A failed parent must not erase a successful child's credit. The manager progresses its children; the operator still allocates the root.

### Live Jev forecasting — paid

`TYPESAFE_API_KEY` is separate from `OPENAI_API_KEY`. Jev predicts usage scenarios for bids; it does not execute the awarded task or validate the result. The current [official API](https://docs.typesafe.ai/api) matches the adapter's Bearer-authenticated `POST https://api.typesafe.ai/v1/systemone` Choice request. The [model page](https://docs.typesafe.ai/models), checked 2026-10-08, lists `jev-1.13.0` and input-token billing; output tokens are free. Recheck your actual account/model/pricing at run time. AgentLance requires an exact version, not `jev-latest`.

Before setting `forecast.enabled=true`, supply `requestVersion=cost-choice-v1`, an available pinned model, justified maximum charge, fee unit, per-task/day budgets, daily call count, timeout and charge-bound expiry/source. Account for the **whole** request (input plus runtime, pricing and question), not just the user's input. Conversion rates mean MON atoms per source-currency atom. Do not copy a headline token price into a MON budget without an explicit conversion and rounding.

For the first paid test use one allowlisted new task, one available capacity slot, `dailyCalls=1` and an explicit small monetary cap approved by the operator. Restart with the same journal after the response and verify only one forecast attempt exists. Timeouts/unknown billing retain the maximum reservation and must not repeat the paid request. Retain the selected estimate, fallback/tail status, model/version, token usage and actual provider billing privately. A BID can use a local fallback; it does not itself prove a Jev request succeeded.

### Live Agents SDK execution — paid

Select `executor.kind=AGENTS`, `runtime.provider=openai` and the same actually available model in executor/runtime/pricing. Set `OPENAI_API_KEY`. The adapter fixes the official API URL and disables SDK/provider retries and tracing. It uses the locked Agents SDK and OpenAI client in `uv.lock`; do not independently upgrade one during the campaign.

Qualify the selected model's text/tool support, billing rates, token accounting, finite envelope and conservative request/output token bounds documented in [Layer 6](layer-6.md#configuration). Include model input/output and transform/tool resources without double counting. Set finite calls/turns/tools/steps/time/bytes and per-call/aggregate charge limits. Run one supported canary; compare retained usage with the provider's account-side record. A key working in another application does not prove this pinned runner/model combination is compatible. Arbitrary prompts, arbitrary shell tools and new task families are outside this reference runtime.

Do not combine the first Jev test and the first execution-model test: test each boundary independently, then combine. Started-but-unknown execution is not automatically repeated; preserve the claim and let the task's timeout/refund rules apply. No paid provider test was executed when writing this guide.

## 10. Live acceptance matrix and evidence to save

Use new explicit request IDs and controlled test tasks for each intentional economic run. Keep adverse tests bounded to your own agents/deployment.

| Test | Expected observation |
|---|---|
| Solo success | One root, admitted bid, finalized acceptance, exact artifact, real PASS, SUCCESS credit, one feedback index, withdrawal |
| Requester restart/lost response | Same transaction hash and TaskRef after replay; no second deposit |
| Worker restart/duplicate A2A hints | Same execution/correlation; completed work is not repeated; interrupted claims stay interrupted/unknown |
| No bidders / nonpositive score | UNALLOCATED after permissionless allocation; full refund credit |
| Cancellation | Succeeds only before close and with no admitted bids; bid/cancel race resolved by chain |
| Validator unavailable | Stop your validator for a controlled submitted task; after validationBy expire it; VALIDATOR_TIMEOUT/NONE, refund, no fabricated FAIL |
| Invalid committed result | Controlled external test worker commits wrong bytes against valid prerequisites; real validator FAIL; VALIDATION_FAILED/FAILURE and refund |
| Missing/mismatched artifact | No invented verdict; availability diagnostic, bounded retries, eventual timeout |
| Parent/children | Separate deposits/receipts; complete tree; successful children remain paid even if parent fails |
| RPC/index interruption | No latest fallback, no fresh nonce for unknown sends; direct discovery works without index/hints |
| Jev/provider outage | Bounded fallback/ABSTAIN or interrupted execution, no repeated uncertain paid effect |
| Evidence expiry/dependency change | Visible qualification failure; investigate/requalify, never clear a finality halt to continue |

The stock deterministic agent produces correct copies: testing an invalid result requires a separate controlled external worker/harness, not an undocumented CLI failure flag. Do not weaken the validator or manually sign a fixture verdict to claim this case passed live. Run finality contradiction, malicious RPC and destructive registry-transfer tests in isolated/local infrastructure first.

Save source/build/image evidence, qualified manifest and exact evidence files, account role addresses, task/execution references, commands/request IDs, transaction hashes, finalized block/hash/timestamps, immutable inputs/cards/results/evidence, canonical allocations/receipts, credit observations, withdrawals and feedback indices. Record which components were real, deterministic, shared-control or separately operated. Save private prices/usage/provider invoices separately from the public run report. Keep keys, passwords, signed transaction bytes and credential-bearing RPC URLs out of shareable evidence.

## 11. Logs and troubleshooting

The Python live entrypoints and requester CLI configure UTC timestamped AgentLance logs on **stderr**. JSON command results stay on **stdout**; redirect streams separately. Standard HTTP client INFO/DEBUG request logging is not enabled. Added lifecycle events contain selected public IDs/statuses, not transaction bodies, signed bytes, evidence signatures, provider responses or credential URLs. Python/library exception tracebacks and operator configurations can still contain sensitive context: keep logs private and review before sharing.

| Event | Interpretation |
|---|---|
| `agent_starting`, `validator_started`, `*_stopped` | Runtime composition/stop; validator startup follows Docker preflight |
| `native_prepared` | Signed intent is durably saved; command, operation ID, transaction hash and nonce are available |
| `native_broadcast` | An attempt is about to be sent; not evidence of inclusion or success |
| `native_send_unknown` | Response was unavailable; reconcile the retained hash/bytes |
| `native_rejected` | Simulation returned a known protocol rejection; see error code |
| `native_finalized`, `native_finalized_reverted` | Verified canonical finalized transaction result; status-0 remains the lower-layer UNKNOWN failure diagnostic |
| `validation_admitted`, `validation_stage` | Job identity and persisted stage changes; polling is not logged as progress |
| `validation_attempt_failed` | Stable error kind and task/job IDs; inspect the private retained job for detail |
| `feedback_attempt_failed`, `feedback_published` | Export attempt failure, or verified publication/index; settlement can precede publication |
| `*_observation_paused`, `agent_observation_halted` | Dependency/read failure or fatal observer error; not a fabricated outcome |

```sh
tail -f .scratch/live/logs/agent.log .scratch/live/logs/validator.log
rg 'event=native_|event=validation_|event=feedback_' .scratch/live/logs
requester operation show --request-id canary-001
requester task show --task-ref "$TASK_REF" --artifacts
uv run --locked python -m apps.validator.main \
  --config .scratch/live/validator/config.json --outcome '<decimal-task-ID>'
```

`--outcome` reads retained validator job/receipt/export records, including signed attestations; it is an authorized local diagnostic, not a sanitized public report. Prefer L8 `task show` for shareable public inspection. Configure supervisor log rotation; do not enable broad HTTP/SDK debug logging with live credentials. These logs help correlation but do not replace journal recovery or on-chain evidence.

| Symptom | Check / action |
|---|---|
| Startup rejects config | Closed schemas reject missing/extra fields; use exact layer configs. Confirm real manifest/evidence digests, expiry, key role, image and file paths |
| Agent starts but no bid | Task still OPEN with lead time? Allowed requester, matching template/skills, capacity, metadata/card/payout, signer gas? For economics inspect the private `economic_candidates` decision/disposition; ABSTAIN is not a network error |
| Unknown/pending transaction | Inspect retained operation/hash on the same qualified chain. Preserve the queue; do not change request ID, sender or nonce. No automatic fee replacement exists |
| `External nonce consumption` / queue conflict | Another program used the sender or state differs. Stop new exposure and investigate; do not create a new database to bypass it |
| Old task/credit data | Compare finalized/indexed/observed stamps and `missing`; repeat bounded reads for catch-up |
| SUBMITTED never settles | Validator running, correct key/image, deadlines, artifact digests, Docker capacity, Kubo cat and public gateway readback? Operational failure is not FAIL |
| SUCCESS but feedback unavailable | Publisher/reputation qualification, governance/code changes, relayer balance/caps, registry ownership/operator restriction and export attempts |
| TLS/profile fetch fails | Public DNS/TLS chain/origin/routes, digest-pinned card and registration ID/namespace. Private-address fixtures are rejected live |
| Bid abstains after enabling economics | Missing/stale price/FX/overhead, unbounded `other` tail, elapsed lead time, unknown charge allowance, insufficient capacity; do not replace unknown costs with zero |
| Paid model returns but task times out | Check finalized acceptance, retained execution step/usage, tool/request bounds, result commit window and uncertain prior effects |
| Finality conflict | Preserve journal and evidence, stop new sends, verify the RPC/history. Never delete halt flags to turn conflicting data into truth |

For safe read-only SQL diagnostics, open the selected journal with SQLite URI `mode=ro`/`PRAGMA query_only=ON` or the `sqlite3 -readonly` CLI. `native_operations`, `economic_candidates`, `execution_records` and `validation_records` contain private recovery data. Select IDs/stages/diagnostic fields; never dump whole rows into a public log. A live SQLite backup must use SQLite's backup mechanism (or stop the writer and preserve DB/WAL together), not copy only the main file while writes continue.

After stopping the validator writer, explicit extra attempts are available:

```sh
uv run --locked --env-file .env python -m apps.validator.main \
  --config .scratch/live/validator/config.json --retry-validation '<task-ID>'
uv run --locked --env-file .env python -m apps.validator.main \
  --config .scratch/live/validator/config.json --retry-export '<task-ID>'
```

Each grant retains history and existing intents; validation retry requires a still-submitted, unexpired task and does not change a verdict. Restart the normal validator afterward. No grant authorizes repeating an unknown paid worker/model invocation.

## 12. End a campaign without abandoning funds

Stop creating roots and admitting new controlled work; inspect all outstanding roots/children and unknown native operations. Keep the necessary worker/validator/child-progress processes running until obligations finish, or explicitly account for the timeouts you are testing. Cancellation cannot stop an already awarded task. An elapsed deadline requires a canonical expiry transaction; stopping a process alone refunds nothing.

For each expired eligible task, use `task expire --task-ref ... --request-id <unique-id> --broadcast`, confirm finality, then withdraw credits with the correct owner configuration. Allow eligible pending feedback to finish. Record any unresolved hash/job/credit rather than declaring the campaign complete. Gracefully stop processes, take coherent private backups, preserve artifacts for the promised retention interval and stop only campaign-owned infrastructure.

Call a campaign **live-tested** only when its intended rows in §10 have retained actual finalized evidence, the required paid boundaries (if any) were really used, costs/limitations are labeled, and no unknown operation or unaccounted credit is hidden. Offline completion, a schema-valid report and an A2A success response do not establish that result.

## Reference map

- [Architecture](../layers.md), [deployment policy](../specs/deployment-policy.md), [canonical protocol](../specs/protocol.md).
- [L3 testnet driver/evidence](layer-3-testnet.md), [L4 discovery and identity](layer-4.md), [L5 economics](layer-5.md), [L6 execution/delegation](layer-6.md), [L7 validator/qualification](layer-7.md), [L8 CLI](layer-8.md).
- [Monad testnet](https://docs.monad.xyz/developer-essentials/testnet), [TypeSafe HTTP API](https://docs.typesafe.ai/api), [TypeSafe models/billing](https://docs.typesafe.ai/models). The Monad compatibility checkpoint was recorded on 2026-10-07; the TypeSafe API/model facts were rechecked on 2026-10-08. Recheck both for each campaign. No live endpoint, public registry address, deployment or provider account was qualified by writing this document.
