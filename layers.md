## 1. Goal and governing decisions

AgentLance is a decentralized task market on Monad. Independently operated agents discover public tasks, decide locally whether to participate, submit bids, execute awarded work, and optionally buy work from other agents through the same market. The protocol owns task and economic state; it does not own a fleet of agents.

The supplied research paper, *Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private Information*, already calls its framework AgentLance. This project implements and adapts that research for on-chain coordination. Credit the paper; do not present its allocation mechanism as an original invention of this implementation. See the paper's Sections 3–5 and Appendices A and E.

### Locked architectural decisions

1. **External agents, permissionless participation.** Any agent meeting the published identity and transaction rules can bid. No operator-maintained list of approved workers.
2. **Pull-based discovery.** Agents watch task events directly or query a replaceable indexer. Capability, availability, and affordability filtering happen locally.
3. **Separate standards and responsibilities.** ERC-8004 supplies identity and portable feedback infrastructure; A2A carries execution messages; AgentLance defines market and settlement rules.
4. **No warm-up workload per agent.** Every new identity receives a weak, explicitly synthetic prior. Registration and discovery trigger no benchmark execution or LLM call.
5. **Deterministic allocation.** Contracts determine the result from recorded bids and reproducible reputation snapshots. Neither a backend nor an LLM chooses the winner.
6. **Advisory cost estimation.** Known prices, execution history, and Jev inform an agent's private bid policy. They cannot authorize a payment or alter a recorded bid.
7. **Budget-aware critical-price procurement.** Preserve the paper's allocation score, add an explicit no-award option, and cap the critical payment at the advertised task budget.
8. **Payment on validated success for v1.** A failed or expired task pays zero. This is a deliberate change from the paper, with different incentives; do not claim the paper's truthfulness argument applies unchanged.
9. **Independent child escrows.** A manager funds children from its own available balance. Expected parent revenue is not spendable escrow. Successful children are paid even if the parent later fails.
10. **Bounded hierarchical delegation.** Reuse the task market recursively, with one parent per task and enforced depth, child-count, funding, and deadline limits. General dependency graphs are deferred.
11. **Explicit validation trust.** Start with objective validation templates and a disclosed, task-pinned validator. On-chain recording does not make an off-chain validator trustless.
12. **Small implementation surface.** Build a modular codebase with a few runnable processes. A layer is a responsibility boundary, not a microservice or an instruction to create many files.

Items 7–11 are protocol design choices introduced by this audit. Changes require a versioned specification change before implementation, because they affect money, incentives, or task outcomes.

## 2. Final architecture

```text
REQUESTER / CLIENT                         EXTERNAL AGENT OPERATORS
  creates task + funds escrow               ERC-8004 identity + Agent Card
              |                            local filter + estimator + bid policy
              |                                          |
              v                                          | signed bids / acceptance
  +-----------------------------------------------------------------------+
  | MONAD: authoritative AgentLance protocol                               |
  | tasks | immutable bids | reputation snapshots | allocation | escrow     |
  | result commitments | validator outcomes | settlement | evidence events  |
  +-----------------------------------------------------------------------+
       | public events                    ^                  |
       v                                  |                  | winner observes award
  replaceable indexer/read API             |                  v
       |                                  |        winner's A2A-compatible runtime
       +--> UI / explorer                 |           solo execution OR
       +--> optional agent polling        |           funded child-task creation
                                          |                  |
                              validator attestation <--- result artifact
                                          |
                                 settlement + real evidence
                                          |
                       +------------------+--------------------+
                       v                                       v
               ERC-8004 feedback                     cost history / private notes
               publication adapter                   for future local decisions
```

Agents may read Monad directly; the indexer is not an admission gate. Anyone can call time-dependent allocation and settlement functions when their predicates hold. A convenience worker can relay award messages to the single winner, but cannot select it or make receipt of that message a prerequisite for noticing an award.

### State ownership and trust

| Component | Owns | Must never own |
|---|---|---|
| Monad protocol | Economic task state, accepted bids, winning identity, price, escrow balances, outcome commitments, scoring counters | LLM prompts, model selection, secret agent history |
| ERC-8004 integration | Registry identity resolution and interoperable feedback publication | AgentLance's allocation or payment logic |
| External agent | Whether to bid, private forecast, tools, execution, delegation proposal, private notes | Its authoritative success probability or validation outcome |
| A2A adapter | Message transport and mapping between execution and market references | Award authority or payment finality |
| Indexer / read API | Rebuildable query projections and artifact references | A second authoritative task state machine |
| Cost estimator | Provenanced cost forecast | Bid submission without agent policy, payout authority, verified correctness |
| Validator | Evidence-backed result judgment under a pinned task policy | Winner selection, task-budget changes, worker self-approval |
| Settlement publisher | Retryable external feedback from immutable protocol receipts | New outcomes, discretionary score changes, duplicate evidence |
| UI | Forms, status explanations, comparisons, receipts | Independent formulas or hidden decisions |

Decentralization is deliberately scoped: participation, allocation, and escrow are protocol-governed; execution is external; v1 validation uses disclosed trusted infrastructure. Jev, artifact storage, RPC providers, and convenience services retain their own availability risks.


## 3. Protocol semantics that every layer must preserve

### 3.1 Canonical objects and identifiers

| Object | Required meaning |
|---|---|
| `AgentRef` | Chain namespace, identity-registry address, and agent ID; a bare numeric ID is insufficient |
| `AgentProfile` | Registry metadata reference, Agent Card reference/digest, claimed capabilities, protocol compatibility version; advertised skill is not proven skill |
| `TaskSpec` | Requester/refund address, input URI + digest, output schema, task family, budget, asset, alpha, bidding/allocation/acceptance/result/validation deadlines, validator policy, delegation permissions, parent/root/retry references, policy versions |
| `Bid` | Task reference, agent identity, non-negative amount, authorized execution signer, payout address, frozen reputation value/evidence reference, profile digest, chain ordering reference |
| `Allocation` | Winning bid/identity or no-award reason, score, runner-up score, uncapped critical price, capped payable price, mechanism version |
| `ExecutionRef` | Task reference + award reference + run ID, with a separately mapped A2A task/context ID |
| `ResultCommitment` | First final artifact URI/digest, producing execution reference, submission time; no mutable “latest result” after submission |
| `ValidationRecord` | Task/result/policy digests, verdict, evidence digest, validator identity/signature, replay-protected reference |
| `SettlementReceipt` | Outcome/reason, reserved price, paid amount, refund, winner, evidence-update effect, immutable receipt ID |
| `CostEstimate` | Private estimate ID, estimator/runtime/pricing versions, distribution, quantiles, coverage assumptions, timestamp and fallback status |
| `CostReport` | Execution and estimate references, actual usage and expenses, provenance, missing/censored fields; optional for payment |
| `ReputationEvidence` | Unique task receipt, identity, family, accepted binary outcome, validator-policy provenance; never a synthetic completed job |

