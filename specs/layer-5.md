# Layer 5 — Hybrid Cost Oracle and private economic policy

**Status:** offline implementation and acceptance complete (2026-10-07); live provider/deployment qualification remains pending. Based on Layers 1–4 at `60b6f83`. Requirements **L5-01–L5-10** in [the architecture](../layers.md) govern this layer; [protocol semantics](protocol.md), [canonical schemas](schemas/protocol.schema.json), [Layer 4](layer-4.md) and [the technology choices](../tech_stack.md) remain unchanged.

## 1. Purpose and scope

Turn an eligible public task into a private, explainable **BID or ABSTAIN** decision. Estimate the agent's own execution cost from explicit resource prices, comparable execution history and optional TypeSafe Jev probabilities. Preserve the estimate so later usage can be compared with what was known before bidding.

The MVP is one configured external agent running a solo worker, whether bidding on a root task or someone else's child task. Use Python, the existing local SQLite journal and an operator-supplied pricing catalog. Jev is optional; history or declared bounds must support a disconnected demo. No registration, discovery or ignored task triggers inference.

| Owns | Does not own |
|---|---|
| Resource pricing, currency conversion with explicit provenance, cost distributions and fallbacks | Discovery, eligibility filtering, finality, registry/profile resolution or index operation (L4) |
| Private policy, forecast spending limits, cache and durable decision records | Bid signing, broadcast, nonce management, transaction recovery or award admission (L2/L4) |
| Ingestion of attributed usage and estimate-versus-actual evaluation | Execution, capacity/resource enforcement, retries, decomposition or child funding (L6) |
| Read-only use of public reputation and supplied settlement receipts | Authoritative reputation, allocation, escrow or settlement (L1/L3); validation, receipt collection and feedback publication (L7) |

No production FX oracle, learned bid optimizer, cross-agent history service, new daemon, public forecast endpoint or UI is required. Forecasts never alter an admitted bid, reserved price, payout, result or reputation. Missing telemetry cannot delay settlement.

## 2. Inherited behavior and reuse

The Layers 1–4 implementation inspected before this work provides:

- [`deliverObserved`](../modules/agent_client/discovery.py) checks a fresh finalized task and calls `onCandidate(task, stamp)` only after `filterTask` returns `ELIGIBLE`. Delivery repeats after restart; returning normally acknowledges it. The callback result is not a bid command.
- [`DiscoveryRuntime.consider`](../apps/reference_agent/chain.py) supplied a configured fixed amount to `Participant.prepareBid`. L5 adds an explicitly selected economic-policy mode for amount selection; existing fixture/fixed-bid modes remain available and clearly labeled.
- [`Participant.prepareBid`](../modules/agent_client/participant.py) validates task support, refreshes authority/profile, signs a permit and persists the immutable bid intent. It recovers a retained intent before preparing a new one. L5 must reuse it.
- [`Journal`](../modules/adapters/storage/journal.py) already binds the local identity/session and live deployment, locks out a second writer and retains content, operations and execution claims. Add a versioned migration without resetting these records.
- [`validateRecord`](../modules/domain/records.py), `contentDigest`/`jsonBytes`, [`calculateProbability`](../modules/market_core/reputation.py), [`calculateScore`](../modules/market_core/market.py) and [`summarizeCosts`](../modules/market_core/analytics.py) supply record validation, local digest encoding and existing arithmetic. Cost-estimate cross-field checks still need implementation; schema validation alone is insufficient.

`CostEstimate`, `CostReport`, `CostItem`, `MoneyUnit` and `Rational` are already frozen. Do not add fields to them. Store provenance, configuration, attribution, corrections and pre-award expenses in private local records. `ExecutionRef` is exactly `{taskRef, awardId}`; do not invent an execution reference for a losing bid or an unawarded forecast.

L4's capacity value is a snapshot, not a reservation. L5 does not solve concurrent-award overcommit; L6 must do so before enabling a general execution fleet. The existing reference worker is deterministic and supports one schema/policy pair. L5 does not turn it into an LLM worker or qualify a live deployment.

## 3. Components and interfaces

Keep five small responsibilities: `PricingCatalog` prices quantities; `ExecutionHistory` stores/selects observations; `CostEstimator` constructs distributions; optional `JevPredictor` supplies scenario probabilities; `BidPolicy` makes the local decision. Ordinary functions suffice for pure logic. Only the predictor and existing storage/chain/content boundaries need adapters.

