# Layer 8 operations (no frontend)

The requester CLI creates funded roots, progresses markets, cancels eligible tasks and withdraws credits. Public reads compose finalized contract state with verified event history. An explicit cost export discloses one selected execution from a private operator journal. None of these commands executes an agent or changes market economics.

## Setup and checks

Use the existing Python/Node/Foundry/Docker setup from [Layer 7](layer-7.md). No dependency or contract deployment is added.

```sh
make setup
make setup-l3
make setup-l6
make setup-l7
make check-l8
make demo-l8
```

`check-l8` runs the entire inherited gate, requester/inspection/disclosure tests, the unchanged 24 external A2A HTTP assertions against the independent Node example, Node parser/ABI checks and the CLI demo. Missing tools and skipped required cases fail. Reports are under ignored `.scratch/layer8/`. Run gates serially: fixtures use local EVM identities and Docker resources, and a full gate already owns its child checks.

The demo covers an independently implemented Node participant, a parent and two funded children using the L6 workers, canonical validation failure and validator timeout. Root creation/progression, tree/credit inspection and withdrawal use separate CLI processes. Docker validation, Kubo, the publisher and pinned reputation implementation are real. The Node card/A2A/artifacts cross actual TLS. L6 worker artifact/model transport and fixture clock/identity setup remain explicitly synthetic. All roles share fixture control; this is not evidence of a third-party operator or live deployment. The frontend is deferred.

## Configuration and signer ownership

Save configuration locally, outside tracked source. `--config` or `AGENTLANCE_APP_CONFIG` selects it. Paths are resolved from the process working directory. This live-mode example requires existing qualified facts; it is not a usable deployment manifest:

```json
{
  "networkScope": "QUALIFIED_TESTNET",
  "manifestPath": ".scratch/deployment/manifest.json",
  "registryVerificationPath": ".scratch/deployment/registry-verification.json",
  "validatorVerificationPath": ".scratch/deployment/validator-verification.json",
  "rpcUrl": "<qualified RPC URL in the manifest>",
  "genesisHash": "<qualified genesis hash>",
  "database": ".scratch/requester/application.sqlite",
  "keystore": {"path": ".scratch/keys/requester.json", "passwordEnv": "AGENTLANCE_REQUESTER_KEYSTORE_PASSWORD"},
  "maxFinalizedAgeSeconds": 60,
  "maxGas": 2000000,
  "maxGasPriceWei": "100000000000",
  "ipfsGateway": "<public HTTPS IPFS gateway origin>",
  "fixtureOrigins": [],
  "caFile": null
}
```

A reader uses `keystore: null`. Reads never load the configured key. Do not share this writable database with the reference worker or validator. One sender must have one nonce queue, including other programs that use the wallet. Different credit owners use separate application configurations/databases. Only encrypted EOA keystores are supported by this CLI. Other wallets retain the existing public ABI.

`LOCAL_FIXTURE` is restricted to chain 31337 at a loopback RPC. Its explicit network scope permits declared loopback HTTPS origins with a local CA and uses the fixture EVM clock. It never qualifies a live deployment. The demo generates this configuration, certificates and encrypted test keys in its ignored working directory. Public mode rejects those fixture exceptions and validates complete manifests and digest-bound qualification documents. Registry/publisher availability is reported separately from canonical market state.

## Requester commands

References below are JSON strings containing the entire canonical reference. Supply actual values; the angle brackets are explanatory. Monetary amounts are decimal integer strings in native MON atoms (18 decimals). Publish input/schema/policy bytes before constructing the canonical `TaskTerms` file.

```sh
uv run --locked --env-file .env python -m apps.cli.main task validate \
  --terms-file .scratch/terms.json --requester '<requester address>'
uv run --locked --env-file .env python -m apps.cli.main task create \
  --terms-file .scratch/terms.json --request-id root-1 --broadcast
uv run --locked python -m apps.cli.main operation show --request-id root-1
uv run --locked --env-file .env python -m apps.cli.main operation resume \
  --request-id root-1 --broadcast
```

Validation checks terms, exact content digests, supported output shape/policy semantics and any legal root retry. Creation retains the native intent before dispatch. Request IDs contain 1–128 characters and are scoped to chain, market and sender; changed contents under the same ID conflict. Retry the same ID after a lost response. A protocol retry is a new funded root with legal `retryOf` and a new request ID.

Use `task allocate`, `task expire` or `task cancel` with `--task-ref '<TaskRef JSON>' --request-id <id> --broadcast`. Allocation is permissionless inside its window; the contract chooses the winner. Cancellation is requester-only, before close and before any accepted bid. Expiry uses the existing state cutoff. The chain decides races.