All signed messages bind chain ID, market address, operation, task/award references, payload digest, nonce, and expiry. Specify canonical serialization and hashing in L0. Cross-market replay, cross-agent replay, and ambiguous encodings must fail.

### 3.2 Task lifecycle and money outcomes

`DRAFT` is local only. Funding creates `OPEN` atomically; no unfunded public market exists.

```text
OPEN --> AWARDED --> RUNNING --> SUBMITTED --> SETTLED
  |          |           |           |
  +----------+-----------+-----------+--> SETTLED with explicit terminal reason
```

| Transition | Authority / condition | Payment and evidence |
|---|---|---|
| Create → `OPEN` | Requester deposits full budget; complete immutable terms | Funds reserved; no evidence |
| `OPEN` → `AWARDED` | Anyone at/after bidding close and before `allocationBy`; eligible positive winner exists | Record immutable price ≤ budget; no transfer to winner yet |
| `AWARDED` → `RUNNING` | Awarded execution signer accepts before `acceptBy` | Runtime may start; no payout |
| `RUNNING` → `SUBMITTED` | Same signer submits final result before `resultBy` | Result fixed; no payout |
| `SUBMITTED` → `SETTLED/SUCCESS` | Anyone supplies valid pinned-validator PASS before `validationBy` | Credit fixed price to winner; refund remainder; one success observation |
| `SUBMITTED` → `SETTLED/VALIDATION_FAILED` | Valid FAIL under same policy before `validationBy` | Zero payout, full refund; one failure observation |
| `OPEN` → `SETTLED/UNALLOCATED` | Bidding closed, before `allocationBy`; no eligible positive bid | Full refund; no reputation update |
| `OPEN` → `SETTLED/ALLOCATION_EXPIRED` | No award before `allocationBy` | Full refund; no bidder penalty |
| `AWARDED` → `SETTLED/NO_SHOW` | Acceptance deadline passed without acceptance | Full refund; one failure observation for the winner |
| `RUNNING` → `SETTLED/EXECUTION_TIMEOUT` | Result deadline passed without a final submission | Full refund; one failure observation |
| `SUBMITTED` → `SETTLED/VALIDATOR_TIMEOUT` | Validation deadline passed without an accepted verdict | Full refund; reputation unchanged because correctness is unknown |
| `OPEN` → `SETTLED/CANCELLED` | Requester, only while bid count is zero and bidding is open | Full refund; no evidence |

The invariant is `createdAt < biddingClose < allocationBy < acceptBy < resultBy < validationBy`, with `acceptBy − allocationBy ≥ minimumAcceptanceWindow` from the deployment policy. Actions requiring a deadline are valid strictly before it; corresponding timeout actions become valid at or after it. Chain time determines boundaries. The allocation cutoff guarantees the winner a minimum acceptance window even if auction closure is late. At/after that cutoff, an unawarded task closes as `ALLOCATION_EXPIRED`. Once any bid exists, the requester cannot cancel, edit inputs, change validators, or reduce the budget.

Terminal outcomes cannot be reopened. A retry is a new funded task with `retryOf`, new deadlines, and a new auction. Concurrent calls must be safe: the first valid transition wins, and subsequent calls cannot pay or count evidence again.

Settlement records credits atomically; recipients withdraw separately. A failing receiver must not block market finality. A2A `completed`, an HTTP success, or a worker's “done” message does not constitute protocol success.

### 3.3 Allocation, critical payment, and units

For each eligible bid, use the paper's score:

\[
S_i=\hat p_i-\alpha b_i,\qquad
S_{-w}=\max(0,\max_{j\ne w} S_j),\qquad
P_{\mathrm{critical}}=\frac{\hat p_w-S_{-w}}{\alpha}.
\]

V1 modifications:

- `alpha > 0`; no special alpha-zero auction.
- The task budget `B` is a public maximum payment, not the predicted execution cost.
- Only bids `0 ≤ b_i ≤ B` are admitted. Identity/signature and policy checks must also pass.
- Select the highest score only if it is strictly positive; otherwise return `UNALLOCATED`.
- Equal positive scores use ascending canonical `AgentRef` byte order, fixed in L0. Do not use arrival time or a random LLM decision as the tie-break.
- Reserve `P = min(B, floorToAssetUnit(Pcritical))` at award. Pay exactly `P` on validated success; zero otherwise. Actual cost never changes `P`.

Budget capping is part of the published mechanism, not an emergency haircut after execution. The one-bid case has `S−w = 0`; its uncapped price can be much greater than the bid. A zero bid is allowed but still needs a strictly positive score.

**Integer representation.** Let `Q = 1,000,000`, `p_i` be an integer in `[0,Q]`, and alpha be the positive rational `a_num/a_den` per atomic unit of the task's asset. Compare signed integers:

```text
score_i = p_i * a_den - Q * a_num * bid_i
second  = max(0, highest score excluding the winner)
criticalAtoms = floor((p_w * a_den - second) / (Q * a_num))
reservedAtoms = min(budgetAtoms, criticalAtoms)
```

All implementations must use the same precision, tie-break, overflow bounds, and rounding. L0 selects admissible integer ranges and L1 tests boundary values. Never use floating point for economic decisions. Every awarded auction must satisfy `0 ≤ winningBid ≤ reservedPrice ≤ budget`.

All amounts within one market use one settlement asset and its atomic units. Alpha's units must match that asset; a USD-denominated alpha cannot silently multiply MON bids. V1 uses one configured settlement asset per deployment. The asset, decimals, network, and any USD conversion source are deployment decisions to resolve before L3 integration. Cost telemetry may retain provider currency; conversion used by a bid policy must record its source, time, and rounding. No live exchange-rate oracle is required inside allocation.

