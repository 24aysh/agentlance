# Layer 4 operation and verification

Layer 4 connects the existing participant to finalized contract state. The direct agent needs one qualified RPC connection, an exclusive SQLite journal and its own profile/content endpoints. Envio and A2A award notifications are optional. L3 remains the only economic authority. [The specification](../specs/layer-4.md) defines the boundaries and pending live qualification.

## Offline gate and demo

```sh
make setup
make setup-l3
make check-l4
make demo-l4
```

Use Python 3.12.12, Node **22.15 or later**, pnpm 11.19.0 and a running Docker engine. The lockfiles pin Web3.py 8.0.0 and Envio 3.14.0. Initial setup downloads dependencies; the index integration also needs `postgres:18.3` and `hasura/graphql-engine:v2.43.0` Docker images. Pre-pull these for disconnected runs. No live credentials, real model calls or testnet funds are used.

`check-l4` runs the complete L0–L3 gate, Envio code generation/type checks/handler tests, all L4 Python tests, the L4 demo, Ruff and diff checks. It fails on missing infrastructure or skipped L4 cases. Reports are in ignored `.scratch/layer4/gate.json`, `pytest.xml` and companion logs. The GraphQL test creates uniquely named loopback-only Postgres/Hasura containers, a private network and an actual Envio process; it removes only those resources afterwards. Restart/reindex tests operate solely on that disposable database.

`demo-l4` starts an isolated Monad Anvil and runs the actual Web3 adapters, direct watcher, SQLite journal and reference participant. No task reference is supplied to the worker: it discovers a task, submits one explicitly priced bid, observes its award, waits for finalized acceptance, invokes the deterministic L2 worker once, commits its artifact and survives restart/replay. An explicit L3 caller allocates and later triggers the validator-timeout refund. The report records actual transaction hashes, result and receipt in a fresh `.scratch/layer4/demo-*/demo.json`. The registry is a labeled test double; HTTPS content is served by an isolated test transport. The inherited L2 gate separately verifies real HTTPS/A2A subprocesses.

To run focused checks after setup:

```sh
uv run --locked pytest tests/integration/test_layer4_*.py modules/agent_client/test_discovery.py modules/adapters/a2a/test_notifications.py modules/adapters/storage/test_journal_migration.py
pnpm --dir apps/service check
```

## Direct adapter composition

- `ChainConnection` verifies chain ID/genesis, deployment block/hash, market runtime hash and `readPolicy`, and retains finalized checkpoints. Configure a finite positive `maxAgeSeconds`; unsupported or stale finalized tags never fall back to `latest`.
- `MonadRegistry` implements the existing registry port and lazily resolves identities through `resolveProfile`. `MarketWatcher.scanRegistry()` records identities/invalidation provenance without fetching every registration URL. Current identity/URI reads are refreshed directly; exact admitted card bytes stay digest-pinned in the journal.
- `MonadMarket` implements the existing market port. A single controlled execution account exclusively owns the journal and its native nonce stream. Signed bytes and their hash are saved before broadcast. Retries reuse those bytes; at most three sends are attempted, at least two seconds apart and before the action deadline. Reconciliation continues afterwards. No fee replacement or automatic nonce cancellation is implemented. External nonce consumption requires operator investigation.
- `MarketWatcher.scanMarket()` processes finalized whole blocks, normally in ranges of 128 with a 32-block overlap. RPC range failures reduce the range down to one block. Defaults bound each response to 2,000 logs, a tick to 2,048 blocks, projections to 10,000 tasks/identities and retained logs to 100,000. Constructors expose these limits. Capacity errors stop progress at the preceding committed block; records are never silently dropped.
- `listOpenTasks(kind="ALL", parentRef=None, cursor=None, limit=100)` is shared by the direct watcher and `IndexedMarket`. Pages carry `indexedThrough`, `finalizedHead` and an opaque cursor. Parent filtering is valid only for `CHILD`. Keep the same filters across pages. A `CONFLICT` cursor requires restarting pagination. Always use a fresh `MonadMarket.readTask` before forecasting or action.
- `deliverObserved` performs private template/skill, asset, deadline, budget, capacity and requester filtering before delivering a candidate. Its callback owns the bid decision; the L4 reference runtime uses only an explicitly configured fixed amount. Policy version/capacity changes reconsider retained tasks. This does not reserve resources or implement L5 pricing.
- `AwardNotifier.notify(executionRef)` is the optional callable hint boundary for an operator/relay. It reads the canonical winner, uses only the admitted card digest, serializes attempts and persists one stable message ID. Missing historical card bytes can make notification unavailable. Use a dedicated HTTP client created with `createHttpClient(NetworkPolicy())`; unknown metadata must never use an unrestricted HTTP transport. A2A success cannot authorize execution or payment. The direct runtime does not require a relay.