| Interface | Inputs → outputs |
|---|---|
| `enqueueCandidate(task, stamp)` | L4's eligible canonical `TaskSpec` and `ReadStamp` → durable candidate acknowledgement; no paid call in this callback |
| `estimateCost(task, runtime, pricing, history, now)` | Pinned local inputs → `{estimate, provenance, reason}`; estimate is canonical `CostEstimate`, or null if no valid pricing basis exists; may await the bounded predictor |
| `decideBid(task, estimate, reputation, overhead, policy, now)` | Bound estimate, public terms/counters and explicit operator policy → local `BidDecision`; no signing or chain write |
| `recordUsage(report, context, supersedesReportId=None)` | Canonical `CostReport` plus explicit attribution/compatibility metadata → stored/no-op/conflict result |
| `attachSettlement(executionRef, receipt, stamp)` | A matching finalized L4 receipt observation → idempotent history annotation; not a settlement action |
| `evaluateForecasts(scope)` | Explicit private history scope → coverage counts, absolute errors, baseline comparisons and unavailable reasons |

Add one read-only capability, `readReputation(taskRef, agentRef)`, to the chain and fixture adapters. Return the closed wrapper `{taskRef, agentRef, taskFamily, snapshotBlock, counters, p, stamp}`: canonical refs, `Counters`, integer ppm `p` and the existing `ReadStamp`. Read the task and existing Solidity `readCounters(agentRef, taskFamily, task.reputationSnapshotBlock)` at the same qualified finalized boundary. Compute `p` with L1's `calculateProbability`; fixture mode uses L1 checkpoints. This is an additive Python interface, not a new contract getter or schema change. Reject mismatched refs, family, snapshot or stamp; unknown counters are not `(0,0)`.

L6 supplies runtime compatibility metadata and usage, including failed/aborted runs. L7 supplies finalized receipts and corrected reports through these ingestion functions, owning its own collection/retry loop. L8 may read an operator-authorized projection of decisions, estimates and calibration; private history is not automatically published. External agents remain free to use their own policy and submit directly through the existing protocol.

## 4. Private models and state

Use closed, versioned local records, validated at ingestion. Reuse canonical scalar encodings: amounts/timestamps/large counters are decimal strings, ppm/counts use bounded integers, fractions are reduced with positive denominator. Use Python integers/`Fraction` for arithmetic; never binary floating point for a decision. Decimal provider probabilities are decoded exactly at that adapter boundary.

For new local digest records, recursively sort mapping keys before `jsonBytes`, preserve array order and hash with `contentDigest`. Derive each estimate ID from a versioned domain label, its context digest and the estimate without `estimateId`; derive decision IDs equivalently. Retain the hashed bytes. This convention does not change existing protocol/content hashing or externally supplied report IDs.

| Record | Required contents |
|---|---|
| Runtime cost profile | Version/digest; provider and exact model/runtime/config identifiers; supported family/schema/policy digests; complete billable-resource inventory and units; joint scenario definitions; declared resource/fixed-fee bounds and their justification; estimator/history age and forecast TTL limits. Local configuration, never Agent Card claims. |
| Pricing snapshot | Version/digest; `effectiveAt`, `expiresAt`, source references; unique resource/provider/model/unit keys with nonnegative rational rates in a `MoneyUnit`; applicable fixed fees. Explicit zero rate is allowed; absent rate is unknown. |
| Conversion snapshot | Optional version/digest, source and target `MoneyUnit`, positive rational target-atoms per source-atom, `observedAt` and `expiresAt`, source/provenance. No implicit precision or MON/USD conversion. |
| Forecast context | AgentRef and explicit economic cost-bearer address; TaskRef and task/input digests; runtime/pricing/conversion/estimator/scenario/request versions; history cutoff and selected report IDs; limits and provenance, including omitted/censored counts. |
| Forecast attempt/expense | Stable request ID and context digest; state, reserved maximum fee and unit, attempts/times, provider request reference, response digest, actual usage/fee or explicit unknown. Expense category `FORECAST`; a task reference is sufficient before an award. |
| Bid policy | Version/digest; selected quantile `50`, `80` or `95`; uint32 `marginPpm`; `successPpmCap` in `[1,Q]`; finite forecast spending/call limits and bidding lead time. No silently supplied economic defaults. |
| Bid decision | Stable ID; AgentRef/TaskRef; estimate ID or null; policy/context digests; action `BID`/`ABSTAIN`; bid atoms only for BID; ordered reason codes and the numeric calculation inputs; reputation stamp; creation/expiry; separate submission disposition/operation link. |
| Usage context | Explicit cost bearer and agent identity; TaskSpec, runtime digest/provider/model, execution status `COMPLETED`/`ABORTED`/`TIMED_OUT` and provenance; active report selection/correction chain; optional finalized receipt/stamp. Execution status is not a validator verdict. |