**Worked examples in whole currency units** (convert to atomic units in fixtures):

| Case | Inputs | Expected result |
|---|---|---|
| Normal auction | A: p=.90, b=.20; B: p=.70, b=.15; alpha=2; budget=.30 | Scores .50 and .40; A wins; critical price .25; success pays .25 |
| Budget binds | Only A: p=.80, b=.05; alpha=2; budget=.10 | Score .70; uncapped price .40; reserve/pay .10 on success |
| Outside option | Only A: p=.20, b=.20; alpha=2; budget=.30 | Score −.20; no award and full refund |
| Exact tie | A: p=.90, b=.20; B: p=.80, b=.15; alpha=2; budget=.30; A sorts first | Both score .50; A wins; critical price .20 |

**Incentive limitation.** The paper's immediate profit is fixed payment minus execution cost. Here, with actual success chance `q`, expected solo profit is approximately `q × P − E[cost] − participation overhead`. Thus `P80(cost) × margin` or even `E[cost]` is not automatically the economically appropriate bid. A success-adjusted reservation bid can be an agent's heuristic, but the implementation makes no dominant-strategy, Sybil-resistance, or collusion-resistance claim. Public bids also expose strategic information.

### 3.4 Synthetic priors and authoritative reputation

V1 defines one immutable `taskFamily` per task, from a small versioned taxonomy. A task family means a published category/validation template, not a keyword inferred differently by each service. Task-specific reputation initially means **conditioned on this family**, not an exact calibrated probability for every natural-language task.

For every `(AgentRef, taskFamily)`, start lazily with the synthetic prior `Beta(1,1)`. No history rows, benchmark calls, external scoring job, or per-discovered-agent transaction are needed to create it. An explorer may display it immediately for a discovered agent.

\[
\hat p_{i,k}=\frac{1+s_{i,k}}{2+s_{i,k}+f_{i,k}}.
\]

`s` and `f` count accepted, unique AgentLance outcomes in family `k`. Convert to the fixed-point integer by flooring `Q × (1+s)/(2+s+f)`. With no outcomes the estimate is .5, not an observed 50% success rate. For example, three successes and one failure give 4/6 before fixed-point rounding. The prior's effective weight is two observations and its relative influence falls as real evidence accumulates.

All new identities get the same prior in a family. Declaring “expert” in metadata, choosing a more expensive model, or changing a prompt earns no numeric boost. This intentionally sacrifices differentiated cold-start ranking in exchange for a reproducible policy without fabricated competence. Declared capabilities still support local filtering and explorer search.

The protocol stores family outcome counters with historical checkpoints. Each task pins `reputationSnapshotBlock = creationBlock − 1` and a scoring-policy version. Every bid reads the same historical boundary, records its reproducible score, and cannot change it after submission. Evidence arriving during that auction affects future tasks only. This avoids a privileged score-signing server or a periodically published Merkle root that can gate participation.

Only results settled under the deployment's accepted validator/template policy affect these counters. Third-party ERC-8004 feedback can be displayed with provenance, but is not automatically imported into allocation. Synthetic priors are never exported as completed-work feedback. The recorded task receipt is the deduplication key.

No-show and execution-timeout observations are failures; validator outages are unknown. Workers receive evidence for their own child task, and managers for the parent result. There is no second multiplication by an undefined reliability score: the selected success metric already includes accepted failure modes.

Identity minting and transfers do not establish one-agent-one-person or prove unchanged capabilities. New identities can escape a poor history, and colluding requesters can manufacture easy work. Uniform weak priors, task-family separation, validator policies, and transparent evidence reduce obvious score fabrication but do not solve these attacks. Use modest-value demo tasks; stronger economic identity and evidence-weighting policies need a later mechanism version.

### 3.5 Escrow and subcontracting

For every task, `deposit = outstanding escrow + credited payouts + credited refunds`. Amounts remain non-negative, and each atomic unit is credited once. Contract balance must cover all outstanding escrows and withdrawal credits; direct donations are not assigned to tasks.

The configured asset must support exact accounted transfers. Reject an inexact deposit; fee-on-transfer or rebasing assets are outside v1. No payment can depend on a token behaving differently from the deployment's tested asset contract.

A manager may publish a child only while the accepted parent is `RUNNING`. It is the child requester and deposits the full child budget from its own spendable funds. Parent escrow cannot be advanced, borrowed, or counted again as child collateral.

Additionally, the parent award creates a subcontracting envelope:

```text
child budgets still reserved
  + child payouts already committed
  + manager's declared own-work reserve
  <= parent reserved price
```

An unused portion of a terminal child's budget releases envelope capacity; a paid portion remains consumed. Enforce this across all linked children, including retries, not separately for each creation call. Set the non-negative own-work reserve once at parent acceptance, no greater than the parent price. It is a planning limit, not proof of actual provider cost or a guarantee of profit. Creation/delegation functions must check both actual funds and the envelope. The protocol constrains the declared hierarchy; it cannot police an operator's unrelated external spending.

Example: parent price is 100 units and own-work reserve is 20. Child budgets of 30 and 40 are permitted only if the manager separately deposits 70 units. If children succeed for 25 and 35, they receive 60 even if the parent fails. Parent failure refunds the root requester and leaves the manager bearing that loss. If the parent succeeds, the manager receives 100 and its profit subtracts child payments, its own costs, and overhead.

This needs manager working capital but avoids credit machinery, shared-escrow rollback, or clawing back a successful child's payment. A future parent-funded design would require an explicit requester-authorized loss policy and new ledger semantics; it is not an implementation shortcut.

V1 task relationships form a bounded tree. A root opts into maximum depth and maximum direct children, within deployment caps. Each child receives one parent and the same root reference; its depth is parent depth + 1. Only the parent's awarded signer may create it. A child cannot extend the root's permissions.

Require `child.validationBy + synthesisSlack ≤ parent.resultBy`, positive slack, and the child's complete deadline ordering. Every level must leave its manager time to consume results. Reject late or excessive child creation before taking funds. No circular references or cross-parent shared tasks.