The journal migrates version 1 to 2 while retaining claims, correlations, intents, content and halt flags. A fixture-session journal cannot bind to a chain. Journal content defaults to a 64 MiB limit (`maxContentBytes`); writes at capacity wait/fail explicitly. There is no automatic deletion of admitted cards, results or unresolved transactions. Archive or raise limits deliberately after investigating capacity. Back up the journal and preserve its chain/sender binding; deleting it can destroy recovery evidence.

## Explicit Monad reference runtime

```sh
uv run --locked python -m apps.reference_agent.main --mode monad --config /absolute/path/qualified-agent.json
```

The default mode remains the isolated L2 fixture. `monad` mode accepts a **complete** schema-valid `DeploymentManifest`, including L7-dependent registry/publisher fields. The proposed L3-report bootstrap is unresolved and is not silently enabled. No live activation is claimed until the actual deployment and registry inputs are supplied and qualified.

The JSON configuration is closed and requires all these fields:

| Fields | Values |
|---|---|
| `manifestPath`, `genesisHash`, `registryVerificationPath` | Full verified manifest, chain genesis digest, and local exact evidence bytes matching the manifest's `registryVerification.digest` |
| `rpcUrl` | Operator RPC URL present in the manifest; finalized/historical calls and creation-form `eth_call` must work |
| `database`, `agentRef` | Exclusive durable database path and canonical identity in this deployment's chain/registry |
| `executionKeystore`, `ownerKeystore` | Each is `{ "path": "/absolute/keystore.json", "passwordEnv": "AGENTLANCE_KEY_PASSWORD" }`; encrypted Ethereum keystores, passwords in operator environment |
| `bundleDir`, `artifactOrigin`, `ipfsGateway` | Directory with exact `card.json`, `output-schema.json`, `policy.json`; publicly reachable HTTPS artifact origin; explicit HTTPS IPFS gateway or null |
| `maxFinalizedAgeSeconds`, `maxGas`, `maxGasPriceWei` | Positive integer freshness seconds; integer native gas limit; positive decimal-string gas price cap in wei |
| `bidAtoms`, `capacity`, `broadcast` | Explicit uint96 decimal-string bid, positive integer capacity snapshot, literal `true` acknowledging this mode broadcasts signed transactions |
| `listenHost`, `agentPort`, `certificate`, `tlsKey` | A2A/artifact HTTPS listener address/port and local TLS certificate/key paths |
| `filter` | Closed policy object described below |

The filter has `version` (nonempty string), `supportedDigests` (ordered output-schema and validation-policy digests), `requiredSkills` and `advertisedSkills` (unique skill IDs), `minBudgetAtoms` and `maxBudgetAtoms` (uint96 decimal strings), `leadTimeSeconds` (nonnegative integer), `denyRequesters` and `allowRequesters` (canonical addresses; empty allow list allows all). Change `version` when changing policy. The reference worker supports exactly the L2 structured-output schema/policy pair; it checks that its advertised card skills match the filter. Its CLI signs owner permits with an EOA owner keystore. External clients can use contract owners through the unchanged permit/registry interfaces.

The operator publishes matching registration/Card/content URLs and supplies funded signing accounts. No registration transaction, validator, allocator/expiry keeper, price oracle or model runtime is started. The A2A endpoint uses the existing public profile; output availability is independent of payment. Credentials, journals and configuration with secrets belong outside tracked source.

## Registry pin and verification record

