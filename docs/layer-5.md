# Layer 5 operation guide

Layer 5 adds private cost forecasts and an operator-selected bid policy to the existing participant. The [specification](../specs/layer-5.md) defines its assumptions and acceptance criteria. It introduces no contract, public oracle or settlement authority.

## Offline checks

```sh
make check-l5
make demo-l5
```

The full gate includes `check-l4` and its lower-layer gates, so the pinned Foundry toolchain, Node/pnpm and Docker must already be available. Reports are written under ignored `.scratch/layer5/`. The demo uses actual local-EVM reads/transactions, synthetic registry/content and the deterministic worker. It exercises BID and ABSTAIN, restart, one execution, estimate-linked usage, validator-timeout refund and a later history forecast. Jev and the indexer are disconnected. It does not establish live calibration or Monad deployment qualification.

Focused checks without Docker or paid providers:

```sh
uv run --locked pytest modules/economics modules/adapters/test_jev.py \
  modules/adapters/storage/test_economics_storage.py \
  tests/integration/test_layer5_recovery.py tests/integration/test_layer5_boundaries.py -q
```

## Configuration and credentials

Copy [.env.example](../.env.example) to ignored `.env`. The application reads exported environment variables; it does not implicitly load arbitrary dotenv files:

```sh
uv run --env-file .env python -m apps.reference_agent.main --mode monad
# --config /absolute/path/config.json can override AGENTLANCE_CONFIG.
```

`TYPESAFE_API_KEY` is Jev's server-side credential. `AGENTLANCE_CONFIG` names the complete JSON runtime configuration. The two keystore-password variables in the example must match the JSON's `passwordEnv` fields. Leave keys empty and `economics.forecast.enabled=false` for disconnected forecasting. An enabled provider with a missing key fails configuration; it does not silently substitute another account.

Keep the existing [Layer 4 configuration](layer-4.md), including deployment verification, TLS, content origins, signer/owner keystores and filter. To select economics, set `bidAtoms` to null and add `economics`. A fixed amount and an economics block are mutually exclusive. Omitting `economics` retains fixed-bid behavior. Economic `policy.leadTimeSeconds` must be at least the filter's allowance. Mainnet and partial deployment manifests remain unsupported.

The complete [synthetic economics fixture](../specs/fixtures/layer-5/economics.json) shows the closed JSON shape. **Its prices, bounds and budgets are test data, not a live configuration.** The local-EVM demo replaces its gas allowance with three transactions at the configured adapter gas/fee caps. Supply actual values for:

| Field | Meaning |
|---|---|
| `operator` | Explicit economic cost bearer; not inferred from payout/ownership |
| `runtime` | Exact provider/model/config digest, supported template digests, resource inventory, joint scenarios, declared envelope, history maximum age and estimate TTL |
| `pricing` | Provider/model-specific rates and fixed fees, effective/expiry times, source, explicit conversions into MON atoms |
| `policy` | Version, quantile 50/80/95, margin ppm, success cap ppm and lead time; no economic defaults |
| `overhead` | Gas, own validation and other known task forecast cost bounds in MON atoms, expiry and source; null means unknown |
| `forecast` | Enabled flag, pinned model/request version, fee unit, per-request maximum charge, per-task/day budgets, daily call count, timeout, charge-bound source/expiry |
| `maxRows`, `maxBytes` | Private economics storage limits; exhaustion stops new work without removing recovery records |

Runtime `resources` are disjoint billable quantity names and units. Each scenario's `usage` contains every resource, including explicit zeros; `other` is mandatory. The optional envelope has the same usage vector and a justification. Fixed fees are charged once per obligation. Do not split overlapping cached/uncached/reasoning quantities into separately billed resources unless the selected provider does so. Provider billing normalization belongs to that runtime's reporting boundary.

Rates and quantities are reduced rational objects with decimal-string `numerator` and `denominator`. Rates mean **source currency atoms per resource unit**. A conversion means **MON atoms per source currency atom**, not whole-currency exchange rates. Each conversion supplies `sourceUnit`, MON `targetUnit`, rational `rate`, `observedAt`, `expiresAt` and `source`. Prices, model/request versions and configuration digests are retained with the forecast. There are no environment overrides that secretly change its economics.