Managers wait for their funded children to become terminal before submitting the parent result, using incremental terminal-child counts rather than an unbounded traversal. If none are awarded, execute locally if time and budget allow. If some fail, use valid outputs and finish missing work locally, or let the parent fail. Parent timeout does not cancel an already funded child's rights. Any caller can still finalize that child and release its money.

### 3.6 Validation and evidence policy

Every task must commit its output schema, executable evaluation/template version, validator identity, evidence requirements, and deadlines before bidding. “Validation criteria eventually” is removed from `appflow.md`'s task-creation semantics.

V1 supports objective, reproducible templates with binary PASS/FAIL. A scalar diagnostic score can be displayed but does not prorate payment or update fractional reputation. Subjective work, open-ended validator-agent markets, appeals, and multi-validator consensus are deferred.

The chosen validator belongs to a published deployment policy and is fixed for the task. It must not be the winning agent or a manager judging its own child's work. Check obvious address-role conflicts at task creation and bid admission, before maintaining the top-two bid records; do not invalidate a leading bid later through a new eligibility rule. Independent addresses do not prove independent operators. Validator allowlisting is a trust boundary for judgments, not a worker allowlist.

An off-chain validator signs the task reference, result digest, policy version, verdict, evidence digest, and expiry. The contract checks authorization, binding, state, and time; it does not pretend to run arbitrary tests itself. The first accepted final verdict settles the task. L0 defines this adapter contract; L3 exercises it with deterministic fixtures; L7 connects the real runner.

If the validator is unavailable, the specified timeout returns funds and records unknown correctness. The worker bears the risk of unpaid work under this v1 policy; show it before bidding. Validators are operator-funded for the MVP, with cost recorded separately. Do not silently subtract validator fees from the promised worker price.

Settlement atomically writes the receipt and updates eligible reputation counters. Publication to ERC-8004 and cost-history ingestion happen afterward with idempotent retries. Their outages cannot block refunds or create a second settlement.

## 4. Sequential layers and exit gates

### L0 — Core specification and executable examples

**Decision and rationale.** Freeze shared meanings before implementation. The original plan correctly identified this need but left failure and validation semantics for later. An incomplete happy-path specification is insufficient for escrow.

**Requirements**

- **L0-01:** Specify all objects in §5.1, state transitions, command/event payloads, failure codes, authority rules, and immutable task fields.
- **L0-02:** Specify the exact math, precision, sorting, caps, outcomes, reputation snapshot rules, evidence deduplication, and ledger invariants in §5.
- **L0-03:** Define the AgentLance A2A compatibility profile, signing domains, authorization rules, artifact hashing, deadline boundaries, event replay rules, and supported protocol versions.
- **L0-04:** Produce machine-readable schemas and golden input/output examples. Keep illustrative sample data unmistakably separate from real evidence.
- **L0-05:** Freeze v1 deployment limits before contract implementation: numeric ranges, maximum payload/URI sizes, supported task families/templates, maximum depth and children, minimum timing slack, and confirmation policy. Suggested demo bounds are depth 2 below the root and 4 direct children, giving a bounded hierarchy without pretending to support arbitrary recursion. Final constants belong to the versioned deployment manifest.
- **L0-06:** Define asset/amount semantics independently of a particular currency, and identify the deployment facts to resolve in §10. Do not introduce database tables or framework models as the domain definition.

**Outputs:** State-transition table; schemas; command/event catalog; protocol-policy record; numeric, lifecycle, and ledger fixtures; requirement-to-test map. These are real artifacts to create during L0 implementation, not files already supplied by this architecture document.

**Does not own:** Runtime code, deployment tooling, UI, model prompts, or vendor SDK selection.

**Exit gate:** Walk through normal success, one bidder, no bids, score ≤ 0, tied bids, failed validation, all timeout classes, transfer of an identity, failed parent with successful child, and duplicate events without inventing a new field or rule. Every terminal path has a payer/refund/evidence answer. All consensus constants are resolved before L3.

### L1 — Deterministic market, reputation, and accounting core

**Depends on:** L0 only.

**Decision and rationale.** Move pure market arithmetic ahead of Web3. Include the simple reputation reducer and ledger rules here because they affect allocation and funds. L5 should not later supply an opaque authoritative score.

**Requirements**

- **L1-01:** Implement allocation and price calculation as pure functions, including no-award, ties, one bidder, budget capping, and integer arithmetic.
- **L1-02:** Implement family-prior evaluation and immutable evidence reduction. The core consumes a historical evidence snapshot; it does not fetch registries or call Jev.
- **L1-03:** Define transition predicates and escrow/subcontract-envelope calculations from the same specification.
- **L1-04:** Calculate analytics without confusing transfers and resource cost: allocation score, realized execution utility, worker profit, manager profit, and full overhead.
- **L1-05:** Run the shared golden vectors and property checks. In particular, bidding higher must not improve that bidder's allocation score; bid order must not change the outcome; awards obey `bid ≤ price ≤ budget`; no terminal transition spends twice.

For a successful solo task, cash profit is `P − ownCost − overhead`; for failure it is `−ownCost − overhead`. Manager cash profit is `actualParentPayout − childPayments − ownCost − overhead`. System execution cost is the sum of unique underlying execution expenses, not the sum of payments and expenses. Preserve the paper's `correctness − alpha × executionCost` metric only after expressing cost in matching units; separately report user spending and total operating cost.

**Contract:** A frozen auction plus evidence returns an allocation record; a permitted transition plus ledger state returns an exact ledger/evidence effect. No network, database, clock lookup, framework, or provider import.

**Does not own:** Storage, endpoints, identity minting, forecasting, or choosing whether an agent participates.

**Exit gate:** Golden examples agree exactly. Boundary and property tests find no rounding-induced underpayment, negative balance, or bid-order dependence. L1 can run with no chain, model key, or internet connection.

### L2 — Agent compatibility and A2A communication

**Depends on:** L0; L1 only where a client previews a market result.

**Decision and rationale.** Define one external participant's contract before integrating many participants. A reference agent is a conformance example, not an application superclass that all external agents must inherit.

**Requirements**

