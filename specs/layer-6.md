# Layer 6 — Awarded execution and bounded delegation

**Status:** implemented against merged Layers 0–5 at `4161876`; acceptance evidence is recorded in §13. Live qualification remains separate. Requirements **L6-01–L6-09** in [the layer plan](../layers.md) govern this work. [Protocol semantics](protocol.md), [canonical schemas](schemas/protocol.schema.json), [deployment policy](deployment-policy.md) and [Layer 5](layer-5.md) remain authoritative.

## 1. Purpose and smallest complete scope

Turn a finalized accepted award into one durable, bounded execution, an immutable result commitment, and attributed usage. A manager may publish funded child tasks, wait for their independently settled results, and complete missing work locally within its original limits.

The MVP supports one configured identity per process, multiple locally reserved obligations, a solo worker, and a manager with independent parallel children. Use the selected **Python + OpenAI Agents SDK + Docker + A2A** stack. Keep the deterministic worker as the disconnected executor and test the actual SDK boundary with an isolated model implementation. Live model credentials are not required for acceptance.

| L6 owns | Existing or later owner |
|---|---|
| Capacity reservations before bidding; per-run resource and spending limits | L4 eligibility/discovery; L5 forecasts and immutable BID/ABSTAIN decisions |
| Durable execution steps, local cancellation and recovery, bounded model/tool adapters | L2 award authorization, A2A correlation and artifact identity; L4 finality and transaction recovery |
| Child-plan validation, funded publication, observation, artifact consumption and fallback | L3 winner selection, escrow, deadlines, reputation and settlement |
| Metered execution observations and durable delivery to L5 | L5 report ingestion, corrections and forecast evaluation; L7 invoice verification, outcome reconciliation and feedback export |

No contract/schema/ABI changes, new task family, general workflow engine, cross-host scheduler, arbitrary sibling dependency graph, automatic identity creation, model-selected signer/validator, or public execution API. SDK specialists are not registered market agents. Reflection is disabled in v1; a small structured private run history satisfies L6-09 without additional inference.

## 2. Inspected Layer 5 baseline and reuse

- [`Participant`](../modules/agent_client/participant.py) already verifies frozen winning bids, digest-checks supported inputs, accepts awards, claims execution and commits results through durable intents. `publishChild(parentRef, terms, requestId)` already funds `createChildTask`; it is not yet a delegation coordinator.
- Execution is currently `execute(raw) -> bytes` inside `advanceRun` while the participant lock is held. Extend this boundary for asynchronous jobs; do not run a model/container or wait for children while holding the participant or native-transaction lock. Preserve the synchronous deterministic API for existing L2 tests.
- [`Journal`](../modules/adapters/storage/journal.py) v3 owns identity/session binding, exclusive database access, immutable start claims, content and transaction state. Its constructor currently converts every `STARTED` execution to `INTERRUPTED`. L6 must explicitly distinguish managed runs from legacy runs before permitting coordinator recovery; never clear the original execution claim.
- [`ContentStore`](../modules/adapters/storage/content.py) publishes result bytes atomically with the immutable artifact. Extend its content publication narrowly for child input bytes; do not fabricate an `ExecutionRef` or use `publishResult` before an award.
- [`EconomicRuntime`](../modules/agent_client/economics.py) already runs paid forecasting in background work. Its capacity check and [`DiscoveryRuntime`](../apps/reference_agent/chain.py)'s check currently count execution rows only; neither reserves space for outstanding bids. Both must consult the same L6 reservation ledger.
- Reuse [`validateChildEnvelope`](../modules/market_core/accounting.py), canonical term validation, finalized task views, [`MarketWatcher`](../modules/agent_client/discovery.py), and [`MonadMarket`](../modules/adapters/chain/market.py)'s signed-byte/nonce recovery. L1 previews are checks, not a replacement for direct chain preflight.
- [`ExecutionHistory.recordUsage`](../modules/economics/history.py) already checks own-award attribution and estimate/runtime binding. Reuse its exact context, report correction and receipt interfaces; do not create another history/calibration database.

## 3. Boundaries and interfaces

Keep domain execution rules in `modules/execution/`, persistence/process/provider boundaries under `modules/adapters/`, and composition in the existing reference application. A coordinator plus a few pure planning/limit functions is sufficient; no service per responsibility.