Prospective **overhead** is a separately itemized, sourced, expiring MON-atom bound for the cost bearer's gas, validation and task forecasting expense. Include only expenses that bearer pays. Zero must be explicit and justified. Gas allowances are private budgeting inputs; they do not replace L4's actual fee/balance checks. Child payments are not own execution usage and are outside this solo bid policy.

Persist candidates, immutable estimates/provenance, forecast charges, decisions, reports and active-report selections in the existing journal. Scope keys by the bound agent/operator and full market namespace, not numeric task ID alone. Preserve unsigned values as text, not SQLite int64. Enforce configured positive row/byte limits; storage exhaustion stops new forecasting/bids without deleting unresolved work or failing the market.

One candidate slot per `(AgentRef, TaskRef)` progresses `QUEUED → EVALUATING → DECIDED`. Persist a forecast attempt as `STARTED` before contacting a paid provider; its terminal state is `COMPLETE`, `FAILED` or `UNKNOWN`. Restart after STARTED without a durable response makes it UNKNOWN and invokes the non-provider fallback; it does not issue another paid request. Decision action/content is immutable. Submission disposition can become `PENDING`, `APPLIED`, `REJECTED` or `EXPIRED` by observing the existing intent/market. No local state is a second task lifecycle.

## 5. Deterministic pricing and forecasting — L5-02–L5-06

### Cost basis and units

The estimate covers **the agent's own EXECUTION expense for one solo obligation**, across success, failure and abort, including bounded execution retries only if declared by that runtime. Forecast fees, gas, validation and transfers are separate; never add a historical total bill to a predicted total bill.

For each joint usage scenario, sum applicable `rate × quantity` and fixed fees exactly. The profile must identify disjoint billable quantities: cached input cannot also be charged as uncached input; reasoning tokens are a separate charge only when that provider actually bills them separately. Unknown resources, incompatible units, missing prices or an invalid fee model make the source unusable, not free.

V1 emits estimates in `MoneyUnit {currency:"MON", decimals:18}`. Convert each complete source-currency subtotal through its explicitly supplied conversion, then sum the rational target costs and **ceil once** to MON atoms. Identity conversion needs no FX record. Preserve source quantities/currencies and conversion provenance for audit. Reject expired/not-yet-effective prices, stale conversions and results beyond uint256; do not clamp. No live price polling is necessary: validated local snapshots are sufficient.

### Comparable history

Estimator version `hybrid-v1` uses only the same agent/cost bearer, deployment namespace, input digest, family, schema/policy digest pair and exact runtime/provider/model configuration. Exact input matching is deliberately conservative; generalizing across task sizes or natural-language similarity is deferred. Select the newest at most **32** observations inside the configured positive maximum age, before filtering on completeness, breaking equal timestamps by report ID. Freeze the selection and cutoff before inference. Later producers must submit an INCOMPLETE/CENSORED observation for a terminated run with missing telemetry; absence is not evidence of zero usage.

Use one active COMPLETE report per execution with all required EXECUTION quantities known. Reprice quantities at the current snapshot; do not pool old currency totals. Include adequately observed failures and aborts, not only successful jobs. Retain excluded/missing/censored observations and exclusion reasons. A same-cohort incomplete/censored execution disqualifies the empirical history source for v1; it must not silently bias the forecast toward cheap completed runs. A known failed job with a complete bill remains eligible. `sampleCount` is the number of comparable complete runs actually used, never synthetic trials or Jev options.

Each eligible history run is one joint sample of all resource usage with equal probability. Preserve correlation between model calls, tools and retries; no multiplication of independent marginal estimates. Keep up to **32** configured Jev scenarios including an `other`/overflow option. Both sources price the same resource inventory, currency and execution scope. Jev scenarios have stable IDs, mutually exclusive descriptions and joint quantity vectors (or a null-cost overflow), supplied by the runtime profile rather than generated by executing the task. Use source-qualified IDs of at most 64 characters in the combined distribution.

Finite historical observations do not prove a bounded tail. A BOUNDED distribution requires a documented finite runtime cost envelope covering every billable resource and all scenarios, including overflow; that declaration is a local trust assumption until L6 enforces it. Price a bounded overflow at that full envelope. Values outside it invalidate the envelope. If uncovered cost has a known positive probability, preserve it as null-cost mass and mark UNBOUNDED. If its probability cannot be justified, return UNKNOWN/ABSTAIN. Zero predicted overflow without an envelope does not establish BOUNDED. For history without a valid envelope, v1 uses the conservative UNKNOWN fallback rather than inventing tail probabilities.

### Fusion and summaries