- **L2-01:** Resolve an `AgentRef` to registration metadata and an Agent Card. Validate the declared AgentLance profile and supported A2A version/binding.
- **L2-02:** Define the AgentLance extension payloads: task/award reference, input digest, validation policy, execution reference, artifact/result reference, and status correlation. Use A2A's normal message/task/artifact concepts rather than claiming custom market operations are standard A2A methods.
- **L2-03:** Provide ports for reading a task, submitting a signed bid, observing an award, accepting it, publishing a child, committing a result, and reading settlement. Market writes go to the protocol. A2A transports work and progress; it does not replace transaction authorization.
- **L2-04:** Bind a verified execution signer and payout address to each bid. For v1, an identity owner submits directly or explicitly authorizes a task-scoped signer with an expiring, replay-protected permit. Resolve a nonzero verified payout wallet from the pinned registry implementation; an unset wallet must be restored by its owner before bidding. A registry payment address alone is not permission to bid.
- **L2-05:** Freeze the signer, payout address, identity owner-at-bid, and profile digest for that obligation. A later identity transfer or endpoint change must not redirect an existing payout. New bids require current authority and fresh profile resolution. Reputation remains attached to the identity, with transfer provenance visible.
- **L2-06:** Deduplicate award delivery and execution-start requests by the immutable award/execution reference. Repeated messages return the existing run state instead of invoking the expensive model again.
- **L2-07:** Build a minimal reference worker with a deterministic local executor and in-memory market fixtures. Live LLM execution is unnecessary at this gate.

**Contract:** Agent Card + identity binding + AgentLance profile + signed market behavior. Map A2A task IDs to chain task IDs explicitly; neither ID substitutes for the other. Polling is sufficient for v1; streaming and push notifications are optional features, not core requirements.

**Does not own:** Fleet management, mandatory SDK inheritance, global routing, reputation approval, or market closure.

**Exit gate:** A separate process can receive a fixture award, reject a forged or stale award, execute once, and return a correlated artifact through A2A. Duplicate and out-of-order messages do not create duplicate runs. The same conformance suite can test an independently implemented agent.

### L3 — Monad protocol, escrow, and canonical evidence

**Depends on:** L0–L1 and the authorization/profile contract from L2.

**Decision and rationale.** Put every enforceable economic rule on-chain now, including closure and child funding. Later execution and validation layers supply real inputs to these rules; they do not add missing financial semantics.

**Requirements**

- **L3-01:** Implement funded task creation, irreversible bid admission, allocation, acceptance, result commitment, validation settlement, timeouts, cancellation, withdrawal credits, and linked retries as specified.
- **L3-02:** Verify the configured ERC-8004 identity registry and task-bound bid authorization. Namespace every identity and market reference; do not create a second proprietary identity system.
- **L3-03:** Permit one immutable bid per identity per task. Record each accepted bid and its snapshot-based reputation value. No bid edits, withdrawals, or late insertions in v1.
- **L3-04:** Maintain the best two bids incrementally using the canonical total ordering, so closure is independent of total bidder count. Store/emit sufficient records to reproduce the auction. Do not loop over all agents or bidders during finalization.
- **L3-05:** Read historical family counters at the task's snapshot block and record the derived probability when the bid arrives. The transaction caller cannot choose `p_hat`. The bid's eligibility/authority is fixed at admission; a later owner transfer does not silently delete a leading bid and invalidate the top-two calculation.
- **L3-06:** Atomically settle money credits, receipt, and canonical outcome-counter update. Reject double settlement, replayed attestations, mismatched artifacts, wrong assets, and unauthorized child creation.
- **L3-07:** Enforce independently funded child escrows, cumulative envelope consumption, depth/count bounds, and deadline nesting. Maintain bounded per-parent counters rather than recursive settlement loops.
- **L3-08:** Implement the pinned validator-verification interface and exercise PASS, FAIL, and timeout with fixture attestations. Emit an event suitable for later external feedback publication.
- **L3-09:** Use a single immutable policy version per deployment for v1. No silent admin reassignment of winners, mid-task price changes, or upgrade-dependent reinterpretation of old tasks. Configuration/version changes apply to new markets through an explicit migration plan.

**Contract:** Transactions are commands; accepted state plus events are authoritative. Events include schema version, identity/task references, reason codes, and references needed for replay. Artifact content remains off-chain, but content digests bind what was agreed, submitted, and judged.

**Does not own:** Agent scheduling, HTTP calls, token-count prediction, task decomposition intelligence, UI search, or an LLM-based allocator.

**Exit gate:** Complete a funded solo lifecycle and a funded child lifecycle using deterministic workers/validators. Verify balance conservation, atomic evidence counting, all timeout/refund paths, malicious signer rejection, identity-transfer behavior, and no external receiver blocking settlement. Contract and L1 outputs match the same golden vectors exactly. Show that an arbitrary caller can finalize an eligible auction without the application backend.

### L4 — Agent connectivity, registry discovery, and market observation

**Depends on:** L2–L3.

**Decision and rationale.** Distinguish finding agent profiles from agents finding work. A registry helps people and software resolve participants; it must not become the service that invites selected workers to bid.

**Requirements**

- **L4-01:** Support registration/profile discovery through registry events and lazy metadata/Agent Card resolution. Reuse the configured shared registry; no manual import into an AgentLance worker allowlist.
- **L4-02:** Provide a direct chain-event watcher and a convenience query interface for open root and child tasks. Agents choose which to use.
- **L4-03:** Apply cheap local filters for claimed capability, supported validation template, asset, deadline, budget, current capacity, and policy. Only surviving tasks may incur forecasting expense.
- **L4-04:** Persist event cursors using chain ID, block hash/number, transaction hash, and log index. Resume after disconnects, deduplicate overlap, and handle removed/reorganized events. Wait for the configured confirmation rule before expensive execution; verify current chain state before writing.
- **L4-05:** Expose index freshness and canonical references. An API outage must not prevent direct bids or state reads; historical replay must reconstruct its task/market views.
- **L4-06:** Fetch external metadata lazily, cache by digest/version, enforce size/time limits, and restrict network destinations to prevent metadata URLs from reaching internal services. An unreachable endpoint is an operational state, not a fabricated task failure.
- **L4-07:** Deliver optional A2A award notifications only to the winner. The winner's own watcher remains sufficient to observe the award; notifications are retryable conveniences.

**Contract:** Registry records yield discoverable profiles; market events yield candidate tasks. Neither method returns a privileged “selected agent pool.” Local filters do not affect another operator's ability to bid directly.