[`registry.identity.abi.json`](../specs/registry.identity.abi.json) is a minimal ABI subset of the official ERC-8004 [IdentityRegistryUpgradeable source at b9e466c](https://github.com/erc-8004/erc-8004-contracts/blob/b9e466c250744a7e06b13dff9d3c2844ed64f825/contracts/IdentityRegistryUpgradeable.sol), including inherited ERC-721 `Transfer`. It pins `ownerOf`, `getAgentWallet`, `tokenURI`, `Registered`, `URIUpdated` and `MetadataSet`; it is not evidence that any address deploys that revision. The test double emits those shapes without claiming production wallet-control verification.

The manifest-bound evidence file must contain an `identityVerification` object with:

- `identityRegistry` and `identityVersion` matching the manifest;
- `sourceRevision` equal to the pinned full commit hash;
- `deploymentBlock`, `deploymentBlockHash` and `validUntil` as decimal height, digest and Unix expiry;
- `proxyKind` and inspected `codeObservations` (`address`, `codeHash`) / `storageObservations` (`address`, `slot`, `value`); proxies require storage observations of their implementation/admin dependencies;
- successful `probes.transferClearsWallet`, `restoredWalletControl`, `contractOwner` and `registrationDiscovery`, backed by actual verification evidence.

These are operator-qualified facts, not booleans the agent can establish by reading metadata. The adapter rechecks their expiry, runtime code, recorded proxy storage and deployment hash. Qualify the actual shared registry and its historical RPC support before live use. Unexpected revisions fail closed for new identity-dependent work; existing accepted duties retain their admitted authority.

ERC-1271 calls use creation-form `eth_call` with tiny initcode that makes a **STATICCALL capped at 50,000 gas**, returns success/return length/first word, and is never deployed. This avoids treating a state-writing simulated wallet call as a valid signature. Revert, gas exhaustion, short/malformed/oversize returns cannot produce valid magic. Wallet caller/context restrictions still require deployment qualification; actual market preflight and transaction execution remain authoritative.

## Optional Envio service

`apps/service/config.yaml` defaults to an explicitly local chain/address. Set `ENVIO_CHAIN_ID`, `ENVIO_MARKET`, `ENVIO_START_BLOCK` to verified market deployment facts and `ENVIO_RPC_URL` to the operator's RPC. Configure Envio's `ENVIO_PG_HOST`, `ENVIO_PG_PORT`, `ENVIO_PG_USER`, `ENVIO_PG_PASSWORD`, `ENVIO_PG_DATABASE`, `ENVIO_PG_SCHEMA`, `HASURA_GRAPHQL_ENDPOINT`, `HASURA_GRAPHQL_ADMIN_SECRET` and `ENVIO_INDEXER_PORT` for a dedicated Postgres/Hasura instance, then run:

```sh
pnpm --dir apps/service codegen
pnpm --dir apps/service start
```

Provision read-only query access using Hasura's permissions; do not expose its administrative secret to public consumers. The Python `IndexedMarket` accepts an operator-owned HTTPX client for endpoint authentication. No private agent cost/capacity/signing data enters the index. Running Envio `--restart` rebuilds its configured database: use it only on an explicitly disposable/dedicated projection.

`MarketLog` retains canonical creation/event bytes and block/transaction/log provenance. `MarketTask` stores only creation, immediate parent and first closure height. There is no economic reducer or cached parent accounting. Envio's built-in `_meta.progressBlock` supplies its contiguous processed prefix, including empty blocks; the query client caps that prefix at an independently qualified finalized RPC header and verifies each returned creation against its block hash and successful transaction receipt. Tentative Envio rows beyond that prefix are excluded, and Envio rollback handles its unfinalized history. Index omissions/incorrect hints never authorize work: candidate state is re-read directly.

## Trust and recovery limits

RPC integrity and the configured finality policy remain trust assumptions; L4 is not a light client. A previously finalized hash conflict persistently halts writes and execution and requires investigation, not deletion of the halt flag. Unavailable/stale heads pause processing. If historical data is pruned, reconnect to a qualified archive provider rather than fabricate missing records.

The durable start claim preserves L2's at-most-once invocation rule. A crash after claiming work but before persisting output is `INTERRUPTED`; L4 does not rerun a paid worker. Native transactions may remain `UNKNOWN` after exhausted sends or an expired deadline. A finalized failed transaction with no authenticated revert bytes records a private diagnostic, raises an adapter error, and is never relabeled with a guessed protocol code or rebroadcast. Routine gates exercise these limits; live Monad qualification remains separate.