| Interface | Inputs → outputs and ownership |
|---|---|
| `reserveBid(task, bidAtoms)` | Trusted task and unchanged offer, coordinator-pinned configuration and retained optional L5 decision → existing/new capacity reservation or `CAPACITY_UNAVAILABLE`; durable before a bid can be signed |
| `observeReservations()` | Retained reservations, qualified canonical state and existing L4 operations → hold, convert to own award, or release; no inference |
| `startExecution(executionRef, raw)` / `tick()` | Existing participant obligation → bounded coordinator progress; launch once after finalized acceptance; return promptly while external work continues |
| `runStep(executionRef, stepId, request)` | Bound run/step IDs, verified bytes, explicit limits and allowed tools → exact output bytes plus usage, or typed failure/unknown outcome; no signer/market access |
| `validateChildPlan(plan, view, profile, schema, signer, pending)` | Untrusted proposed plan plus canonical parent and local reservations → immutable validated plan or reason; no publication |
| `advanceChildren(executionRef)` | Persisted child intents and L4 observations → terminal receipts, digest-verified successful artifacts, or missing-work entries |
| `queueUsage(executionRef)` / `flushUsage()` | Durable metered observations → canonical `CostReport` and L5's existing `recordUsage(report, context, supersedesReportId)`; replay-safe local delivery |

L5 supplies an optional `estimateId`, saved configuration and decision; a forecast is not authority to execute. L6 consumes the exact L5 runtime cost profile and adds an execution configuration digest to its existing `configDigest` binding. The complete pre-bid runtime snapshot must cover planning, tools, fallback and synthesis, not just a worker's happy path. Changed configuration applies only to new bids; retained obligations use their pinned snapshot.

The child coordinator needs narrowly scoped `allocateTask(command, operationId)` and `expireTask(command, operationId)` wrappers in the existing market adapters/participant intent dispatch. These call existing L3 commands with zero value. Extend native preflight explicitly: allocation has `[biddingClose, allocationBy)`; expiry has the current state's lower deadline and no fabricated upper deadline. Do not reuse the present submission-only `< deadline` test unchanged. Use stable operation scopes, existing nonce serialization, bounded gas and finalized reconciliation. No `settleVerdict` or withdrawal authority is exposed to the executor.

L7 receives immutable artifacts and usage observations; it owns real evaluation, corrected billing and exported evidence. L8 can read an authorized projection of run state. A2A remains a hint/status transport; child work is never assigned by directly invoking a preferred endpoint.

## 4. Private records and state

Add versioned, closed private records and one journal migration, preserving existing rows/claims/operations. Reuse canonical references, decimal-string amounts/timestamps and L5's sorted-record digest convention. Scope every key by journal identity plus full chain/market/task/award reference. Retain the configuration and bytes needed to reproduce each digest.

| Record | Required fields/meaning |
|---|---|
| Execution profile | `version`, L5 `runtime` snapshot, executor kind (`DETERMINISTIC`/`AGENTS`), exact model and prompt/tool/image versions, supported schema/policy pairs, `capacity`, limits, delegation policy and explicit cost bearer. Credentials are references to host environment variables, never record contents |
| Limits | Positive wall/request/CPU/memory/PID/workspace/output/log bounds; integer model-turn/model-call/tool-call caps; disjoint resource-quantity maxima matching L5's inventory; maximum execution charge/unit/source/expiry; bounded read/usage-delivery attempts and delay; storage row/byte limits. Unknown billable resources disable that executor |
| Delegation policy | Enabled flag, fixed `ownWorkReserveAtoms`, maximum child count (≤4), total fresh-deposit allowance, child-progress gas allowance, child publication cutoff, synthesis/commit margins, allowed child template pairs, fallback enabled flag. Automatic child retries are **zero** in v1; children published by the reference manager are leaves (`maxChildren=0`) |
| Capacity reservation | Stable ID, TaskRef, decision/estimate IDs or null, pinned profile digest/bytes, bid-operation link, state, optional ExecutionRef, creation time, release reason and canonical evidence |
| Managed run | ExecutionRef, pinned task/winning bid/profile, capacity reservation, unchanged execution claim, start/cutoff times, current stage, immutable plan ID or null, aggregate resource/spend ledger, final artifact or null, usage-delivery state and diagnostic |
| Step | `(ExecutionRef, stepId)`, kind (`PLAN`, `SOLO`, `FALLBACK`, `SYNTHESIZE`, allowlisted tool), request/config/input digests, reserved resource/fee maxima, state, provider/container reference, exact output digest, usage with nullable unknowns and diagnostic |
| Child plan | Version, parent ExecutionRef, profile digest, mode `SOLO`/`DELEGATE`; ≤4 stable ordered slots. Each slot has a slot ID, complete canonical `TaskTerms`, exact published input/schema/policy refs, and the local fallback input binding. No winner, signer, child TaskRef or outcome may be supplied by the model |
| Child progress | Plan/slot ID, deterministic publication request ID, existing operation ID, canonical child TaskRef once observed, matching parent/root references, latest qualified view/receipt, consumed artifact digest or missing-work reason |
| Usage outbox | ExecutionRef, immutable report/context and correction predecessor, local report scope/source references, pending/delivered state, retry count/next time. L5 owns selected-report pointers; L6 does not duplicate them |