For `n` eligible history samples, use `w=n/(n+20)` when both sources are usable. Form the mixture of their distributions; do not add their costs. HISTORY alone uses its empirical distribution, JEV alone its validated distribution. If neither is usable, a complete declared envelope produces BOUNDS with one scenario at its full upper cost and probability `Q`; otherwise ABSTAIN has `tail=UNKNOWN`, no scenarios and null summaries. A stale/missing pricing snapshot prevents any priced fallback; record an ABSTAIN decision with no estimate rather than inventing a pricing digest.

Normalize provider decimals only after checking exact option coverage, finite values in `[0,1]`, positive total and sum within `1/Q` of one. Reject other malformed distributions. Divide by the accepted total using exact rationals and merge null-cost mass into one tail scenario. Allocate the final mixture's `Q=1,000,000` ppm by floor plus largest fractional remainder, ties by stable scenario ID; discard zero-mass entries. Preserve positive unbounded-tail probability with at least one ppm, taking that atom from the largest finite mass (ties by ID) if ordinary apportionment rounded it to zero. At most 64 scenarios remain; do not silently truncate excess scenarios. Invalid or oversized source output falls back explicitly.

Apply [L0's summary contract](protocol.md): finite mean is the **floor** of the probability-weighted cost sum; quantile is the first sorted cost whose cumulative mass reaches its threshold; null-cost mass sorts last. UNBOUNDED requires null mean and at least one null-cost scenario. P95 is null when finite cumulative mass is below 950,000, including a 5.0001% unbounded tail; exactly 5% can leave a finite P95 and must still show UNBOUNDED. UNKNOWN/ABSTAIN has no numeric summaries. A finite chosen quantile is not proof of a bounded tail.

`coverage` describes the cost basis, envelope/tail assumptions and fallback in plain language within its existing limit. Detailed source IDs, repricing, normalization and exclusions live in the private context. Estimates are explicitly uncalibrated predictions until evaluated; mixture weights are a heuristic, not a proven probability model.

## 6. Optional Jev, caching and spending — L5-07

Use the existing HTTPX dependency for a small TypeSafe adapter. The documented Choice API accepts a fixed option map and returns probabilities for those options; AgentLance's use of execution scenarios is an application-level design, not a vendor-provided cost oracle. Send the task input and runtime scenario definitions as state; do not send prices, bids, budgets, reputation or allocation state that could bias the usage judgment. Pin endpoint, exact model identifier, request/template and scenario versions; check the returned model and option IDs. Use `probabilities`, not the selected choice or `confidence`, to construct the distribution. See the [official Choice request/response contract](https://docs.typesafe.ai/primitives/choice), inspected 2026-10-07.

Only locally configured provider endpoints/credentials are permitted. Send the minimum digest-verified task content and local runtime/rate descriptions needed for the forecast. Do not send keys, wallet material or private history rows. Task text is untrusted data, with no authority to choose URLs, tools, spending caps or signing actions. Reuse `Participant.supportTask`/`ContentStore` for supported input fetching and size/digest/URL checks; cap the serialized forecast request at 64 KiB and response at 256 KiB. An over-limit task uses a local fallback; do not silently truncate away cost-relevant content.

Cache within the private agent namespace by **TaskRef and full task digest, input digest, runtime/config digest, pricing/conversion digest, estimator version, scenario/request/model versions and history-selection digest**. No reuse across a changed key. Set `expiresAt` to the earliest of configured TTL, any pricing/conversion validity limit, selected-history age limit and bidding cutoff (`biddingClose − leadTimeSeconds`). It must exceed creation time. Use explicit local UTC time for data age, monotonic time for request duration and qualified chain time for protocol deadlines; a detected backward clock movement invalidates cache freshness until resolved.

Atomically reserve a known maximum provider charge before a request. Enforce per-agent configured call count and amount budgets in one declared fee unit, per UTC day and per task, plus **one concurrent paid forecast and at most one provider attempt per task**. Limits survive restart and configuration/cache changes; changing versions cannot evade task or daily budgets, and an existing ledger cannot be rebound to another fee unit. Check and reserve against all limits in one transaction. A maximum charge that cannot be established means no paid call. Jev-disabled operation reserves/spends zero.

Use a total request timeout of at most 10 seconds, further shortened by remaining bid lead time; no hidden HTTP/SDK retries. Bound late-response handling to that same attempt. A timeout, cancellation, process loss or ambiguous bill keeps the full reservation charged against local limits, with actual cost unknown. Release unused reservation only on attributable billing evidence; a success/HTTP error alone does not prove the charge. Keep already-started attempts charged to their original day and never reset the ledger on a backward clock jump. These limits control local allowance, not provider billing guarantees.

Persist a successful response/estimate before exposing it to policy. Replays use the retained response/fallback/decision, never a new paid request. New history or policy versions do not reopen an already decided task in this MVP; a new funded retry task is a distinct candidate. Expiry makes an undecided candidate ABSTAIN or a saved, unsubmitted BID's disposition EXPIRED; do not rewrite the saved action. Once an intent exists, only L4 reconciles it.

## 7. Private bid policy — L5-08

The reference policy is an explicitly selected, versioned **reservation-cost heuristic**. It makes no truthfulness, win-rate or profit guarantee. Other external agents need not use it.

For a fresh BOUNDED estimate with all overhead known or conservatively bounded:

```text
Q = 1_000_000
c = selected P50/P80/P95 own execution cost, in MON atoms
h = gas + own validation + task forecast expense bounds, in MON atoms
p = L1 probability from the task's pinned public reputation snapshot
q = min(p, policy.successPpmCap)
bidAtoms = ceil((c + h) * (Q + policy.marginPpm) / q)
```

Require `q>0`; missing reputation is unavailable, not the synthetic prior. The known empty canonical history legitimately yields the existing `Beta(1,1)` prior. `q` is a private proxy for paid-success risk, not a new authoritative score; Jev confidence, self-declared skill and locally reported successes cannot raise `p`. Choosing a cap and margin is an operator decision. All formula arithmetic is exact; ceil is integer division `(numerator + denominator − 1)//denominator`.

ABSTAIN if cost, required overhead or conversion is unknown; tail is UNKNOWN/UNBOUNDED; estimate or policy inputs are stale/mismatched; `q=0`; the result exceeds uint96 or task budget; or L1 `calculateScore(p, alphaNum, alphaDen, bidAtoms)` is nonpositive. Never clamp a costly bid to the budget. Zero is allowed only when all included expenses are explicitly zero and the score is positive. Do not predict competitors' bids or equate task budget, bid and eventual critical payment.

Persist reason codes such as `BID_READY`, `COST_UNKNOWN`, `UNBOUNDED_COST`, `PRICING_UNAVAILABLE`, `FORECAST_LIMIT`, `REPUTATION_UNAVAILABLE`, `ZERO_SUCCESS_PROXY`, `OVER_BUDGET`, `NONPOSITIVE_SCORE`, `EXPIRED` and `CONFIG_CHANGED`. Distinguish a provider fallback from abstention: a failed Jev call may still produce a valid BOUNDS/HISTORY bid. These are local diagnostics, not additions to the protocol error catalog.

## 8. Execution and recovery flows

1. **Handoff:** L4 performs discovery/filtering. The candidate callback atomically creates or finds its slot, then returns. Queue capacity/storage failure leaves the L4 delivery unacknowledged. Economics processing is bounded background work in the existing process; an awaiting forecast must not block award admission, acceptance deadlines or transaction reconciliation.
2. **Recover first:** before any forecasting, check the existing admitted bid and journal bid intent. Adopt/link the exact retained amount and operation; no forecast, replacement amount, signature or new nonce is justified by a restart. An old fixed-bid intent remains valid even after selecting economic mode; label its missing estimate explicitly.
3. **Evaluate:** obtain fresh direct state and reuse `filterTask` with the current capacity/policy; fetch supported input only after eligibility. Use a lead time at least as large as L4's configured allowance. Capture prices/history/limits and the pinned reputation read. If the task/config changed, is closed or lacks lead time, stop without paid work. Build or reuse the estimate, then durably save the decision and explanation. Temporary chain/content unavailability keeps QUEUED work pending with bounded retries; the bidding cutoff terminates it.
4. **Submit:** immediately before creating an intent, refresh direct state, reuse L4's filter, and check decision expiry and all bound configuration versions. Persist/link the BID decision before calling `Participant.prepareBid` with its exact amount and the existing permit construction. A crash after decision but before intent creation resumes this handoff while timely; a crash after intent creation resumes that intent regardless of estimate expiry. Only a finalized accepted bid establishes admission; policy output is not admission.
5. **Execute/settle:** existing Layers 2–4 continue award observation and deterministic execution. L5 does not require an estimate for an already admitted duty. L6/L7 later feed usage and receipt annotations through §3; no new validator or execution loop is introduced here.

Polling/retry intervals for unpaid reads must be at least two seconds with at most ten seconds per request, bounded by the candidate cutoff. A finality conflict preserves L4's persistent halt and disables new forecasts/bids; it does not become a task failure. A failed bid transaction does not reopen the economic slot. An unresolved operation stays under L4 recovery even after task expiry; ABSTAIN never cancels a possibly broadcast bid.

## 9. Usage ingestion and evaluation — L5-09–L5-10

Validate canonical reports plus their context. Check task/award/agent/estimate bindings and explicit cost-bearer attribution; do not infer the payer from the owner or payout address. `COMPLETE` means all declared quantities/costs are known, with unique expense IDs and valid reduced fractions; `INCOMPLETE` and `CENSORED` retain nulls and lower-bound observations. An aborted/timed-out run is never automatically a complete zero-cost run. A complete empty report is zero only with an explicit zero-usage declaration in its context.

Reports are immutable by `(operator, reportId)`. Identical replay is a no-op; changed bytes are a conflict. A correction requires a new ID and explicit `supersedesReportId` naming the currently selected report for the same attributed execution; switch the active pointer atomically and keep both. Stale/concurrent corrections conflict. Replaying an old report does not reactivate it. Expense values are immutable by `(operator, expenseId)` across reports; corrected values need new IDs. Feed only the selected reports and explicit scope to L1 analytics, preserving its deduplication and unavailable semantics.

Keep FORECAST, GAS, VALIDATION, EXECUTION and CHILD_PAYMENT distinct. Pre-award or losing-task expenses stay in the local expense ledger. When a real award later exists, they may be explicitly attributed once to its report using the same expense ID; never fabricate an award or count the ledger and report twice. Do not present L1's execution/tree metrics as whole-agent profit including unmatched participation expenses. Provider references are provenance, not proof: `PROVIDER_ATTESTED` requires verified provider evidence; an operator upload alone remains `OPERATOR_REPORTED`. Store incomplete evidence without upgrading it.

Receipt attachment verifies full task/award/winner binding and finalized provenance through existing adapters; identical receipt replay is harmless and conflicting finalized receipt data halts that ingestion path. Reports may arrive before receipts and receipts before reports. A receipt can classify success/failure/unknown outcome but cannot fill missing usage. No-show, timeout and validator-timeout records remain distinct; no record changes protocol counters locally.

Freeze a **rates-plus-history baseline** beside each estimate using the same pre-forecast history/prices and no Jev (HISTORY, else BOUNDS/ABSTAIN). For later comparable complete execution reports, reprice EXECUTION quantities at the saved forecast pricing/conversion snapshot for comparison. Actual reported bills stay unchanged and are displayed separately; price drift must not masquerade as usage-prediction error. Exclude a run from its own forecast/baseline and expose censoring, incompatibility and missing-data counts.

Report P80/P95 coverage as exact `covered/eligible` counts (`actual ≤ quantile`) and mean absolute error of the predicted mean in atoms, with the same measures for the baseline. No eligible rows yields unavailable, never 0% or perfect coverage. Null quantiles/means have their own excluded counts. Include sample count, selection scope, provenance and an `uncalibrated` label; v1 does not assert statistically validated calibration or automatic improvement. Deduplicate by execution and selected report; corrections update the evaluation selection, never the historical estimate, baseline or bid. Synthetic fixture observations are visibly labeled and isolated from live history.

## 10. Invariants, failures and security

- Economic authority stays on chain. No L5 input authorizes payment, boosts counters, changes an award or permits execution before finalized acceptance.
- Eligibility precedes task fetching/inference. A repeated delivery, retry, cache miss or restart cannot cause a second paid forecast attempt for that task, or another bid intent.
- Every finite price has a complete resource basis, units, source/version, validity window and recorded rounding. Unknown cost/charge/FX is never replaced with zero.
- Scenarios have unique IDs, positive integer ppm totaling Q and no lost tail mass; cost summaries obey L0. History and Jev describe the same expense scope.
- An existing obligation survives estimator/provider/registry outages and changes to current policy. Frozen estimates and reports remain auditable; no latest-wins economics.
- RPC, operator price/bound declarations, telemetry and Jev remain disclosed trust dependencies. Forecast output is fallible and private; provenance labels are not validation of task correctness.

| Failure/edge case | Required result |
|---|---|
| Unsupported/ignored task, unknown capacity, new registry identity | Zero forecast calls and no L5-created execution |
| No history, Jev disabled/error/429/timeout/malformed response | HISTORY if adequate, otherwise complete BOUNDS or explicit ABSTAIN; no unbounded retry |
| Missing/stale price, unknown tool fee, mixed currencies, rounding overflow | No priced bid; retain diagnostic and source data |
| Positive tiny or >5% unbounded tail, uncovered scenarios | Preserve tail; reference policy abstains even if P80 remains finite |
| Different runtime/model/input, incomplete failures, censored timeouts | Exclude or disable the history source as specified; never silently train on success-only costs |
| Duplicate callback/concurrent forecast, crash before/after provider response | One slot/reservation; persist-before-call and retained result, or UNKNOWN with conservative budget charge |
| Changed prices/policy while evaluating, expiry while awaiting Jev | No stale intent; undecided work abstains or a saved BID expires; keep evidence |
| Already admitted or possibly broadcast bid with no estimate | Recover exact existing operation; no duplicate fee or repricing |
| Identity transfer or market finality conflict | Existing L2/L4 authority and halt rules; no local reassignment of history or admitted duties |
| Duplicate report/receipt, changed-ID correction, foreign execution | Idempotent exact replay; explicit correction policy; reject mismatched scope |
| Storage or provider budget exhausted | Stop new paid work, preserve recovery records and continue admitted obligations |

## 11. Implementation plan

**Goal:** deliver L5-01–L5-10 and the observable flows above, with only private economics added to existing market participation. All exclusions in §1 apply; protocol schema/ABI/Solidity changes are out of scope.

1. Define the closed private records and reviewed golden vectors under `specs/`; implement pricing, forecast validation/fusion and policy as a few cohesive files in `modules/economics/`. Use existing JSON validation, integer/Fraction arithmetic and hashes; no numerical/dataframe dependency.
2. Extend the journal with a migration and bounded economics/history storage, atomic attempt reservations and immutable decision/report handling. Keep persistence at the storage boundary; `ExecutionHistory` performs selection/ingestion rules without an independent database service.
3. Add the optional HTTPX Jev adapter under `modules/adapters/` and deterministic injected test providers. No SDK dependency is necessary for one HTTP operation. Add the narrowly scoped reputation read to existing ports/chain/fixture adapters, preserving old public signatures and frozen ABI.
4. Compose the candidate queue, policy and durable handoff in `apps/reference_agent/chain.py`, reusing L4 filtering and `Participant.prepareBid`. Add explicit economic-mode configuration; prohibit ambiguous fixed-price-plus-policy configuration. Use the current deterministic worker in offline acceptance.
5. Add adjacent module tests, cross-boundary tests under `tests/integration/`, and `check-l5`/`demo-l5` scripts/Make targets following the existing gates. Update operating documentation with configuration, trust limits and recorded verification when implementation exists.

Implementation work plan (2026-10-06): use `modules/economics/{records,pricing,estimator,policy,history}.py`, one economics storage adapter, one Jev HTTP adapter and one agent-side decision coordinator. Extend journal migration and existing chain/fixture reputation reads; compose the coordinator in the reference runtime. Add reviewed fixture configuration/vectors, `.env.example`, an operator guide, focused tests and the L5 gate/demo. Reuse HTTPX and the standard library; no new dependencies. Verify pricing/fusion first, then crash/budget/report recovery, then local-EVM participation and all L4 regressions. Live paid Jev and deployment qualification remain disabled unless the explicit settings/evidence in §13 are supplied. Preserve the user's `.agents/` and `skills-lock.json`.

## 12. Acceptance criteria

Layer 5 implementation is complete only when the following run successfully with deterministic, isolated providers and no mandatory paid or live-network calls:

1. **Pricing and policy goldens:** applicable input/output/cache/tool charges, fixed fees, exact unit conversions/ceil, stale/missing prices, uint256/uint96 limits and zero cost. For `c=10`, `h=2`, `p=q=500000`, `marginPpm=100000`, bid is **27 atoms**; budget 26 abstains, never yields 26. Positive-score checks use L1, with no new auction formula.
2. **Forecast goldens/properties:** for costs 100/200 at 800000/200000 ppm, mean=120, P50=P80=100, P95=200. Two equal history samples 100/200 and a Jev point 300 use weights `2/22` and `20/22`, not additive cost. Cover largest-remainder ties, exact normalization boundaries, zero options, tail below/at/above 5%, and positive tail below one ppm. Property checks preserve mass, ordering, bounded output and immutable inputs.
3. **Fallbacks:** MIXTURE/HISTORY/JEV/BOUNDS/ABSTAIN, no history, incompatible/old data, complete failed/aborted jobs, censored/missing telemetry and invalid envelopes. No fabricated finite estimate when neither cost basis nor tail is available. Irrelevant tasks incur zero inference calls.
4. **Recovery and limits:** candidate acknowledgement, atomic budget reservation, crash before/after send/response/save/decision/intent, duplicate/concurrent callbacks, task/provider timeout, clock rollback, daily rollover and changed config. One provider attempt per task, no budget overspend under the declared request bound, one immutable bid intent; expiry does not erase reconciliation. Awaiting Jev cannot block an existing award's acceptance.
5. **History and evaluation:** exact and conflicting report replays, explicit corrections, expense deduplication, false provenance claims, mismatched estimate/task/agent, no-award forecast expenses, before/after-receipt ordering and unchanged settlement price. Evaluation uses saved rates and pre-run baselines, includes failed observations, exposes missing/censored data and cannot count a run twice.
6. **End-to-end offline demo:** an eligible task traverses the actual L4 callback, receives a bounded forecast with Jev disconnected, submits through the existing participant and local-EVM adapter, executes the deterministic worker, and accepts an explicitly labeled usage report linked to its estimate. Use existing L3 fixture callers for allocation/settlement. Restart/replay gives no duplicate forecast, bid or run; an uneconomic candidate abstains. A second task can use compatible history. An independent external bidder and indexer-off operation still work.
7. **Regression gate:** run `make check-l4` (including its lower-layer gates), new L5 tests/demo, relevant journal/port migration tests, Ruff lint/format and schema/ABI consistency checks. Preserve existing golden expectations, signing/escrow behavior and receipt authority. Record what actually ran; synthetic forecasts are not live calibration evidence.

A separately authorized, bounded live Jev check may qualify the pinned provider contract. It is not required for the disconnected MVP gate and must not be described as evidence of calibrated forecasting or a qualified live Monad deployment.

## 13. Open Questions

1. **Live provider contract and spending bound:** which exact Jev model, API/account pricing and enforceable maximum request charge will the operator qualify? Choice documentation establishes the probability shape, not a billing guarantee. Live paid forecasting stays disabled until these are pinned and tested; offline fixtures remain sufficient for the MVP.
2. **Runtime envelope and prices:** which real L6 worker/provider resource inventory, retry limits and verified price sources will replace the deterministic worker's declared fixture bounds? A claimed hard cap cannot be advertised as enforced until the runtime implements it. Broader workload/history matching needs a new estimator version with evidence; it is not inferred from task family.
3. **Conversion and risk settings:** which timestamped MON conversion source, maximum age, margin, success cap and overhead allowances will the operator supply? L5 defines the record/rounding and private heuristic, not authoritative exchange rates or profitable defaults. Missing settings disable economic-mode bidding.
4. **Inherited live activation:** Layer 4 still needs qualified deployment/registry/finality facts, and its full-manifest versus L3-report bootstrap question remains unresolved. L5 does not relax that gate, appoint an allocation keeper or fill pending L7 fields with placeholders.

These are live configuration/qualification or later-layer decisions. The deterministic local behavior, unavailable paths and MVP acceptance above must be implementable without silently resolving them.

## 14. Delivered implementation and acceptance evidence

The implementation follows §11 with no new dependencies or protocol/schema/ABI changes:

- [`modules/economics/`](../modules/economics/) implements exact resource pricing and conversion, joint-scenario/history fusion, conservative fallbacks, private policy, attributed usage and saved-rate evaluation.
- [`EconomicRuntime`](../modules/agent_client/economics.py) composes durable candidate processing with L4 discovery and the existing participant. Read-only reputation snapshots come from the existing fixture/chain adapters. Paid forecasting runs in a bounded background task; signing rechecks expiry, configuration, clock and persistent halt.
- [`EconomicsStore`](../modules/adapters/storage/economics.py) uses journal migration v3 for bounded private records, atomic spending reservations, immutable decisions, report corrections and receipt annotations. Started requests without retained responses become UNKNOWN on restart; their full allowance stays reserved.
- [`JevPredictor`](../modules/adapters/jev.py) implements the documented TypeSafe Choice HTTP boundary with pinned versions, exact probability decoding, request/response limits, timeout and no retries. [`.env.example`](../.env.example) documents `TYPESAFE_API_KEY`, runtime configuration and keystore environment variables. [The operation guide](../docs/layer-5.md) defines the complete configuration and producer interfaces.

Executed on 2026-10-07:

| Check | Result |
|---|---|
| `make check-l5` | Passed; 67 L5 tests, no failures or skips; requirement mapping in `.scratch/layer5/gate.json` |
| Included `check-l4` and lower gates | Passed; 48 L4 tests, service/indexer integration, L3 contract/conformance/coverage checks and L0–L2 gates |
| Included `scripts/demo_layer5.py` | Passed over the real local EVM: BID and ABSTAIN, restart without duplicate bid/run, one worker invocation, estimate-linked usage, validator-timeout receipt, later HISTORY forecast; zero paid requests and indexer/hints disabled |
| Ruff lint and format, `git diff --check` | Passed |
| Documentation links and CLI help | Local links resolve; reference-agent entry point accepts `--config` or `AGENTLANCE_CONFIG` |

The L5 suite reports two upstream aiohttp deprecation warnings concerning Python's already-fixed connection cleanup; neither is a skipped check. No live Jev request, testnet deployment or calibration claim is part of this evidence. Arbitrary `PROVIDER_ATTESTED` uploads are rejected: this MVP accepts operator-reported/incomplete telemetry and requires an actual provider-evidence verifier before that stronger provenance can be admitted. Resource envelopes remain operator declarations until L6 enforces them; L7 owns automatic receipt/usage collection. The open questions in §13 remain unchanged.