**Does not own:** Global broadcast to every endpoint, registration-triggered LLM calls, runtime spawning, centralized worker selection, or authoritative scoring.

**Exit gate:** An agent registered after deployment joins without a code or configuration change to the market. It can ignore irrelevant work with zero model calls, bid through direct chain access while the indexer is offline, and recover from a watcher restart without duplicate bids or runs.

### L5 — Hybrid Cost Oracle and private economic policy

**Depends on:** L1–L4 interfaces. Completed receipts initially come from fixture executions; real L6/L7 data plugs into the same schemas later.

**Decision and rationale.** Forecast privately and pay for intelligence only when useful. Known unit prices are deterministic; future usage is uncertain. Avoid presenting three overlapping estimates as three additive costs. Authoritative reputation already exists in L1/L3; L5 reads it for agent decisions.

**Requirements**

- **L5-01:** Separate `CostEstimator`, `PricingCatalog`, `ExecutionHistory`, optional `JevPredictor`, and `BidPolicy` as small modules. Only define an interface where a real provider, source, or trust boundary exists.
- **L5-02:** Price resource usage once: `C = Σ rate_r × usage_r + fixedFees`. Rates carry units, currency, version, and effective date. Include input/output/cache/reasoning charges and tool fees only where actually applicable to the selected provider.
- **L5-03:** Represent uncertain usage with a finite set of joint scenarios or samples. History and Jev estimate probabilities over the same resource/cost basis. Reprice historical resource quantities using the current pricing snapshot where possible; do not pool incompatible raw currency totals. Do not add a historical total cost to a Jev total cost, or assume dependent token/tool/retry quantities are independent without evidence.
- **L5-04:** Define an explicit, versioned fusion rule. Initial estimator v1 may use `F = w × F_history + (1−w) × F_Jev`, with `w = n/(n+20)` for `n` comparable, adequately observed runs. This is a pragmatic prior-weight choice, not a validated calibration claim. Put provider/runtime/task-family compatibility and recency rules in the estimator version; exclude mismatched observations.
- **L5-05:** If Jev is unavailable, use comparable history; if history is unavailable, use Jev plus deterministic pricing; if both are absent or inadequate, use declared resource bounds or abstain. Unknown/unbounded cost must never become zero cost. Record the fallback mode.
- **L5-06:** Produce mean, P50/P80/P95, scenario probabilities, range/tail assumptions, sample count, provenance, timestamp, and expiry. Quantiles must be ordered and probabilities normalized. An overflow/tail scenario must prevent a false finite P95 when the tail cannot be bounded.
- **L5-07:** Cache by task/input digest, runtime/configuration, pricing, and estimator versions. Enforce per-agent forecast budgets, concurrency limits, and timeouts. No registry-wide Jev or LLM fan-out.
- **L5-08:** Let agent policy select BID or ABSTAIN, risk quantile, failure-risk adjustment, and margin. Return a transparent decision explanation locally. Neither the oracle nor the protocol forces a universal margin or bids on behalf of unrelated operators.
- **L5-09:** Ingest actual usage with provenance labels such as provider-attested, operator-reported, or incomplete. Record zero-success and aborted runs; timeouts may be censored observations, not complete zero-cost runs. Separate execution expenses, forecast expense, validation, and gas.
- **L5-10:** Evaluate P80/P95 coverage and absolute errors against later comparable runs, with sample counts and a rates-plus-history baseline. Do not claim automatic improvement just because more rows exist. Small samples remain explicitly uncalibrated.

Jev's documented Choice primitive returns probabilities across supplied options. AgentLance proposes using suitable, mutually exclusive execution/cost scenarios as those options; the vendor does not supply a ready-made calibrated task-cost oracle. Keep inference and exact pricing separate, pin the model/request version, and use its probability distribution rather than interpreting the returned confidence field as a success guarantee. [TypeSafe Choice documentation](https://docs.typesafe.ai/primitives/choice)

**Contract:** `(task, agent runtime profile, pricing snapshot, comparable history) → CostEstimate`; `(estimate, public market terms, local policy) → bid or abstain`. Predictions can remain private; publication for a demo is optional and visibly labeled. Missing cost telemetry cannot block settlement.

**Does not own:** Winner selection, canonical success probabilities, payment amount, universal agent strategy, provider training, or a production financial price oracle.

**Exit gate:** Unit/currency mismatch, stale prices, provider errors, malformed probabilities, no history, and oversized tails have deterministic handling. For irrelevant tasks there are no inference calls. A fixture market can run with Jev disconnected, and recorded actual usage can be reconciled with a specific estimate without affecting its settled price.

### L6 — Awarded execution and bounded delegation

**Depends on:** L2–L5 and L3's existing validator/settlement contract.

**Decision and rationale.** Execution starts from an accepted award, not from a central planner selecting a runtime. Delegation creates funded market work, not new agents. Begin with solo completion, then use the identical protocol for children.

**Requirements**

- **L6-01:** Verify the canonical award and input digests, accept it, and execute only once per execution reference. Persist enough run state for restart and recovery.
- **L6-02:** Isolate each logical agent's signing authority, limits, private history, and running tasks even when a shared process hosts several identities. Shared wallets/ownership must be disclosed, not presented as independent operators.
- **L6-03:** Run an expensive execution model only for accepted work, or an explicit locally budgeted reasoning operation. Availability checks and ordinary filtering must be code paths without model inference.
- **L6-04:** Enforce local resource, retry, and wall-time limits. Reserve capacity before bidding so multiple concurrent awards do not silently overcommit the runtime.
- **L6-05:** Permit solo execution or a manager-generated decomposition. Validate each proposed child's input/output contract, own validator template, budget funding, task family, deadline nesting, depth, and envelope before publication.
- **L6-06:** Observe child markets through the same watcher and consume independently validated artifacts. The manager cannot choose winners or directly mark children successful.
- **L6-07:** Use the fallback rules in §5.5. Do not keep recursively decomposing a task without a bounded plan. Support parallel independent children; arbitrary sibling dependency graphs wait for a later version.
- **L6-08:** Commit a final artifact and record usage, including failed planning/synthesis and tool calls. Keep child-payment accounting separate from the manager's own provider usage.
- **L6-09:** Preserve a small structured private execution/market history. Optional reflection summarizes an agent's own experience within its budget; it does not generate shared reputation or run after every ignored task.

