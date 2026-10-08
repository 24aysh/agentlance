# Layer 8 — Requester commands, public inspection and interoperability

**Status:** **Layer 8 non-frontend offline scope complete**, verified on 2026-10-08 in the working tree based on the merged Layer 7 baseline `2a29c42`; see §11 for executed acceptance. Frontend delivery and live activation remain separate. This is the **non-frontend scope** requested for Layer 8. Use the repository's existing `specs/` directory. [The layer plan](../layers.md#l8--application-explorer-and-demonstrable-interoperability), [protocol](protocol.md), [deployment policy](deployment-policy.md), [canonical schemas](schemas/protocol.schema.json) and [Layer 7](layer-7.md) remain authoritative.

## 1. Purpose and scope

Make the completed protocol usable and inspectable end to end without a browser: a requester can create a funded task, progress or cancel it where authorized, inspect its bids and task tree, trace validation and feedback, and withdraw available credits. An external example agent must join through the published interfaces without changes to AgentLance's market or runtime.

Deliver one Python CLI with reusable application functions, small extensions to existing adapters, a standalone interoperability example, and a reproducible acceptance demo. Interpret L8-01–L8-07's display requirements as structured JSON and concise CLI explanations. A later frontend consumes these interfaces; it does not supply missing economic behavior.

**Excluded:** `apps/web`, Next.js, wallet-connect UI, dashboards, charts, browser flows and styling. Also exclude a new hosted API, authentication service, agent fleet manager, automatic winner selection, new contracts, identity issuance service, market/pricing changes, new evaluators, validator scheduling, public cost database, production hosting and automatic public deployment. The existing Envio service stays optional. No new Python or JavaScript dependency is needed for this scope.

## 2. Inspected baseline and reuse