The live TypeSafe [HTTP API](https://docs.typesafe.ai/api) uses a Bearer key and one Choice question. The adapter pins an exact model ID, currently illustrated by `jev-1.13.0`, and `requestVersion="cost-choice-v1"`; moving aliases are rejected. Verify available versions and pricing in the [official model reference](https://docs.typesafe.ai/models). No checked-in fixture price is used as a real Jev quotation. An operator must supply a justified maximum billable charge before enabling requests.

The adapter sends only the task input and runtime scenario definitions, excluding prices, bids, budgets, reputation and allocation state from the usage judgment. It bounds requests to 64 KiB, responses to 256 KiB, and total duration to at most ten seconds and the remaining bid lead time. It does not follow redirects or retry HTTP requests. Numeric probabilities are parsed exactly, checked against the complete scenario set, then normalized/apportioned in code. Confidence and the chosen option never authorize a bid.

## Persistence and recovery

Journal migration v3 retains L2/L4 content, claims, transactions, stream cursors and halt flags. New private tables retain candidates, estimates/baselines, forecast attempts, reports, active report selections and receipt annotations. Each journal keeps the existing exclusive process lock; do not run multiple runtimes against one database. A bound history cannot silently change its agent or cost bearer.

The L4 callback durably enqueues a candidate and returns promptly. One asynchronous economics job checks for an existing bid/intent first, refreshes eligibility, captures comparable history and pricing, obtains a forecast and saves its decision. The existing participant signs/submits. A fresh check at signing catches expiry during profile fetching. Accepted work and transaction reconciliation continue while Jev is awaiting a response.

Forecast allowance is reserved before calling Jev. One attempt per task and one in-flight paid call are enforced in the database. Timeout/crash/ambiguous billing retains the full maximum charge against that day's allowance. A successful token-usage response is not authenticated billing evidence, so the MVP conservatively retains the reservation too; it does not refund allowance based solely on HTTP success. The ledger's actual expense remains unknown until separately reported. A started attempt without a persisted response becomes UNKNOWN after restart and uses a local fallback, never another request.

Decisions are not reopened by changing policy, prices or history. A new funded retry is a new task. Pending native transactions keep their original operation and signed bytes under L4 recovery even after forecast expiry. A finality conflict halts new forecasts/bids. Data-age checks use UTC timestamps; detected clock rollback pauses economics until the clock catches up. It never resets spending allowances.

## Usage and evaluation interfaces

The runtime exposes `economics.history`; these are local Python interfaces for L6/L7 producers, not public ingestion endpoints:

```python
await history.recordUsage(report, context, supersedesReportId=None)
await history.attachSettlement(executionRef, receipt, stamp)
evaluation = history.evaluateForecasts([executionRef])
```

`report` is the frozen `CostReport`. `context` contains `operator`, `agentRef`, canonical `task`, the exact `runtime`, local execution `status`, `zeroUsage`, `evidence` (`FIXTURE` or `LOCAL`). Record ingestion re-reads the canonical own award; receipt ingestion re-reads settlement. Do not report missing/aborted usage as zero. Use INCOMPLETE/CENSORED and preserve null fields. Status is not a validator verdict.

The current adapter accepts operator-reported and incomplete usage. Arbitrary provider references do not authenticate invoices: `PROVIDER_ATTESTED` claims are rejected until an actual provider-evidence verifier is implemented. This is a disclosed reporting limitation; it has no payment effect.

An exact report replay is harmless. A correction names the currently selected report, uses a new report ID and retains the old record. Changing an expense's contents requires a new expense ID; an existing expense cannot be attributed to a different execution. A prior report replay cannot undo a correction. Forecast fees before an award remain task-scoped in the private ledger; they cannot be packaged as reports for imaginary executions. Explicit later attribution retains the expense ID and must match that task's award.

History matches input/template/runtime exactly and reprices at the current snapshot. Selection records counts for incompatible, stale, self-task, evidence, capacity and incomplete exclusions. A censored/incomplete observation in the selected cohort disables the empirical source; failures with complete quantities remain eligible. Forecasts keep a pre-run rates-plus-history baseline. Evaluation uses the saved rates to isolate usage error from price drift, alongside the unchanged reported bill and evidence/status labels. It returns P80/P95 covered/eligible/excluded counts and exact rational mean absolute error. Zero eligible samples include explicit unavailable reasons, with no calibration claim.

L5 does not enforce the worker's resource envelope or reserve execution capacity. Those remain L6 responsibilities. Real validation, automatic receipt/cost collection and provider-attested telemetry remain later integrations. Existing qualified-deployment requirements are unchanged; the offline gate is not permission or evidence for live funded operation.