**Contract:** Accepted award → persisted run → artifact/result commitment. A proposed child is a normal funded `TaskSpec` plus parent authorization. The market sees the same external interface regardless of the agent's internal framework.

**Does not own:** Validator consensus, authoritative success declaration, agent registration on someone else's behalf, or creating an internal registry of hardcoded worker classes.

**Exit gate:** Complete one real solo job, then a parent with two child markets using the same protocol. Demonstrate local fallback after an unallocated child, manager restart without duplicate spending, resource-budget exhaustion, and a parent failure that leaves a successful child's payment intact. Deterministic fixture validation is sufficient until L7.

### L7 — Real validation, settlement reconciliation, and portable evidence

**Depends on:** L3 and L6; outputs feed existing L4 projections and L5 history interfaces.

**Decision and rationale.** Replace fixture validators and telemetry consumers with real ones. Payment semantics, state transitions, and evidence rules already work; this layer closes operational integrations without redesigning prior layers.

**Requirements**

- **L7-01:** Run the task-pinned objective evaluation template in an isolated runner with resource limits. Treat agent-generated artifacts as untrusted input. Store reproducible evidence and sign only the matching result digest.
- **L7-02:** Submit or allow anyone to relay the attestation. Verify that a worker cannot approve itself, alter criteria after bidding, replay another task's verdict, or settle the same task twice.
- **L7-03:** Consume settlement events to build receipts, cost history, agent-local summaries, and exportable feedback. Store durable retry state; avoid an assumed distributed transaction across chain, database, and external services.
- **L7-04:** Publish real AgentLance outcomes to the configured ERC-8004 Reputation Registry using a bounded publisher adapter. A publisher records each receipt's export status so anyone triggering a retry cannot duplicate its feedback. Its role must remain distinct from the agent owner/operator where the pinned registry requires that separation.
- **L7-05:** Include task family, receipt, validation policy, artifact/evidence digests, and market references in exported evidence. Publish no “warm-up successes” or synthetic prior as feedback. External observers must be able to distinguish AgentLance receipts from arbitrary third-party ratings.
- **L7-06:** Keep export failure separate from canonical settlement. Display “settled; feedback publication pending” when appropriate. An ERC-8004 outage or rejected publication cannot reverse earned payment or count a second outcome.
- **L7-07:** Label actual-cost provenance; reconcile forecast and observed resource cost without rewriting the bid, price, or result. Retry ingestion idempotently by execution/cost-report reference.

ERC-8004 offers identity, reputation-feedback, and validation registries; it does not prescribe AgentLance's scoring formula or payments. The specification is currently a draft. Pin the implementation/version and addresses, and test its actual authorization behavior. V1 requires identity and reputation interoperability; writing to the ERC-8004 Validation Registry is optional because AgentLance already verifies and records its task verdict. Do not build two competing authorities for settlement. [ERC-8004 specification](https://eips.ethereum.org/EIPS/eip-8004)

**Contract:** Artifact + task-pinned policy → attestation/evidence; canonical settlement receipt → exactly one local projection effect and at most one export by the configured publisher. A client looking only at A2A status must still query chain settlement before displaying payment as final.

**Does not own:** New auction math, discretionary outcome editing, retroactive validation rules, subjective arbitration, or claiming a signature proves correctness by itself.

**Exit gate:** A full real lifecycle produces a verifiable result, correct payment/refund, one canonical outcome observation, portable ERC-8004 feedback, and a cost reconciliation record. Repeat processing, stop the exporter, restart it, and replay events without duplicate payment or evidence. Validator timeout must still release escrow.

### L8 — Application, explorer, and demonstrable interoperability

**Depends on:** Stable public interfaces of L3–L7. A minimal diagnostic CLI/script may exist earlier; polished UI waits until the lifecycle is working.

**Decision and rationale.** The application explains and invokes the protocol. It must not become the hidden allocator, scorer, or sole market access point.

**Requirements**

- **L8-01:** Provide task creation with required validation terms, budget, deadlines, alpha, and delegation permissions. Show the difference between budget, bid, reserved price, payout, refund, and actual resource cost.
- **L8-02:** Expose open markets, canonical bidders/scores, winner and tie-break explanation, execution progress, bounded task tree, validation evidence, settlement, and feedback-publication state.
- **L8-03:** Show identity provenance, claimed skills, endpoint status, family-specific prior, real success/failure counts, scoring snapshot, and policy versions. New agents must not appear to have completed jobs.
- **L8-04:** Display forecast source, sample count, fallback mode, currency, and actual-cost provenance when an operator elects to share them. Never invent competitors' private estimates for a chart.
- **L8-05:** Surface pending versus confirmed chain activity and stale index data. A displayed local transaction is not an accepted bid or final payment.
- **L8-06:** Demonstrate an external participant joining without modifying AgentLance code. Several logical agents sharing one runtime are acceptable for an economical demo, but must be labeled as such.
- **L8-07:** Provide a reproducible demo and short explanation tying the system to identity, evidence, and agent trust. Include attribution to the paper and clearly describe v1 trust and incentive limitations.

**Does not own:** Duplicated auction/reputation formulas, synthetic transaction histories presented as live, invisible fallback to a central router, or compulsory wallet custody for agents.

**Exit gate:** A reviewer can create a task and trace its funds, identity, bid, award, child work, validation, and settlement from public references. Include one failure/refund demonstration and one genuinely independent agent process. The demo does not require dozens of model subscriptions or 100 running agents.

## 4. Dependency rules and mapping to `appflow.md`

### Stable build boundaries

| Build layer | Uses already defined contracts | What the next layer can rely on |
|---|---|---|
| L0 Specification | User requirements and audited mechanism | Objects, invariants, fixtures, authority, failure semantics |
| L1 Pure core | L0 | Deterministic auction/reputation/accounting behavior |
| L2 Agent interface | L0, optional L1 previews | Agent profile, authorization, A2A mapping, reference worker |
| L3 Protocol | L0–L1, L2 signing contract | Full financial lifecycle, evidence, and child-funding API |
| L4 Discovery | L2–L3 | Replaceable read views and resumable task observation |
| L5 Economics | L1–L4 schemas and ports | Provenanced estimates and local bid decisions |
| L6 Execution | L2–L5 | Restartable solo/delegated runs and result artifacts |
| L7 Validation/reconciliation | L3 and L6 | Real evidence, settlements, cost feedback, portable reputation |
| L8 Application | Published L3–L7 interfaces | Reviewable end-to-end product |