| Existing implementation | Reuse or narrow extension |
|---|---|
| [`MonadMarket`](../modules/adapters/chain/market.py), [`NativeSender`](../modules/adapters/chain/native.py) | Reuse qualified reads, durable signed transactions, gas caps and one nonce queue. Add root `createTask`, `cancelTask`, `withdrawCredit` and credit reads. Current preflight assumes an existing task and current completion logic assumes task events; extend those specific cases. |
| [`MarketWatcher`](../modules/agent_client/discovery.py), [`IndexedMarket`](../modules/adapters/chain/index.py) | Reuse finalized discovery, verified event storage, pagination and freshness. `BidAccepted` and `CreditWithdrawn` already persist as logs, but are deliberately absent from `task_versions`; derive inspection views from those logs rather than inventing a second ledger. |
| [`WireCodec`](../modules/adapters/chain/codec.py), frozen [write ABI](protocol.abi.json) and [read ABI](contracts.read.abi.json) | Already cover all required contract methods/events. No Solidity or canonical ABI/schema extension. |
| [`market.py`](../modules/market_core/market.py), [`reputation.py`](../modules/market_core/reputation.py) | Reuse exact score, ordering, price and probability calculations for explanations. Chain allocation and counters remain authoritative. |
| [`MonadRegistry`](../modules/adapters/registry/chain.py), [`resolveProfile`](../modules/adapters/registry/resolution.py), safe [`ContentStore`](../modules/adapters/storage/content.py) | Reuse identity provenance, Agent Card parsing, digest checks and network controls; keep current metadata separate from the profile digest frozen in a bid. |
| L7 [`OutcomeObserver`](../modules/validation/observer.py), [`FeedbackAdapter`](../modules/adapters/registry/feedback.py), validator and publisher | Reuse canonical receipts, submitted results, evidence references and verified publication reads. Public inspection must work without access to the validator's private job database. |
| L5 [`ExecutionHistory`](../modules/economics/history.py), L7 [`Reconciler`](../modules/validation/reconciliation.py) | Export an explicitly selected, allowlisted summary of existing records. Do not recalculate forecasts, ingest another report or create another reconciliation pipeline. |
| [`Journal`](../modules/adapters/storage/journal.py) v5 | Reuse sender binding, operation storage, finalized-log checkpoints and content retention. A new application journal isolates requester/read activity from agent and validator writers. |
| [L2 black-box conformance](layer-2.md#external-conformance-invocation), [`demo_layer7.py`](../scripts/demo_layer7.py), L6/L7 process fixtures | Reuse protocol assertions and local infrastructure. Existing worker processes share the reference implementation; add an independently implemented example to establish interoperability beyond those tests. Root creation/progression in existing demos uses test helpers, so it does not demonstrate the new requester path. |

The stack stays Python 3.12, argparse, SQLite, Web3.py/eth-account, existing HTTP/A2A adapters, Foundry, Docker and Kubo. The independent example can use the already pinned Node.js/ethers toolchain. No Jev/model call is needed to explain existing data or run the deterministic interoperability example.

## 3. Ownership and inherited assumptions

- **On-chain:** unchanged task admission, immutable bids, allocation, child escrows, verdict authorization, settlement, counters, withdrawal credits and L7 publication deduplication.
- **Off-chain L8:** authoring checks, invocation of existing commands, durable requester request IDs, read composition, provenance/availability labels, selected cost export and demonstration.
- **L4–L7 remain owners:** discovery verification, native-send recovery, private economics, awarded execution/delegation, validation/signing, IPFS evidence publication and cost-history selection. L8 calls them and cannot override their decisions.

Live operation requires the existing complete qualified `DeploymentManifest` and its verification documents. Local fixture mode remains explicitly separate, restricted to the isolated local chain and declared localhost transports. A fixture descriptor must never be accepted as a live manifest. Preserve L7's pinned registry/publisher/validator and unresolved live activation gates.

One process owns each writable journal and one queue owns each transaction sender. A requester using several wallets uses a separate journal per sender; it cannot share the agent's or validator's active queue. The MVP CLI signs with an operator-owned encrypted EOA keystore. Other wallets can use the unchanged public ABI; L8 does not require them to surrender custody or pretend an EOA can spend a contract wallet's credit.

## 4. Inputs, outputs and application interfaces

Inputs are canonical `TaskTerms`, `TaskRef`, `AgentRef`, explicit integer amounts and addresses, operator configuration, public finalized observations, and optionally one operator-selected private execution record. Task terms reference **already published** input/schema/policy bytes; no task-upload service or natural-language policy generator is required. Existing HTTPS/IPFS publication tools and local demo hosting supply those bytes.

Outputs embed canonical objects unchanged: `TaskSpec`, `Bid`, `Allocation`, `ResultCommitment`, `SettlementReceipt`, `ValidationRecord`, `AgentProfile` and content references. Application provenance/diagnostics live in a separate versioned envelope, never extra fields on those objects.

The CLI entry point is `python -m apps.cli.main`. Its command groups and reusable functions must provide:

| Command / function | Behavior |
|---|---|
| `task validate` / `validateTaskRequest(terms, requester)` | Check canonical terms and fetch/validate committed prerequisites; return a preview with deposit, deadlines, validator and delegation limits. No signing, upload or transaction. |
| `task create` / `createTask(command, requestId)` | Create one funded root through the durable market adapter; return its operation and, only after finalized confirmation, its emitted TaskRef. |
| `task allocate`, `task expire`, `task cancel` | Invoke the existing permissionless progress or requester-only cancellation rule for one explicit TaskRef. |
| `credit show`, `credit withdraw` | Read an address's credit at a finalized stamp; withdraw an explicit amount to an explicit receiver from the configured sender's credit. |
| `operation show`, `operation resume` | Inspect a saved request without broadcasting; explicitly resume its unchanged durable native operation. |
| `tasks list`, `task show`, `task bids`, `task tree` | Read open markets or one explicit task, auction explanation, bounded descendants and public lifecycle/evidence/feedback state. |
| `agent show` | Inspect one explicit AgentRef, current identity/profile availability and family counters; optionally include a task's frozen scoring snapshot. No global agent-ranking product. |
| `cost export` | Read one explicitly supplied operator journal and ExecutionRef through a read-only snapshot; write a selected summary to stdout or a new local file. Never publish automatically. |

All commands emit versioned JSON to stdout and diagnostics to stderr. Required flags include explicit configuration and full references, terms-file for creation, request ID for writes, and receiver/atomic amount for withdrawal. Mutating commands require explicit `--broadcast`; validate/read commands never broadcast, including when they find an unresolved transaction. Avoid calling `NativeSender.readOperation` from a read-only command: it can rebroadcast retained bytes. `operation resume` is the explicit recovery action.

Use nonzero exit status for invalid input, conflict, missing configuration or unavailable required data; a saved pending transaction returns its operation status successfully and does not masquerade as a confirmed action. Do not wait indefinitely for finality. No operation requires an interactive confirmation prompt after its complete inputs and broadcast intent are supplied.

### Configuration and bounds

Reuse current manifest, RPC, genesis, encrypted-keystore, gas cap, artifact gateway and qualification configuration conventions. Reader configuration needs no keystore. Add only application database and optional index configuration plus the explicit limits below; document any new environment variable in `.env.example` without secrets or raw-key examples.

Keep the existing 10-second RPC/fetch timeout, three-redirect content policy, at most three native broadcast attempts and polling no faster than two seconds. Use page sizes 1–100, default 20; no unbounded list response. Reuse watcher's bounded scan/storage settings. Limit task-tree output to the inherited depth/child limits (at most 21 nodes for the current depth-2/four-child policy). Limit a cost export to one execution and 1 MiB. Operator-requested catch-up can repeat bounded scans; a single CLI read must not loop until the entire chain has been indexed.

## 5. Requester and fund lifecycle — L8-01, L8-05

### Creation

1. Validate `TaskTerms`, native MON units, reduced alpha, immutable validator, refund address, current chain-time deadline ordering/acceptance slack, delegation limits and any `retryOf`. Reuse existing term validation helpers where suitable; contract simulation is the final admission check, not a new copy of the state machine.
2. Fetch exact input/schema/policy through `ContentStore`. Reuse L7's prerequisite parsing, shape semantics and policy-digest/pointer checks by extracting only the shared prerequisite validation if needed. Input is at most 1 MiB; schema and supported policy are each at most 64 KiB. No candidate result, execution, forecast or verdict is required. Missing or mismatching prerequisites block this client creation attempt.
3. Persist the canonical command, explicit deposit and request ID before native dispatch. Caller supplies no task ID, requester override, creation time, ancestry or reputation snapshot. `msg.value` equals the uint96 budget exactly; gas is separately funded. Never auto-change deadlines, round alpha or replace content under a retained request ID.
4. Extend `MonadMarket` root creation preflight without calling `readTaskAt` for a nonexistent ID. Read `retryOf` only when present. Use the existing native sender to simulate, sign, persist and broadcast.
5. Confirm the actual transaction and `TaskCreated`: emitter, namespace, sender/requester, root ancestry, terms and deposit must match. Obtain the assigned ID from that event, then read the canonical task at the receipt block. A similar task or guessed next ID cannot prove this request succeeded.

Scope request IDs to `(chainId, market, sender, requestId)` and retain exact command/value binding using existing journal/native-operation storage. Bound request IDs to 1–128 characters and derive the operation key from that scope, not from mutable command contents: changed contents under the same request ID must conflict. Look up a retained request before authoring checks; repeating it reconciles its unchanged native intent, without making confirmation depend on refetching artifacts or still-valid creation deadlines. New creation broadcasts stop at `biddingClose`. An intentional second task needs a new request ID. A protocol retry also needs a new funded task and legal `retryOf`; transport retry is never protocol retry.

### Progress, cancellation and withdrawal

- Allocation invokes `allocateTask` during `[biddingClose, allocationBy)`; the contract chooses the winner/price or UNALLOCATED. Expose explicit root progress commands so the demo need not use an internal fixture allocator. A long-running automatic keeper is unnecessary for this MVP.
- Cancellation invokes `cancelTask` only for the requester before biddingClose with no accepted bids. Concurrent bid/cancel races are decided by the chain. A preflight result is not a cancellation guarantee.
- Expiry uses the existing state-specific lower cutoff with no invented upper deadline. An elapsed deadline does not itself establish settlement. The CLI may expire an explicitly named root or child; it must not reschedule existing worker/validator jobs.
- Withdrawal reads `readCredit(sender)` and invokes `withdrawCredit(receiver, amountAtoms)`. Amounts are **uint256**, unlike uint96 task budgets. Require a positive explicit amount and nonzero receiver; no dynamic “withdraw all” intent whose amount changes on replay. Confirmation verifies `CreditWithdrawn` against sender, receiver and amount, not a task event. A rejected receiver leaves credit intact. Withdrawal has no task deadline or TaskRef.

Reuse allocation/expiry wrappers and extend only missing cases in native preflight/event verification. Simulations and status reads may race; the canonical transaction receipt decides. Root/cancel/withdraw recovery preserves the same signed bytes and nonce. A finalized failed transaction remains visible; a new economic attempt needs an explicit new request ID after canonical failure, never an automatic replacement for an unknown transaction.

## 6. Public inspection — L8-02, L8-03, L8-05

### Provenance and read models

Implement reusable `readTaskDetails`, `listTaskBids`, `readTaskTree`, `readAgentDetails` and `readCredit` application functions. Reuse `listOpenTasks`, L7 outcome assembly and publisher reads rather than adding parallel watchers. A public reader can maintain its own L7 receipt projection without running validation or export jobs.

Define closed application output schemas during implementation, in `specs/schemas/application.schema.json`, referencing canonical types. A common envelope contains `version=1`, `kind`, `networkScope` (`LOCAL_FIXTURE` or `QUALIFIED_TESTNET`), `chainId`, `market`, `observedAt`, `indexedThrough`, `finalizedHead`, `complete`, `missing`, `nextCursor` and `data`. Stamps use the existing finalized stamp shape; nullable fields mean unavailable/not applicable, never zero. Each missing entry names a component and stable diagnostic reason. Freshness stamps do not disappear merely because a response includes some valid data.

| View data | Required contents |
|---|---|
| Task | Full canonical task view, chronological verified event references, explicit money fields, auction explanation and separately labeled artifact/feedback availability |
| Bids | Canonical admitted `Bid` records with event provenance, task snapshot reference and page cursor; no pending or rejected offer inserted as an admitted bid |
| Tree | Root/parent references, direct children, depth, child funding counters, each node's canonical state/receipt and completeness; `retryOf` is a separate link, not another child edge |
| Agent | Current registry observation, metadata/card digests and claimed skills when retrievable, endpoint observation, synthetic prior, real family counters and optional frozen task snapshot |
| Credit | Address, current withdrawable uint256 credit, attributed settlement-credit entries and withdrawal events available at the observation stamp; never a per-task “withdrawn” amount inferred from an aggregate balance |
| Operation | Request ID, existing operation result, transaction hash, persisted native receipt/diagnostic and derived confirmation label; no raw keystore, signed transaction bytes or private agent records |

Choose one verified finalized `observedAt` per coherent view. Composite event-derived views use a fully scanned canonical prefix; never mix a current task read with older bid/tree completeness. Expose the newer finalized head separately. Extend stamp-aware read helpers narrowly where current `readBid`/identity methods independently choose a newer stamp. Canonically validate a reused stamp and cursor; reject forged/foreign cursors without poisoning the persisted halt flag.

Operation confirmation labels are derived diagnostics: UNSENT, PENDING, MINED_UNFINALIZED, FINALIZED_APPLIED, REJECTED, FINALIZED_REVERTED or UNKNOWN. Preserve the lower-layer result alongside them, especially a finalized status-0 transaction whose adapter result is UNKNOWN with a failure diagnostic. No new protocol task state or fabricated revert reason is introduced.

On index lag/outage, direct L4 discovery remains usable and bounded catch-up may proceed. Return retained data with its real stamp and incomplete/stale diagnostics when appropriate; never call it current. A direct canonical task lookup may succeed even if history is incomplete, but must mark bids/tree/history incomplete. Registry, metadata and IPFS outages do not hide an otherwise readable canonical settlement. Qualification/finality failures cannot be converted into authoritative current reads.

### Auctions and money

Enumerate bidders from verified `BidAccepted` logs; use existing contract reads for fixed-stamp binding checks. There is no on-chain “list all bidders” API. Reuse L1 functions for ranking/critical-price explanations over the **complete** verified bid set; do not compute a winner from one page. Show the frozen p, counter snapshot, score, exact AgentRef tie order, zero outside option, critical-price floor and budget cap. Before allocation label any calculation a preview; after allocation compare with the actual `Allocation`. Missing history yields an unavailable explanation; disagreement with canonical data is a visible conflict, never a substituted winner.

Keep `budgetAtoms`, bid amounts, allocation `reservedAtoms`, receipt `paidAtoms`/`refundAtoms`, current credit and withdrawals distinct. Use atomic integer strings with explicit units, exact rational alpha and signed integer scores; no floating-point monetary conversion. Actual resource costs appear only through §7. A paid receipt establishes a credit, not a withdrawal. Aggregate credit may combine multiple tasks and roles; do not invent which task financed a withdrawal. Parent failure leaves successful child receipts/credits intact, including children that settle later.

### Execution, identities and evidence

Public progress is the canonical OPEN/AWARDED/RUNNING/SUBMITTED/SETTLED state and its events, result and child tree. There is no public percentage-complete or inference about agent liveness. Optional A2A observations require the existing profile/correlation and their own timestamp; A2A COMPLETED never means paid. Inspection must not send award hints or start execution.

Current owner, verified wallet and metadata are distinct from `ownerAtBid`, frozen signer/payout and committed card digest. Failed current metadata resolution must preserve admitted bid facts. Expose the family prior as synthetic Beta(1,1), real success/failure counts separately, and p through the existing function; registration performs no warm-up work. Task scoring uses its frozen snapshot, not today's counters. Claimed skills and distinct addresses do not prove competence or independent ownership.

Endpoint status describes only observed transport facts. Without an explicit non-mutating, authenticated standard read, report UNPROBED; successful card retrieval proves only card availability. Do not invent a health endpoint, execute a task as a probe, scan arbitrary URLs or bypass advertised authentication. Preserve safe URL/DNS/redirect limits for every metadata/artifact fetch.

Read policy/result/evidence refs from canonical task/result/receipt. Optional artifact inspection hashes exact fetched bytes and verifies evidence/signature binding with existing helpers; unavailable or mismatching bytes get a separate diagnostic without rewriting settlement. Evidence URI changes remain legal where L7's signature binds only its digest. For eligible settled receipts report PUBLISHED only after the configured publisher mapping and registry feedback verify; verified absence means PENDING, failed qualification/read means UNAVAILABLE. NONE means NOT_APPLICABLE. No private validator journal is needed to establish those public states; internal retry stages are absent unless explicitly supplied as non-authoritative diagnostics.

## 7. Opt-in cost disclosure — L8-04

Provide one local export per explicitly selected ExecutionRef and operator journal. Open a bounded read-only SQLite/WAL snapshot, check journal/agent/operator binding, and read the active L5 report/estimate and selected L7 reconciliation coherently. Do not instantiate a second writer, tick reconciliation, change report selection, or query another process's database through a public command.

The closed `CostDisclosure` v1 contains execution/agent/operator references, export time, selected reconciliation key, report/estimate IDs and digests; retained forecast mean/quantiles/tail, source/fallback/sample count, units, scope and uncalibrated flag; selected report completeness and actual-cost provenance; scoped execution/child-payment totals; saved-rate comparison and explicit missing reasons. Export the numeric/enum projection, not the entire estimate or its free-text coverage. Derive a fixed coverage label from the existing scope, with no stronger completeness claim. The allowlist excludes raw provider requests/responses, prompts, keys, credentials, local paths, free-form runtime configuration, private bid policy and unrelated executions/expense records.

Do not blindly serialize the full reconciliation: it embeds private report/context/comparison details. Retain provenance enums and numeric summaries, not arbitrary diagnostic or provider text. If a required field is absent, preserve null plus a reason. Missing usage is not zero usage; COMPLETE own execution is not complete whole-agent cost. Whole-agent profit/system utility remain unavailable unless an existing complete-scope result actually supports them. Export does not call Jev, update prices or upgrade telemetry to provider-attested evidence.

Writing this local file is the entire sharing operation in the MVP. A later consumer may display an explicitly supplied disclosure, labeled **operator-reported and unauthenticated**; a digest binds bytes, not author identity or invoice truth. Never attach it automatically to a public task, IPFS evidence, feedback or competitors' profiles. Corrections create a new export for the new selected report; preserve older exports' IDs/digests as historical snapshots.

## 8. Independent participation and demo — L8-06, L8-07

Add `examples/external-agent/` as a small standalone Node.js/ethers implementation of one existing deterministic solo template. It uses public schema/ABI/signature/profile artifacts and its own process, journal, key and HTTPS endpoint. It must not import or wrap `Participant`, `DiscoveryRuntime`, L5/L6 coordinators or reference worker code. It may reuse the installed ethers dependency and standard Node APIs; no general alternative SDK or delegation engine is required.

After startup it discovers tasks by direct finalized RPC/events, checks its local template/deadline/price policy, submits its own bid, independently observes/accepts its award, runs only after finalized acceptance, retains/publishes the exact artifact and commits it. The harness supplies configuration, funding and isolated fixture identity registration, but does not tell it which task won or invoke its work. Persist signed intents and one execution claim; duplicate observations/hints and process restart cannot initiate a second run. Unknown sends retain their transaction identity. No inference keys or paid model calls are required.

Expose the pinned A2A HTTP+JSON profile, with persisted correlation, standard polling and bounded rejection behavior. Adapt the existing external conformance descriptor/controller to this target; fixture-only preparation may pause discovery and set up isolated chain states for wire tests. Keep that control outside the agent's public endpoint and out of the normal lifecycle. Run the unchanged applicable black-box HTTP assertions against the example, not against a reference process hidden behind it.

`make demo-l8` must run the new requester and inspection paths against real local Monad EVM contracts, real Docker validation/execution, Kubo and the pinned reputation/publisher. Reuse L7 setup, reference agent profiles and deterministic model transport where appropriate. Use actual local HTTPS for the independent agent's card, A2A and artifact boundary. Required scenarios are:

1. A root created through the CLI; the independent agent discovers, bids, accepts, completes, receives canonical SUCCESS and portable feedback. Inspect the full trace and withdraw actual available payout/refund credits through the new command path. Use separate credited-wallet configurations where roles differ.
2. A parent with two public child markets using the existing L6 workers; inspect the bounded tree, independent child deposits/credits and an explicitly exported L5/L7 cost comparison. Reuse L7 validation, without fixture-created verdicts.
3. Deterministic validation failure and validator-timeout/refund, with FAILURE versus NONE feedback semantics and successful refund withdrawal.
4. Restart requester after a creation broadcast and repeat its request ID; restart an external participant and replay events/hints; stop/restart export. Prove the same task, execution, money effects and feedback index survive.

Use existing lower-layer scenarios for additional adversarial parent/child and protocol boundaries rather than duplicating every scenario. L8 adds assertions through its public commands/read outputs. Keep clock advancement, fixture setup and deterministic model transport explicitly labeled in the report; those are not production controls. Run core discovery with Envio and award hints disabled at least once. The requester/inspection app is not required for an already configured external agent to keep observing/executing.

The report includes source revision, process/implementation identities, fixture/shared ownership disclosure, network scope, references, transaction hashes and finality stamps, artifact digests, canonical allocations/receipts, credit changes, feedback indices, privacy-safe cost summaries and unresolved live gates. Credit the paper as in README and explain success-only payment, independently funded children, validator trust, Sybil/common-control limits and infrastructure assumptions. An independent implementation/process in the local demo is not proof of an independently operated public deployment.

## 9. Durable state, failures and invariants

Reuse existing operation records for writes and verified L4/L7 projections for reads. Avoid a new task database or generic job framework. If a new derived read cache is necessary, it is disposable, versioned, namespace/stamp-bound and updated atomically with its own checkpoint; it cannot acknowledge worker or validator deliveries. Preserve all existing v5 journal records if a migration becomes necessary.

| Condition | Required behavior |
|---|---|
| Lost create/withdraw response or restart | Recover exact saved native intent; do not create another funded root, alter withdrawal amount or allocate a fresh nonce while unknown |
| Competing allocation, expiry, cancellation or withdrawal | Re-read canonical state, retain actual local operation status and explain the race; do not claim another caller's action was this transaction |
| Deadline reached while waiting | Stop expired-action broadcasts; retain pending evidence and expose the existing eligible expiry command |
| RPC unavailable/stale or index behind | Return bounded failure or explicitly stamped partial data; no latest-block fallback or fabricated empty market |
| Finalized contradiction | Persist the existing halt and block new transactions; identify conflicting provenance and preserve records |
| Publisher/metadata/evidence unavailable | Keep canonical task/receipt/money visible with separate component status |
| Private report missing/corrected or export too large | Preserve missing/censored semantics, export only a coherent selection, reject over-limit export; never substitute zero or dump the database |
| Storage full, insufficient balance, unsafe content or bad keystore | Fail before the corresponding new external effect; retain recoverable prior operations |

The invariants are: no new authority over outcomes; exact money and frozen terms; no fresh economic intent on transport retry; no paid execution before accepted finality; no partial auction presented as canonical; no mixed-height completeness claim; no automatic private disclosure; no synthetic prior counted as completed work; no per-task withdrawal attribution invented from aggregate credit; no parent failure erasing child payment; and no fixture presented as live qualification. Read and export commands never sign, broadcast or trigger work.

## 10. Implementation plan

**Goal:** satisfy the non-frontend interpretation of L8-01–L8-07 above. Preserve market economics, canonical contracts, external-agent compatibility and L7 ownership. No frontend package or hosted service is introduced.

1. Freeze application output/configuration contracts in this spec and `specs/schemas/application.schema.json`. Reuse canonical schemas by reference. Define stable diagnostics and CLI argument/exit behavior. Reuse canonical golden auction/command fixtures; new integration fixtures exercise application composition without duplicating protocol vectors.
2. Extend `modules/adapters/chain/market.py` for the three missing commands, fixed-stamp reads and command-specific receipt verification. Reuse `native.py`; change it only if a real boundary requires it. Add root/cancel/withdraw recovery regressions alongside existing transaction integration tests.
3. Add cohesive requester/inspection composition under `modules/agent_client/` and `apps/cli/main.py`. Keep chain access in adapters and pure calculation in existing core modules. Reuse existing watchers/receipt projections. Add bounded private export in the owning economics/reconciliation boundary, with a read-only entry point.
4. Add the standalone external example and its black-box conformance controller/tests. Share canonical artifacts, not the reference worker implementation. Keep temporary keys, TLS certificates, databases and logs ignored.
5. Add `scripts/check_layer8.py`, `scripts/demo_layer8.py`, `check-l8` / `demo-l8` Make targets and `docs/layer-8.md`; update README and only relevant environment examples. Extend the existing qualification/run instructions by composition, not another qualification authority.

Reuse assessment and open decisions must be revisited if implementation finds a lower-layer defect. Correct it narrowly with regression evidence; do not copy the defective behavior into an application workaround. New module names are responsibilities, not a requirement to create empty files for every function.

Implementation sequence confirmed on 2026-10-08: extend the existing market adapter and share prerequisite validation; freeze the closed application schema; implement requester/inspection and read-only disclosure; compose the CLI; add the independent Node agent and real-boundary tests/demo; run the full inherited gate. Keep all new state in existing bounded journals or the external example’s own durable file. No new dependency, protocol version, contract, hosted API or frontend is planned. Live facts in §12 remain unresolved and do not authorize public transactions.

Implementation findings: application inspection shares read-only receipt/effect verification with native recovery; fixed-stamp bid/identity reads preserve coherent views. Live configuration checks registry identity/version against the manifest before opening a journal or loading a key. The independent example explicitly supports the isolated local fixture only. The existing external-target harness needed connection-close semantics because its tests and finalizer use different event loops. A no-input Docker subprocess could close stdin before an unnecessary empty drain; the adapter now avoids that drain, with a real-process regression. Validator fixture container names include their durable directory so separate local chains cannot collide through repeated addresses. These narrow fixes preserve protocol behavior and are included in the inherited regression run.

## 11. Acceptance criteria

### Follow-up plan: live testing guide and operational logs

Requested after the merged Layer 8 commit `36b713f`: document the complete developer path from local acceptance to qualified Monad testnet and separately bounded live inference. Reuse the layer operation guides, deployment/qualification scripts, requester CLI and existing standard-library logging. Add `docs/testing-agentlance.md`, a README entry and a small shared application logging setup; extend native sender and validator lifecycle logs with timestamps, stable event names and public correlation IDs. Preserve transaction/state/retry behavior, JSON stdout, canonical interfaces and private payloads. No new dependency, deployment, funded run, paid request, logging service or protocol change is in scope.

Sequence: inspect implemented commands and external documentation; write the runbook with explicit operator-supplied prerequisites and known tooling gaps; add bounded lifecycle logs; test successful/rejected/unknown native sends, validator retries/publication, log redaction and application stream separation; include the logging regression in the L8 gate and run relevant existing recovery/validator/CLI tests plus Ruff and whitespace/link checks. Completion requires executable documented command syntax, accurate live gates and passing affected tests. Registry addresses, hosting, balances, pricing/model availability and live qualification remain operator decisions; placeholders never establish those facts.

Follow-up verification on 2026-10-09 completed the inherited L0-L7 gate, the expanded
**14-test** L8 Python suite, all 24 external HTTP assertions, and both Node parser/ABI
tests. The first full-gate demo invocation exposed a 40-second fixture subprocess timeout
during a finalized credit read; no protocol assertion failed. The fixture now uses a
bounded 90-second subprocess wait and kills/reaps a timed-out child. The exact affected
external-agent tests passed (**3 tests**, including logging setup), and the standalone
four-scenario L8 demo then passed. Ruff lint/format and whitespace/link checks passed.
Public deployment, paid inference and the documented live revision/registry compatibility
checks remain unexecuted.

| Requirement | Executed evidence needed for non-frontend completion |
|---|---|
| L8-01 | CLI-created canonical root, exact deposit/terms, invalid prerequisites/roles/deadlines rejected; same-ID replay and changed-body conflict; cancellation before bids and rejection after a concurrent bid; authorized uint256 withdrawal, receiver rejection and lost-response recovery; budget/reserve/credit/resource-cost distinctions |
| L8-02 | Canonical bidders and complete auction explanation match L1 fixtures and L3 allocation, including ties, single/no bidder and nonpositive score; pagination cannot manufacture a winner; bounded tree and late child settlement; actual evidence/publication reads with unavailable artifacts still showing settlement |
| L8-03 | New identity has zero real outcomes plus labeled prior; task snapshot differs correctly from current counters; identity transfer/card mutation cannot rewrite frozen bid facts; metadata/endpoint availability never implies competence or work performed |
| L8-04 | Default public outputs contain no private data; explicit one-execution export preserves selected correction, sample/fallback/units/provenance and missing/zero distinctions; poisoned free-text/provider fields, unrelated records and credentials do not leak; unsigned claims stay unauthenticated |
| L8-05 | Pending/mined/finalized/rejected/unknown cases, forged cursor, stale/absent index, same-block events, receipt mismatch/finality halt and interrupted create/withdraw are visible and recoverable; reads never rebroadcast; direct discovery works without index/hints |
| L8-06 | Standalone example passes existing applicable external-profile assertions and completes real funded local work without reference-runtime imports or winner instructions; duplicate hints, restart and lost sends do not repeat invocation or transactions |
| L8-07 | Reproducible CLI-driven demo with solo, children, failure/refund, actual withdrawal, restart and privacy-safe cost evidence; independent process/implementation disclosure, research attribution and explicit live/frontend limitations |

`make check-l8` must run the full inherited `check-l7` gate, new requester/read/export tests, external-target conformance, real local-chain/TLS/Docker/Kubo integration and `demo-l8`. Include requirement-to-executed-test mappings, reject missing/skipped required cases, run relevant Python/Node checks, preserve frozen ABI/schema checks, and run formatting/whitespace checks. Store reports under ignored `.scratch/layer8/`. No paid model, public funds or public RPC is required for routine acceptance.

Implementation completion is based on the executed gate below and must be reported as **Layer 8 non-frontend offline scope complete**. Frontend delivery and live activation have separate status and cannot be inferred from a passing local demo.

### Executed acceptance — 2026-10-08

- `make check-l8` passed, including the full inherited `check-l7` gate, its lower-layer regressions/demos, frozen protocol checks and Solidity checks. The final L8 suite executed **13 Python tests without skips**, including requester recovery, finalized revert races, bounded-history/cursor handling, live-configuration rejection, public metadata failure, the Layer 7 result transport ceiling, disclosure privacy/corrections and real independent-agent settlement/withdrawal.
- The standalone Node agent passed **24 unchanged external HTTP profile assertions**. Node syntax checks and **2 parser/ABI tests** passed. Tests exercised actual TLS, direct finalized discovery, a lost broadcast response, process restart and duplicate hints with one execution invocation.
- The final CLI-driven demo passed independent-agent SUCCESS/feedback/payout withdrawal, a successful parent with two independently funded children and an explicit cost export, validation FAILURE/refund withdrawal, and validator-timeout NONE/refund withdrawal. Docker, Kubo, the local Monad EVM and pinned publisher/reputation were exercised. Requester replay, participant restart and validator publication restart retained their canonical identities and effects.
- Ruff lint/format and `git diff --check` passed. Canonical protocol schemas, ABIs, contracts and dependency locks remain unchanged. No paid model call, public transaction or frontend was produced.

The gate, requirement-to-test mapping and test logs are generated under ignored `.scratch/layer8/`; `gate.json` records the result and `demo.log` identifies the full scenario report. The recorded source revision is the committed Layer 7 base plus this reviewed working-tree implementation. Live activation and independently operated third-party participation remain unverified as described below.

## 12. Open Questions and live activation gates

1. **Live deployment and ownership:** which existing Monad testnet market, registries, canonical publisher and validator evidence are approved, and who funds/operates root progress, validators and export? L7's qualification tooling exists; the committed baseline does not establish these live facts. Do not choose addresses, mint live identities, deploy or fund roles as an implicit consequence of implementing this spec.
2. **Live artifacts and uptime:** which existing HTTPS/IPFS hosting, retention, backup and gateway arrangement will serve requester content and external agent artifacts? Local demo hosting is required; production availability promises remain an operator decision.
3. **External operator proof:** is a separately operated third-party agent available for a live interoperability exercise? Deliver the independent example and tests regardless, but do not claim another operator or vendor participated without evidence.
4. **Future frontend/API:** browser wallet integration, public API hosting, access control and UI requirements are explicitly deferred by this request. The CLI/read schema is the minimum boundary; frontend choices must not force changes to protocol authority.
5. **Authenticated private disclosures:** signatures, publication permissions, redaction beyond the explicit allowlist and verified provider invoices are not established. The MVP exports a local operator-reported summary only; stronger trust or automatic public sharing needs a separate specification.

These questions do not block the defined local implementation. They block live activation, frontend completion or stronger operational/privacy claims where applicable.