Capacity progresses `HELD → COMMITTED → RELEASED`: HELD includes a reserved candidate and any possibly sent bid; COMMITTED means the canonical own award. Execution stages are `ADMITTED → ACCEPT_PENDING → READY → WORKING → [WAITING_CHILDREN → SYNTHESIZING] → ARTIFACT_READY → RESULT_PENDING → RESULT_RECORDED`, with local `STOPPED`/`INTERRUPTED` exits. Store planning/child-wait/synthesis stages in the managed-run record; the original execution row stays `STARTED` until artifact publication, preserving `storeResult`'s claim/phase check. Stages refine the existing journal, not a second market lifecycle. In A2A these stages remain `WORKING`; artifact availability remains `COMPLETED`, which does not mean payment.

Steps progress `PREPARED → STARTED → COMPLETE | FAILED | UNKNOWN`. Persist STARTED and resource/fee reservations before dispatch. COMPLETE freezes output and usage. UNKNOWN means an external effect or charge may have occurred; it is not a retryable zero-cost failure.

## 5. Capacity and admission — L6-01–L6-04

1. Reserve one slot atomically before any new bid intent, in economic and fixed-price modes. The L4 capacity snapshot remains a cheap early check. The common participant signing path enforces the reservation again, so concurrent candidates cannot bypass it. Failure leaves the L5 decision unchanged and records a local submission diagnostic; no signature/transaction is created.
2. A slot covers the whole obligation, including child waiting and synthesis. Remote children consume their own operators' slots, not extra parent slots. Do not release on bidding-close, a missing index entry, a lost response or a timer while a bid/award may still exist.
3. Release an unsubmitted reservation after proving there is no retained/signed intent and the candidate has ended. Release a submitted losing bid only after a finalized allocation/terminal view proves no own duty; winning bids convert to COMMITTED. An unresolved operation holds its slot until L4 establishes its outcome.
4. A `RESULT_RECORDED`, `STOPPED` or `INTERRUPTED` run may release compute capacity only after all local processes are confirmed stopped and no further execution can start. Preserve result/child transaction and usage-reconciliation records independently. A parent waiting to synthesize or confirm its result must retain its slot.
5. Reconstruct reservations for retained bids/awards before considering new tasks on restart. If imported legacy obligations exceed capacity, stop new bids and surface overcommit; never erase an admitted duty. Do not automatically accept additional unmanaged awards without a slot. Missing keys/configuration remain visible operational failures and may cause the existing no-show/timeout outcome.

Before bidding, validate the executor/image/tool availability, full limits and ability to retain required state without model calls. Require configured own-work reserve ≤ offered bid in delegation mode, so the protocol's `bid ≤ reserved price` guarantees that reserve can be accepted. This is an execution admission constraint; L6 never increases/replaces L5's amount. Solo mode uses reserve zero and publishes no children. Legacy RUNNING obligations retain their canonical reserve and are never accepted again.

Before acceptance, bind the fixed reserve into the existing execution row/intent. Before the original start claim and every new paid step, re-read the finalized own award: frozen AgentRef/signer, exact TaskSpec/input/profile, RUNNING state, matching reserve, no persistent halt and remaining deadline. Ownership transfer does not reassign an admitted obligation. A2A retries and SDK session IDs cannot establish a second run.

## 6. Bounded execution and isolation — L6-02–L6-04, L6-09

