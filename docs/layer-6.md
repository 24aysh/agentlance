# Layer 6 execution

Layer 6 is an optional composition of the existing participant. It reserves capacity before signing bids, accepts with a fixed own-work reserve, executes accepted work asynchronously, publishes independent child markets, and delivers own-execution observations to Layer 5. Contracts, task schemas, award selection and validation authority are unchanged.

## Offline acceptance

```sh
make setup
make setup-l3
make setup-l6
make check-l6
make demo-l6
```

Docker must provide Linux memory, CPU and PID enforcement. `setup-l6` builds the reviewed worker from a digest-pinned Python base and writes the platform-specific immutable image ID to `.scratch/layer6/image-id`. It does not build images while bidding or executing. The gate fails if Docker, the SDK or the local Monad toolchain is missing; nothing is silently skipped. Build output, local keys, databases and reports remain under ignored scratch/test directories.

`demo-l6` exercises solo execution, two public child markets with three separate bidder processes, and unallocated-child fallback. Workers discover finalized tasks and make L5 decisions independently; the manager cannot select a winner. Two workers have independent local input preferences and the third competes without that preference. The model is an isolated implementation behind the actual Agents SDK runner, and its tool really runs in Docker. Only public content retrieval and fixture validator attestations are isolated. This evidence makes no live model, billing or testnet qualification claim. No paid model call or API key is required. The synthetic actors reuse some owner/payout roles; execution signers and journals are distinct. This demonstrates process isolation, not independent real-world operators.

## Configuration

Add an `execution` object to the existing Monad configuration. Its closed fields are documented in `specs/fixtures/layer-6/execution.json` and checked by `modules/execution/rules.py`. The fixture is explicitly synthetic, not a live price quotation. Set:

- `operator`, `capacity` and the reviewed immutable `executor.image`. Capacity must match the application setting. One process owns one journal and one execution signer; use different signers for simultaneous processes.
- `executor.kind`: `DETERMINISTIC` for the reviewed structured-copy container, or `AGENTS` for the SDK runner. `promptVersion` is `structured-copy-v1`. The host tool has no arguments, receives only the bound step request and cannot access signing, the journal or the market.
- `runtime` and `pricing`: the exact L5 inventory and price snapshot. Deterministic execution declares `transform/invocation`. SDK execution additionally declares `modelInput/token` and `modelOutput/token`, counting input/output totals once, including cached/reasoning tokens. Both require a complete finite envelope and per-resource prices; fixed fees are unsupported. The model name must be identical in executor/runtime/pricing. Live operation requires an explicitly qualified model and billing source.
- `limits`: wall/request time, CPU millicores, memory/PIDs/tmpfs, request/output/log bytes, model turns/calls, tool calls, total steps, finite per-call and aggregate MON charge limits with source/expiry, read/delivery attempts and journal retention limits. The per-call charge must cover the reserved vector at the pinned price. Reservations are conservative and never replenished after uncertain effects. The SDK request reserves `requestBytes + 1024` input tokens and `outputBytes` output tokens per call, enforces serialized request bytes and passes an output-token ceiling to the provider. This conservative text-only bound must be qualified for the selected model; no average usage estimate is treated as an enforced bill.
- `delegation`: enable flag, fixed own-work reserve, fixed `childBudgetAtoms`, total gross deposit allowance, gas allowance, publication/synthesis margins, fallback flag and allowed child schema/policy digest pairs. Solo reserve is zero. The reference manager publishes exactly the `left` and `right` leaf tasks using the existing integer-copy template. Each child opens bidding for 120 seconds, permits allocation for another 120 seconds, then leaves a 120-second acceptance gap. Child result and validation deadlines are parent result deadline minus 300 and 180 seconds. Full L1 deadline/envelope checks can reject a parent with insufficient time. Publication cutoff is an additional configured margin before child bidding close. Gas allowance covers publication as well as scoped allocation/expiry, reserved using the native adapter's gas/price caps. Child deposits require fresh wallet funds; escrow credits are not spendable deposits.