The runtime feedback loop does not imply circular source dependencies. For example, L5 consumes a `CostReport` schema defined in L0; it never imports L7's worker. L6 consumes a settlement status interface already implemented in L3; it never needs a concrete L7 validator class. Test adapters provide these inputs until their real producer exists.

Core modules import only domain contracts. Infrastructure adapters implement ports and depend inward. Runnable applications compose modules; modules never import an application's UI or startup code. If on-chain and off-chain implementations require different languages, share schemas and golden vectors rather than forcing a common runtime or copying undocumented formulas.

## 8. Final directory layout

This is the intended repository layout after the relevant layers are implemented. It is **not** an instruction to scaffold all directories now. Create a file when its layer has a concrete requirement; combine small modules until a real boundary justifies splitting. Extensions, build tools, package managers, and languages remain undecided.

```text
agentlance/
├── README.md                         # purpose, paper attribution, run/demo entry points
├── docs/
│   ├── layers.md                     # this architecture and implementation roadmap
│   └── decisions.md                  # concise dated changes to locked protocol choices
├── specs/
│   ├── protocol.md                   # objects, authority, states, events, reason codes
│   ├── market.md                     # allocation, price, units, rounding, invariants
│   ├── reputation.md                 # priors, family taxonomy, snapshots, evidence
│   ├── agent-profile.md              # identity binding + AgentLance A2A profile
│   ├── economics.md                  # forecasts, provenance, policy inputs, metrics
│   ├── execution.md                  # accepted awards, recovery, bounded delegation
│   ├── validation.md                 # templates, attestations, timeout/evidence rules
│   ├── acceptance.md                 # requirement IDs → scenarios and test locations
│   ├── schemas/                      # machine-readable public boundary formats
│   └── fixtures/                     # golden vectors and clearly synthetic examples
├── contracts/
│   ├── src/
│   │   ├── market/                   # lifecycle + bids + top-two allocation
│   │   ├── accounting/               # escrow credits + child spending envelope
│   │   ├── reputation/               # outcome checkpoints and snapshot reads
│   │   └── integrations/             # identity checks, verdict verifier, feedback publisher
│   └── tests/                        # contract behavior, invariants, shared vectors
├── modules/
│   ├── domain/                       # schema bindings, identifiers, pure value validation
│   ├── market-core/                  # off-chain reference math and accounting/reputation rules
│   ├── agent-client/                 # signing, task observation, compatibility utilities
│   ├── economics/                    # pricing, forecast fusion, history, local bid policy
│   ├── execution/                    # run state, solo execution, child-market coordination
│   ├── validation/                   # objective template runner + attestation construction
│   └── adapters/
│       ├── chain/                    # contract/RPC access; indexer event decoding
│       ├── a2a/                      # standard transport and AgentLance payload mapping
│       ├── registry/                 # profile resolution and feedback export
│       ├── pricing-and-jev/          # provider boundaries, no allocation decisions
│       └── storage/                  # only persistence/artifact adapters actually required
├── apps/
│   ├── service/                      # composes index/read API, validator, export workers
│   ├── agent-runtime/                # optional shared runtime for logical demo identities
│   └── web/                          # requester interface and protocol explorer
├── examples/
│   ├── agents/                       # declarative sample profiles/policies, no agent fleet
│   └── tasks/                        # small objective solo + subcontract demo cases
├── tests/
│   ├── conformance/                  # any external agent can run the profile checks
│   ├── integration/                  # real adapter boundaries, restart/replay behavior
│   └── scenarios/                    # complete success, failure, and child-payment flows
├── deployments/
│   └── manifests/                    # chain, assets, addresses, versions, policy constants
└── scripts/                          # only actual spec-check, deploy, and demo commands
```

`contracts/src/` entries denote cohesive responsibilities; they do not require a separate deployed contract for each directory. Co-locate tightly coupled state when that makes invariants easier to enforce. Conversely, keep an ERC-8004 feedback publisher separate from the agent wallet so publication authority is clear.

| Directory | First created in | Boundary rule |
|---|---|---|
| `specs/`, `docs/` | L0 | Specification precedes implementation; public changes have explicit versions |
| `modules/domain`, `modules/market-core` | L1 | No infrastructure or application imports |
| `modules/agent-client`, A2A/registry adapters, conformance examples | L2 | Standard external interface; no agent-framework dependency required |
| `contracts/`, chain adapter, deployment manifest | L3 | Enforces economic invariants; reads no off-chain model output as authority |
| Event watching and read projections within existing modules/apps | L4 | Rebuildable from canonical records; no additional task authority |
| `modules/economics`, pricing/Jev adapter | L5 | Forecasts and bid policy only; imports published receipt schemas |
| `modules/execution`, runnable agent composition | L6 | Acts under awarded identity and budget limits |
| `modules/validation`, real service workers/export paths | L7 | Implements the already frozen outcome and evidence contracts |
| `apps/web`, complete demonstration scenarios | L8 | Consumes existing behaviors; does not reimplement them |

Place pure-module unit tests alongside the owning module in the chosen language's convention. Reserve top-level test directories for checks that cross a real boundary. Do not create parallel `utils`, `common`, `helpers`, generic repository factories, or one subclass per demo agent to disguise unclear ownership.

### Compatibility contract

- Canonical schemas, events, signatures, and mechanism policies are versioned. Additive optional fields must preserve prior readers; removed or reinterpreted fields require a new version and migration fixtures.
- Once a task is created, its versions and economic terms do not change. Updating the estimator may affect future bids, never settled prices or stored evidence.
- Consumer-facing behavior is covered by shared fixtures: both the reference core and contract implementation must produce the same exact outcome.
- Do not let an adapter introduce a second domain object with a subtly different definition of “task,” “result,” “success,” or “paid.”
- A later layer can reveal a real earlier design defect. Correct it with a documented specification change and regression evidence; do not preserve broken behavior just to claim that earlier files never change.
- Keep expected error handling explicit. Silent central routing, invented reputation, unlimited retry loops, and “temporary” uncollateralized child spending are not acceptable fallbacks.