`credit show --owner <address>` returns aggregate withdrawable credit and paginated credit/withdrawal events. `credit withdraw --receiver <address> --amount-atoms <positive uint256> --request-id <id> --broadcast` withdraws the configured sender's explicitly named amount. A paid settlement creates credit; it is not itself a transfer to an external wallet. No per-task withdrawn amount is inferred from aggregate balances.

A command returns promptly with its saved operation. Inspect `confirmation`, `result`, `transactionHash`, receipt and diagnostic. Confirmation labels distinguish pending, mined but unfinalized, finalized/applied, rejected, finalized/reverted and unknown. `operation show` verifies observations without reconciling or rebroadcasting; a verified finalized receipt can appear while the saved lower-layer result is still null. `operation resume` explicitly recovers the unchanged native intent. Status-0 receipts retain the existing UNKNOWN lower-layer result and failure diagnostic. Unknown sends never receive an automatic new nonce.

## Public reads

```sh
uv run --locked python -m apps.cli.main tasks list --kind ROOT --limit 20
uv run --locked python -m apps.cli.main task show --task-ref '<TaskRef JSON>' --artifacts
uv run --locked python -m apps.cli.main task bids --task-ref '<TaskRef JSON>' --limit 20
uv run --locked python -m apps.cli.main task tree --task-ref '<TaskRef JSON>'
uv run --locked python -m apps.cli.main agent show --agent-ref '<AgentRef JSON>' --task-ref '<TaskRef JSON>'
```

Output conforms to [application.schema.json](../specs/schemas/application.schema.json), which references unchanged canonical types. Every view includes network scope, observation/index/finality stamps, completeness, missing components and an optional cursor. Pass `--cursor` unchanged with the same query. Pages default to 20 and cap at 100. A tree returns at most 21 nodes; retry references are not child edges.

Each command performs bounded direct discovery, without Envio or award hints. A new reader may need several explicit invocations to catch up. Check `missing` for `INDEX_LAG`/`INCOMPLETE_HISTORY`; never interpret an older zero balance or empty page as a current observation. RPC qualification failures stop authoritative reads. Forged cursor stamps cannot halt trusted history. Finalized contradictions do halt the journal.

Auction explanations use the complete verified bid set at the observation stamp, even when the displayed bids are paginated. PREVIEW is a calculation, MATCH compares to canonical allocation, and CONFLICT is visible. Raw bids retain frozen p/counters, score, owner/signer/payout and profile digest. Current identity/card claims do not rewrite those facts. Synthetic Beta(1,1) is labeled separately from real success/failure counts. Endpoint status is UNPROBED; card retrieval is not a work or competence probe.

`--artifacts` verifies committed bytes and evidence/signature binding. It never changes settlement on an availability failure. PUBLISHED requires both the canonical publisher mapping and matching reputation feedback; verified absence is PENDING, failed dependency reads are UNAVAILABLE, and NONE outcomes are NOT_APPLICABLE. No validator private database is needed.

## Explicit private disclosure

```sh
uv run --locked python -m apps.cli.main cost export \
  --journal .scratch/agent/state.sqlite \
  --execution-ref '<ExecutionRef JSON>' \
  --output .scratch/disclosure-1.json
```

The explicit journal path and execution select one read-only SQLite/WAL snapshot. The writer can remain running. Export does not tick reconciliation or call a model. The output is a closed numeric/enum allowlist with selected report/estimate/reconciliation identifiers and digests, forecast summaries, completeness/provenance, own execution cost, direct child payments and the saved-rate actual-cost comparison. Raw reports, runtime context, resource/provider text, prompts, paths and credentials are excluded. A correction selects a new report; stale reconciliation produces null totals with a reason until the owning runtime updates it. Existing output files are never overwritten.

The disclosure is **operator-reported and unauthenticated**. A digest binds bytes; it does not attest invoices or identify an author. Missing usage remains null. COMPLETE own-execution usage does not establish complete whole-agent cost, profit or system utility. Export writes only locally; sharing or publication is a separate action.

## Independent implementation and remaining gates

See [external-agent/README.md](../examples/external-agent/README.md). The local-only example uses Node/ethers and public ABIs/schema artifacts, with its own encrypted key, durable signed intents, execution claim and A2A correlation. It imports no reference-agent runtime. Its test-only controller is outside its HTTP interface and only prepares isolated conformance states.

Live network/registry/validator qualification, ownership/funding, artifact hosting/retention and independently operated external participation remain the open gates in [the specification](../specs/layer-8.md#12-open-questions-and-live-activation-gates). Passing offline checks does not resolve them or deliver a frontend.