The parent bundle's shape/policy are `specs/fixtures/layer-6/output-schema.json` and `policy.json`. Publish them before bidding; child workers use the existing Layer 2 bundle. `filter.supportedDigests`, bundle files, runtime and execution settings must agree. The reference application supports only these reviewed templates.

After selecting every field, calculate the digest and share the resulting runtime/pricing with L5 before starting:

```python
from modules.execution.rules import executionDigest

config["execution"]["runtime"]["configDigest"] = executionDigest(config["execution"])
config["economics"]["runtime"] = config["execution"]["runtime"]
config["economics"]["pricing"] = config["execution"]["pricing"]
config["economics"]["operator"] = config["execution"]["operator"]
```

The digest includes all executable settings and omits only its own `runtime.configDigest` field. Changed configuration applies to new bids; retained reservations/runs preserve their complete snapshot. An incompatible or missing old image/model stops the obligation visibly. L6 cannot increase an L5 bid to cover an own-work reserve: reserve greater than offered bid prevents signing.

`OPENAI_API_KEY` is read only by the host SDK adapter. Live execution requires `runtime.provider=openai` and uses the fixed official API URL; `OPENAI_BASE_URL` cannot redirect it. `TYPESAFE_API_KEY` remains exclusively L5/Jev's key. Encrypted owner/execution keystores keep their existing environment references. No key enters a task container or private execution record. SDK trace export, hosted tools, handoffs, streaming, SDK/provider retries and persisted provider conversations are disabled. `openai-agents==0.17.4` is locked with `openai==2.36.0`: the newer 2.54 client changed required usage fields incompatibly with this runner's default usage constructor.

## Recovery and observations

Journal version 4 migrates existing rows without clearing claims or transaction intents. HELD reservations include outstanding or uncertain bids; only finalized evidence or a locally stopped terminal execution releases them. Retained bids/awards are reconstructed before new admission, even if they exceed a newly lowered capacity. Legacy synchronous STARTED executions keep their previous INTERRUPTED behavior.

Managed steps persist PREPARED and then STARTED before any model/container request. Outputs and metered usage are retained before advancing. A restart can resume a completed step, frozen plan, saved artifact or transaction intent. A STARTED step without retained output becomes UNKNOWN; its named container is terminated and the paid request is never automatically repeated. No arbitrary task shell command or filesystem path is imported: Docker uses exact stdin bytes and bounded stdout JSON, no mounts, a non-root user, read-only root, disabled network and dropped capabilities.

A parent consumes only a finalized SUCCESS child receipt bound to the result, identity, validator, policy and lineage, with matching available bytes. Unallocated/failed/timeout/unavailable results use local fallback if the original limits permit. Parent submission still waits for canonical `activeChildren == 0`. Parent failure does not undo child payment; funded child monitoring and native recovery continue. Root allocation and the real validator remain external responsibilities.

Own EXECUTION reports are OPERATOR_REPORTED, with pricing retained in a private outbox. Unknown telemetry stays INCOMPLETE with null quantities/costs. A deadline-driven never-started award is delivered as TIMED_OUT with `zeroUsage=true` and an INCOMPLETE empty report, so it is recorded without becoming a zero-cost history sample. COMPLETED describes a local artifact, not payment. Forecast fees, native gas, validation and child transfers are not copied into this report. Outbox delivery retries identical bytes a bounded number of times and cannot block artifact publication. Corrections use Layer 5's existing selected-report/predecessor interface; invoice verification and broader profitability remain Layer 7 work.

Inspect `execution_records` through an authorized local SQLite reader for reservation/run/step/outbox diagnostics. Do not edit execution claims, signed intents or completed step bytes to force a retry. A pending outbox after exhausted delivery attempts requires an explicit operator retry of the same report/context through `ExecutionHistory.recordUsage`; it does not authorize re-execution or replacement of payment state.