The host owns signing, the journal, resource accounting and orchestration. Run the trusted Agents SDK adapter asynchronously with a finite loop and only locally registered function tools. Model output can propose structured data; it cannot call the market, access keystores, select a provider URL, install packages or increase a limit. Disable SDK handoffs/agents-as-tools in this MVP so they cannot bypass the market or resource ledger. The [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) is an application-owned runtime; use its [runner](https://developers.openai.com/api/docs/guides/agents/running-agents) behind AgentLance's durable step boundary, not as the source of economic identity.

Model steps run only after finalized acceptance. This v1 adds no paid pre-award planning or reflection; L5's separately budgeted Jev call is unchanged. Pin `openai-agents` and its resolved dependencies in `uv.lock` during implementation. Add `OPENAI_API_KEY` to the existing environment example when the adapter is implemented; `TYPESAFE_API_KEY` remains solely L5's credential. Require an explicit model/configuration and validated price/charge bounds, with no silent model substitution. Disable external trace export by default: SDK tracing is normally enabled, so configure it explicitly rather than assuming private history stays local. [Official tracing controls](https://developers.openai.com/api/docs/guides/agents/integrations-observability).

Docker executes local task/tool code in a pinned, prebuilt image. Require a non-root user, read-only root filesystem, dropped capabilities, no privileged/host namespaces, no Docker socket or signing/database mounts, network disabled, and explicit CPU/memory/PID/tmpfs/output/log limits. Inputs are read-only exact bytes; scratch/output belong only to this step. Reject traversal, symlink escapes and oversized output when importing artifacts. No model-supplied shell command runs on the host. The SDK's host-side tools may invoke only this adapter's allowlisted operations; the model API credential never enters a task-code container. Enforce limits outside the process and fail unavailable if the selected platform cannot enforce them. Docker is a disclosed local isolation dependency, not proof of isolation against every kernel vulnerability.

Before every provider/tool request, atomically reserve its maximum quantity vector and known maximum charge against the run's remaining allowance. Count planning, unsuccessful generation, tool calls, synthesis and fallback in the same own-execution budget. Reuse L5 pricing/unit arithmetic; do not count cached/uncached or reasoning/output quantities twice. Enforce model request/output/turn limits in the provider adapter as well as wall time; SDK turn limits alone are not a token or money cap. Unknown charge bounds prohibit the call. Remote cancellation does not prove a refund: retain the full reservation on timeout/crash/ambiguous usage. Do not label a declared L5 envelope enforced unless every permitted resource and call path is covered by these controls.

Use a monotonic timer while running plus persisted UTC and canonical chain cutoffs on restart. The effective cutoff is the earlier of configured wall allowance and `resultBy − commitMarginSeconds`; child plans must also leave configured synthesis time. Clock rollback/stale chain time prevents new paid steps, never extends an allowance. A watchdog terminates local processes at cutoff even if RPC/provider calls are stalled. Reads poll no faster than two seconds and have at most ten seconds per request. Do not block award acceptance, event scans or native reconciliation behind an awaiting model/container.

One identity/journal/history/configuration per reference process is sufficient. Distinct processes use distinct execution signers; the operator must not concurrently use the same signer through another journal or host. Shared owner/payout accounts must be disclosed. Cross-host nonce/capacity coordination is excluded. Keep bounded step summaries and exact artifacts; no unbounded prompt log, vector memory, shared experience pool or automatic policy rewriting.

## 7. Delegation and fallback — L6-05–L6-07

After acceptance, execute at most one PLAN step. The planner can be the deterministic fixture or the configured bounded SDK runner. Persist its raw output, validate the whole proposal, and freeze one plan before publishing any child. Invalid/no plan uses the configured solo fallback if it still fits the retained limits, otherwise stops. A crash cannot silently produce a different plan or mint new publication IDs.

For each proposed child, check the exact committed input, locally allowed OutputShape/ValidationPolicy pair, policy digest binding, supported `structured-output-v1` family, native MON, positive uint96 budget, reduced positive alpha, pinned deployment validator and address-role restrictions. The host fixes refund address to the configured manager funding account; task text cannot redirect it. No model-provided identity, validator, family or executable template is trusted. Publish immutable bytes before the market command; do not change URIs/terms after an intent exists.

Validate all siblings together, then refresh and validate immediately before each publication:

```text
outstanding canonical child budgets
  + committed canonical child payouts
  + ownWorkReserveAtoms
  + budgets of locally pending creations not yet reflected on chain
  + budgets proposed next
<= parent's reservedAtoms
```

Deduplicate pending creations when their finalized TaskCreated event enters the view. The manager also needs actual spendable signer funds for **fresh full deposits and bounded transaction gas**. Reserve the entire planned batch locally before publication; per-parent envelope checks cannot protect one wallet from competing runs. Keep a separate gross-deposit allowance: an escrow refund/withdrawal credit is not available wallet balance and does not automatically replenish it. Serialize publication using the existing sender lock. No parent escrow advance, automatic withdrawal, or second deposit after an ambiguous send.

Every child must satisfy the protocol's complete deadline order, acceptance gap ≥120 seconds, `child.validationBy + 120 ≤ parent.resultBy`, remaining local synthesis/confirmation margins, inherited absolute maxDepth ≤2, direct-child lifetime limit ≤4 and no stronger permissions than its parent. The reference manager's children have `maxChildren=0`; it never performs recursive replanning. `retryOf=null` for new slots. Lower-layer retry commands remain available to other agents, but L6 does not automatically spend another child slot/deposit on a failed task.

Derive publication `requestId` from a versioned label, parent ExecutionRef, frozen plan digest and slot ID; use the existing `Participant.publishChild`. Obtain TaskRef only from the reconciled canonical TaskCreated event. A PENDING/UNKNOWN operation is the same child attempt forever. If a batch partially publishes, keep those children and abandon only provably unsent slots; no compensating cancellation or change to funded terms.

Observe children with the same watcher/direct reads as any external task. Parallel means independent child markets can run concurrently; it does not bypass the signer's existing transaction serialization. A2A hints target only the canonical winner through L4's existing mechanism and remain optional. The manager neither chooses a worker nor calls one to execute before acceptance.

For its recorded children only, the coordinator may submit bounded permissionless allocation during the allocation window and expiry when the current canonical cutoff passes. Reserve/count that gas separately; a competing caller winning the race is resolved by re-reading canonical state. A new phase may require a different expiry operation scope, but never unbounded retries. Root allocation and real validator operation remain outside this coordinator. These scoped progress calls make unallocated/failed children terminal without introducing a registry-wide keeper.

Consume a child artifact only when a finalized SUCCESS receipt, matching child result commitment, awarded identity, parent/root linkage, validation-policy binding and fetched digest all agree. Structural checks before synthesis are not a validator verdict. A2A COMPLETED, an unfinalized PASS or a submitted-but-unsettled artifact is insufficient. Successful output that cannot be fetched is missing work; its payment is still final.

| Child outcome | Parent action |
|---|---|
| All successful, artifacts available | Synthesize from the bound artifacts within remaining own limits |
| No award / failed / no-show / timeout / validator timeout / unavailable successful artifact | Keep valid outputs; execute missing slots locally if the frozen fallback policy, budget and time permit; otherwise stop |
| Some creation operations ambiguous | Reconcile them; do not republish or count their slots/deposits as free |
| Child deadline passed but not settled | Progress expiry or wait; do not infer terminality from the clock |
| Parent expires/stops while children remain | Stop parent inference and new child creation; retain funded child monitoring/eligible expiry and transaction recovery. Children keep their independent payment rights |

A fallback is a new, explicitly bounded step in the same parent run, not a new funded task or a reset budget. It may compute missing work while terminal observations are pending, but the parent cannot submit until direct-child `activeChildren==0` in a fresh canonical view. Do not use failed child content as validated input. Final synthesis runs at most once; exhausted/unknown synthesis does not trigger another run under a different ID.

## 8. Artifacts, usage and recovery — L6-01, L6-08–L6-09

Persist completed step outputs before advancing. Validate final bytes against the pinned output shape and size/depth/node limits, then use the existing atomic `publishResult`/`storeResult` path. These checks establish artifact structure, not SUCCESS. Commit the identical ContentRef through the existing result intent only while RUNNING, timely, and with no active direct children. On-chain submission/receipt recovery always precedes any attempt to repeat work. A terminal or conflicting result is never overwritten.

The immutable execution claim means **one logical run**, not one uncheckpointed coroutine forever. Atomically associate managed-run ownership with the original start claim, so journal startup can distinguish it from legacy STARTED rows. Managed-run recovery may continue from durable completed stages, observe children, or submit a saved artifact without re-claiming execution. It may dispatch a PREPARED step only when STARTED was never committed. A STARTED step without an atomically retained result becomes UNKNOWN; never resend a model/tool request based solely on a timeout or SDK session. V1 performs no automatic paid step retries. Stop that execution path with incomplete telemetry unless an already saved alternative step/output makes continuation safe. Legacy STARTED rows retain L2's existing INTERRUPTED behavior.

Retain named/labeled containers until their bounded output/status is persisted. Restart reconciles a known container by its bound run/step identifier; it never creates another instance for that STARTED step. If output cannot be recovered unambiguously, terminate any surviving container, mark UNKNOWN and keep charges reserved. A new child or a changed SDK session ID cannot bypass this rule.

Meter each billable execution operation with stable expense IDs derived from run/step/resource and record all declared resources, including explicit known zero quantities. Produce usage for completed, aborted, timed-out and never-started awarded runs. Missing telemetry is INCOMPLETE/CENSORED with nulls, not zero. Runtime status `COMPLETED` means local artifact production, not validator success. A deadline-driven never-started award uses L5's `TIMED_OUT` status with `zeroUsage=true` and an INCOMPLETE empty report: the durable ledger proves that no local call started, while the incomplete marker prevents it from becoming an execution-cost history sample.

Deliver using L5's exact context `{operator, agentRef, task, runtime, status, zeroUsage, evidence}`. Use `FIXTURE` for synthetic/local demo evidence and `LOCAL` for qualified live runtime observations according to the journal mode. Attach the original estimate only when its runtime/task binding matches; old fixed-price obligations can use null. Preserve an explicit own-EXECUTION report scope in the local outbox. Known metered quantities and catalog-derived charges are OPERATOR_REPORTED with their pricing/source records; they are not authenticated provider invoices. Unknown actual costs stay unknown even when a conservative reservation is finite.

FORECAST expenses stay under L5's attempt ledger; GAS, VALIDATION and CHILD_PAYMENT remain separate from own EXECUTION. Do not copy child workers' provider costs into the manager's history or describe a child deposit as a paid expense. Canonical paid/refunded amounts and transaction costs may later be added by L7 through explicit corrections; the initial own-execution report must not claim complete whole-agent profitability. A partial report cannot train history as a complete sample.

Freeze each outbox report before sending it. Retrying delivery uses identical report/context bytes and ID; corrections name the currently selected report and use a new ID under L5's rules. Delivery is independent of result publication/settlement. Bounded automatic delivery attempts leave a visible pending entry when exhausted; an explicit operator/L7 retry can deliver the same entry later. Storage exhaustion stops new bids/steps, preserves obligations and does not hold a result hostage to analytics. Reserve enough journal/output space before admitting a run to retain its worst-case bounded records.

## 9. Invariants and expected failures

- No execution model/tool call before finalized own acceptance; no duplicate logical claim, paid step dispatch, child deposit or result commitment on replay.
- Outstanding reservations plus active obligations never exceed configured capacity during normal admission. Unknown bid/step outcomes cannot free capacity or spending allowances prematurely.
- Every model/tool path is charged to the pinned runtime inventory; resource, fee and wall allowances do not reset on fallback/restart/config changes.
- Chain escrow, child envelope and local spendable funds remain distinct. Parent failure never claws back child payment.
- Only canonical child SUCCESS with exact available bytes qualifies as a validated delegated output. The executor has no validator key or success-writing API.
- Frozen bids, plans, intents, artifacts and report versions are immutable. Schema/profile transfers and provider changes cannot reinterpret existing duties.
- A finality conflict retains L4's persistent halt, blocks new paid work/transactions, and stops local workers safely; unknown remote charges remain accounted for. Ordinary outages remain operational errors, never synthetic FAIL verdicts.

Also cover unsupported templates, malformed/oversized plans, duplicate slot IDs, exhausted lifetime child slots, zero envelope, insufficient wallet funds, deadline equality, unavailable images/models, output import attacks, hung/oversized tools, provider ambiguity, storage backpressure, stale/changed configuration, identity transfer, receipt-before-artifact, and receipts arriving after parent timeout. Each has a visible local reason and bounded behavior; none creates a new protocol settlement reason.

## 10. Implementation plan and files

Goal: implement L6-01–L6-09 and the offline acceptance below without changing protocol schemas, contracts or L5 bid arithmetic. Reuse assessment is in §2. The optional coordinator owns reservations and managed runs; existing participant/native adapters retain authority. New code is limited to execution rules/coordinator, SQLite records, Docker/SDK adapters, reference composition, fixtures and gates. `openai-agents` is the only new direct dependency, necessary to exercise the selected SDK rather than emulate it.

Implement and verify in this order: private records/migration and pure limits; common admission and asynchronous recovery; constrained executors; funded child progress and fallback; usage outbox and app configuration; real boundary demos and all inherited checks. Test success, ambiguous effects, concurrency, deadline equality, authority changes, bounded failure and restart at each boundary. Preserve the synchronous L2 API and its legacy interruption semantics. Completion means the requirement mapping in §11 passes without skips and final diff/format checks pass. Live provider qualification, root keepers and validator operation remain the explicit decisions in §12, not fabricated defaults.

### Sequence

1. Define closed private records, the fixed reference templates and golden capacity/envelope/recovery cases under `specs/fixtures/layer-6/`. Keep the frozen protocol schema untouched. Add pure limit/plan validation in a small `modules/execution/` package.
2. Add one versioned journal migration and adjacent storage tests for reservations, managed runs, steps, child links and usage outbox. Preserve L2/L4/L5 claims, pending operations, content and halt flags. Explicitly test legacy versus managed STARTED recovery.
3. Extend `Participant` with an optional asynchronous executor/coordinator boundary and shared pre-bid reservation check. Compose it in `apps/reference_agent/chain.py`; feed availability to both L4 and L5. Keep legacy fixture mode compatible and label it separately.
4. Implement the Docker execution adapter and the bounded Agents SDK adapter using the existing Python dependency workflow. Use the Docker CLI through bounded subprocesses rather than adding a second container client unless necessary. Add exactly the required SDK dependency; no alternate orchestration framework or independent SDK database.
5. Add child input publication and the two scoped market-progress wrappers to existing ports/adapters. Implement frozen-plan publication, canonical child consumption, fallback and synthesis with existing L3/L4 transaction recovery.
6. Produce execution reports through L5's ingestion API with durable local delivery. Add an operator guide, configuration example and `OPENAI_API_KEY` entry during implementation. Do not change L5 bid arithmetic or introduce a second provider-cost oracle.
7. Add `scripts/check_layer6.py`, `scripts/demo_layer6.py` and Make targets following existing gates. Tests belong beside pure modules/adapters and under `tests/integration/` when crossing real boundaries. Update this spec with actual executed evidence only after those checks run.

## 11. Acceptance criteria

Completion requires all **L6-01–L6-09**, meaningful deterministic failure tests, and the real local boundaries below; a configured but unused SDK/container is insufficient.

| Requirement | Acceptance evidence below |
|---|---|
| L6-01 — authorized, durable execution | 1, 3, 5 |
| L6-02 — identity/authority isolation | 1, 2, 5 |
| L6-03 — accepted-work inference only | 2, 3 |
| L6-04 — capacity and resource limits | 1, 2, 3 |
| L6-05 — validated funded decomposition | 4, 5 |
| L6-06 — public child markets and validated results | 5, 6 |
| L6-07 — bounded fallback | 3, 6 |
| L6-08 — immutable result and complete usage accounting | 5, 7 |
| L6-09 — bounded private history | 7; no reflection on ignored tasks |

1. **Admission/capacity:** two concurrent candidates with one slot produce at most one bid intent; outstanding bids hold capacity; finalized loss releases it; unknown transaction/expiry/config change does not release it early. Restart/adopt old bids and RUNNING awards without another signature/acceptance/execution. Demonstrate separate identities/journals cannot read each other's private data or reuse authority.
2. **Real execution boundaries:** run the deterministic fixture inside a real limited Docker container and exercise the actual Agents SDK runner against an isolated model implementation, including a bounded tool call. Prove unaccepted/irrelevant tasks incur zero execution calls. Test provider/turn/resource/charge/wall limits, memory/process/output/log bounds, container failure and safe filesystem import. Routine tests make no paid requests.
3. **Recovery:** inject crashes before/after execution claim, step STARTED, response/output persistence, plan freeze, each child publication, final artifact and result intent. Resume completed stages without repeating external effects; ambiguous paid work stays UNKNOWN. Awaiting model/children cannot block a different award's acceptance or native reconciliation. Restart must not reset wall/resource budgets.
4. **Delegation arithmetic:** use protocol golden rules including `P=100`, own reserve `20`, fresh child budgets `30+40`; payouts `25+35` leave envelope `20` and lifetime slots consumed. Outstanding local publications count exactly once. Test invalid templates/roles, changed plans, depth/child caps, deadline boundaries, insufficient fresh funds, partial batch publication and concurrent parent runs sharing signer funds.
5. **Solo and two-child local-EVM demos:** use actual L4 discovery, L5 economic-mode bids, native transactions and the new executor. For a minimal objectively checkable task, parent input/output is `{"left":7,"right":9}` with two `equalsInput` predicates; child inputs are existing `{"value":7}` and `{"value":9}` copy tasks. Synthesis constructs the parent shape from the two validated child values. Publish the new parent's exact shape/policy bytes before bidding; keep children on the existing template. Use at least two worker processes with separate signer/history state and one independently operating bidder; no in-process direct dispatch to a chosen child worker.
6. **Failure and fallback demos:** an unallocated child becomes canonically terminal, missing work completes locally within budget, and the parent can submit. Also demonstrate a failed/validator-timeout child, successful-but-unavailable artifact, resource exhaustion, and a parent failure after a child SUCCESS: the child's credit remains intact. Parent submit waits for all direct children; late child settlement still updates its own ledger.
7. **Evidence/usage:** successful, failed planning/synthesis, tool failure, timeout and unknown provider responses all produce correctly attributed observations. Exact outbox replay is harmless; corrections follow L5; execution scope excludes transfers and double-counted forecast fees. Use saved-rate L5 evaluation without rewriting bids/results. No provider-attested or calibrated claim from test telemetry.
8. **Regression gate:** `make check-l6` runs `make check-l5` and all inherited gates, new pure/property/provider tests, real container/local-EVM scenarios, SDK adapter tests, journal migration, Ruff and format/diff checks. No required check may silently skip for missing Docker/SDK/toolchain. Reports include requirement-to-test mapping and distinguish deterministic SDK/model evidence from a live model run. L3 fixture callers may allocate roots and sign fixture verdicts; no L6 production validator is introduced.

A separately configured, cost-bounded live model smoke run can qualify a provider integration; it is not required by the offline gate and does not qualify a live Monad deployment or establish task correctness beyond the pinned validator policy.

## 12. Open Questions and live activation gates

1. **Execution provider qualification:** which exact live model, tokenizer/request bounds, billing source and maximum request charge will the operator qualify? The SDK/client versions are pinned in §13; qualify the live configuration explicitly; do not infer a hard cap from average token usage. The injected model and declared deterministic resource inventory define the offline gate.
2. **Delegation economics:** which own-work reserve, fresh child deposits, risk tolerance and scoped-progress gas allowance suit real tasks? All are explicit operator settings. L5 currently prices solo own execution; optimizing delegated profit or probabilistic child availability needs a later, separately versioned policy. L6 must not silently add it.
3. **Real work and isolation:** which additional schema/policy pairs, tool images and platform-enforced resource limits will be approved beyond the structured-copy reference templates? Unsupported pairs remain unavailable; a different validation family is not an L6 shortcut.
4. **Live liveness and authority:** who operates root allocation/expiry and the immutable validator, and how is the full qualified deployment manifest supplied? L6's scoped child progression does not resolve the L4 bootstrap question or L7's validator/evidence responsibilities. No placeholder addresses or self-validation fill this gap.

These questions gate broader/live configuration, not the specified offline implementation. No live deployment, credentials, provider qualification or validator service is included in this layer.

## 13. Implementation and executed acceptance

The optional coordinator is composed in `apps/reference_agent/chain.py`. Journal v4 retains reservations, immutable execution claims, step checkpoints, child publication/progress records and usage outboxes. The existing participant and native adapter continue to own authorization, signing and transaction recovery. The SDK adapter uses `openai-agents==0.17.4` with the compatible `openai==2.36.0` client; live calls require the explicit OpenAI provider/key and fixed official endpoint. The Docker worker remains the reviewed structured-copy implementation. See [the operation guide](../docs/layer-6.md) for configuration and recovery.

Executed on 2026-10-08 against merged Layers 0–5 at `4161876`:

- `make check-l6` passed all inherited gates through Layer 5, then **40 Layer 6 tests with zero failures or skips**. The requirement mapping is recorded in `.scratch/layer6/gate.json`; the suite covers bounded provider errors, the pinned endpoint, wall cancellation, concurrent parent funding reservations, crash recovery, child failures and late settlement. Eleven upstream aiohttp connection-cleanup deprecation warnings do not represent skipped checks.
- The separate real-boundary demo passed solo execution, two independently funded public child markets and unallocated-child fallback on fresh local EVMs. It used three independent bidder processes, the actual Agents SDK runner with an isolated model, and the constrained Docker tool. `paidModelCalls` remained zero.
- Review corrected the never-started usage boundary to preserve Layer 5's frozen status catalog: it now delivers a `TIMED_OUT`, `zeroUsage=true`, INCOMPLETE observation that cannot train cost history. The successful Docker isolation probe also has a dedicated cold-start allowance; production timeout limits remain unchanged.
- Ruff lint, format, `git diff --check`, example-profile/digest checks and all local Markdown links passed. No contract, ABI or canonical protocol schema changed.

Local evidence is retained under ignored `.scratch/layer6/`: `gate.json`, `pytest.xml`, `pytest.log` and the demo directory referenced by `demo.log`. These are local execution records, not deployment manifests. No paid model requests, live provider qualification, testnet deployment or production validator operation were performed. SDK evidence uses the actual runner with an isolated model/HTTP boundary; child settlement uses the existing fixture validator authority. The live decisions in §12 remain open.
