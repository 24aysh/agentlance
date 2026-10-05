# Layer 1 — deterministic protocol core

Status: implemented and acceptance gate passed on `layer-1-core` (2026-10-05). This is the offline reference layer; no on-chain market or live adapters are implemented.

This is the implementation plan and acceptance contract for L1-01 through L1-05 in [layers.md](../layers.md). Implement against the existing [L0 baseline](layer-0.md), [protocol](protocol.md), [schema](schemas/protocol.schema.json), [catalog](catalog.json), [signing rules](signing/README.md) and [deployment policy](deployment-policy.md). Those files own public protocol semantics. This document fixes Python responsibilities, internal interfaces, fixture interpretation and the L1 test gate. It does not replace the public schema or introduce another protocol version.

## 1. Scope and completion meaning

L1 produces an offline, deterministic reference core. Given explicit policy, prior state, a command and observed context, it returns the next state and canonical event payloads, or one stable rejection with no effects. It also evaluates frozen auctions, reduces canonical outcome evidence and calculates cost-aware analytics. L3 must later reproduce the economic behavior on Monad; running this Python core does not allocate a real task or move funds.

| Requirement | Required deliverable |
|---|---|
| L1-01 | Exact scoring, canonical ordering, incremental top two, outside option and capped critical payment |
| L1-02 | Lazy family prior, historical checkpoints, immutable receipt reduction and identity-transfer invariance |
| L1-03 | All ten command transitions, authority predicates, deadlines, escrow/credits, child envelopes and retries |
| L1-04 | Resource expenses, overhead, actual worker/manager cash profit, user spending and realized execution utility |
| L1-05 | Existing applicable goldens executed unchanged, compact boundary/error matrices, invariant properties and one composed tree scenario |

Excluded: Solidity, RPC, registry reads, cryptographic verification/signing, A2A, event indexing/reorg recovery, persistence, validators, artifact fetching/hashing, cost prediction/Jev, worker execution, scheduling and UI. There is no local agent fleet, router, server or automatic action loop. A2A status and cost estimates are not transition inputs.

“End to end” here means the whole **L1 boundary**, from decoded records through pure transitions to receipts, evidence and analytics. It does not mean the complete deployed AgentLance application. “Full coverage” means every requirement, applicable fixture, owned rejection and reachable branch is exercised. Coverage is not a proof over all possible inputs.

## 2. Reuse and implementation sequence

Inspection baseline: no `modules/` implementation exists. Reuse the single JSON Schema, numeric catalog, ABI, 18 market fixtures, 34 lifecycle fixtures, 15 reputation fixtures, 17 delegation fixtures and applicable replay cases. Existing `scripts/check_specs.py` checks syntax and signatures; it does not execute market logic. Its strict JSON reader, numeric format checks and URI checks can be extracted; its file loading, subprocess calls and signing imports must stay outside the domain/core.

Use Python 3.12 and standard-library integers, dataclasses, tuples and mappings. Keep `uv` and Ruff. During implementation add pytest, Hypothesis and coverage.py to the existing dev group and lock them. Coverage.py is used directly, so pytest-cov is unnecessary. Move the existing jsonschema dependency from dev to runtime only when the domain schema boundary is introduced; do not add a second validation/model library. No Pydantic, Web3, A2A, Ethereum signing or application framework dependency belongs in the core.

Use the following files when needed; do not create placeholders. Python uses `market_core` rather than the roadmap's descriptive `market-core` spelling so imports work directly from the repository root. Keep the current single uv project and `package=false`; separate distribution packages are unnecessary in L1.

| File | Ownership |
|---|---|
| `modules/domain/records.py` | Strict in-memory JSON/schema boundary, reusable format assertions, numeric/ref decoding; no file I/O |
| `modules/market_core/state.py` | Internal state/context/result dataclasses and invariant checks; public record shapes remain schema-owned |
| `modules/market_core/market.py` | Score, ordering, top-two update and allocation |
| `modules/market_core/reputation.py` | Probability, snapshot lookup and receipt/evidence reduction |
| `modules/market_core/accounting.py` | Terminal credit arithmetic, child envelope arithmetic and withdrawal arithmetic |
| `modules/market_core/transitions.py` | Ordered command guards and composition into atomic state/event results |
| `modules/market_core/analytics.py` | Expense deduplication, completeness/unit handling and analytics |
| `modules/domain/test_records.py`, `modules/market_core/test_*.py` | Colocated boundary/unit/golden/property tests |
| `tests/test_layer1.py` | One composed cross-module market/delegation/accounting/analytics scenario |
| `tests/conftest.py` or root `conftest.py` | Only shared fixture loading/builders actually needed; choose root if module tests also consume them |
| `specs/fixtures/layer-1.json` | Reviewed additional boundary, binding and analytics examples specified below; no generated expectations |
| `specs/schemas/scenarios.schema.json` | Add `layer-1` to the test-only domain enum; preserve all existing case/error/event grammar |
| `scripts/check_specs.py`, `scripts/test_check_specs.py` | Import extracted pure validation helpers; preserve current L0 checks |
| `scripts/check_layer1.py`, `scripts/test_check_layer1.py` | Run the complete gate and reject missing source files or nonzero coverage gaps |
| `pyproject.toml`, `uv.lock`, `README.md` | Test dependencies, coverage/test commands, implementation status |

Add package `__init__.py` files only for import structure; do not create barrel APIs or wildcard exports. Imports are `modules.domain.records` and `modules.market_core.market`, etc. Core imports domain/standard library only. Domain may import jsonschema. Tests/tools may load files and import external verifiers. No module reads schemas, catalogs, configuration, environment variables or the clock at import time.

Implementation order:

1. Extract domain boundary helpers without changing existing L0 behavior; add boundary tests.
2. Implement market arithmetic/top two and execute all market goldens.
3. Implement reputation snapshots/reduction and execute applicable reputation goldens.
4. Implement ledger/envelope arithmetic; execute accounting/delegation projections.
5. Compose the ten command transitions, bound observation checks, events and rejection ordering; execute lifecycle and authority cases.
6. Implement analytics and its reviewed examples; add the composed scenario and properties.
7. Run the complete gate in §11, inspect coverage gaps and update implementation status only after passing.

No implementation decision about money, authority, deadlines or identity is deferred to a later layer. Deployment addresses and adapter verification remain deployment/adapter facts, supplied as inputs rather than fabricated here.

## 3. Domain boundary and errors

### 3.1 Public records

Keep canonical records as plain mappings with exactly the existing schema's field names and wire values. Do not hand-copy every schema into dataclasses or an adapter-specific `Task`/`Bid` model. Internal bookkeeping dataclasses in §4 are not new public records and have no wire serialization contract.

`decodeRecord(rawBytes, typeName, schema) -> record` accepts a schema mapping explicitly supplied by the caller. It performs strict UTF-8 decoding, rejects a BOM, duplicate keys, floating-point tokens, non-finite numbers, missing/unknown fields and unknown types/versions, and uses Draft 2020-12 with all existing custom format assertions enabled. No remote `$ref` resolution is allowed; the supplied protocol schema uses local references. The loader that reads the repository schema is test/application infrastructure, not domain code.

`validateRecord(record, typeName, schema) -> None` applies the same shape/format validation to an in-memory record. JSON `true` is not an integer, and `1.0` is not accepted as an integer version/count/probability. Inputs must consist only of the JSON value types permitted by the selected definition. Shape-valid records can still violate economic/cross-field rules; the relevant core guard owns those rules.

Canonical decimal strings remain strings in public records. Arithmetic helpers decode to Python `int` and encode with `str` on output; never use floats, Decimal, rounding tolerances, saturation or modulo wrapping. Ref equality uses decoded numeric fields and validated lowercase hex bytes. `agentKey(agentRef)` returns the exact 84-byte sort key; `taskKey(taskRef)` returns `(chainId:int, market:20 bytes, taskId:int)`. No JSON hash is used as an identity key.

The boundary functions raise `ProtocolViolation(code)` for expected malformed input. Its `code` is a name from the existing catalog, never a new wire code. Bad representation/shape is `INVALID_ENCODING`; a well-formed integer outside its specified width is `INVALID_RANGE`. Zero budget/alpha within width is left to `INVALID_BUDGET`/`INVALID_ALPHA` in economic guards when using the arithmetic test grammar. A public TaskTerms record whose schema forbids zero fails schema validation first. Test the two boundaries separately; do not claim they have identical rejection order.

`applyCommand` accepts schema-validated records and validates all semantic relationships itself. It returns `Rejected(code)` for expected protocol failures. It does not catch arbitrary programming errors and turn them into protocol failures. Invalid internal state or inconsistent adapter observations raise `ValueError` before effects; these are integration faults, not terminal outcomes or new chain error codes.

### 3.2 Numeric validation

Economic scalars obey the widths in L0: uint96 budget/bid/alpha; uint64 task IDs, times, blocks and counters; uint256 chain/agent IDs, nonces and aggregate credits; signed int256 score. Reject Python booleans in integer helper inputs. Validate before arithmetic; widened intermediate integers are intentional. Enforce reduced positive alpha and budget > 0. Probability helpers accept only `0 <= p <= Q`, `Q=1_000_000`.

For `evaluateAuction`, check budget, alpha, then bids in supplied order; for each bid check encoding/ranges, duplicate identity and bid <= budget. An invalid auction returns no partial allocation. Order independence is required for valid inputs; it is not a promise about which of several malformed rows is reported first.

## 4. Explicit state and trusted context

### 4.1 State

Internal numeric bookkeeping uses Python integers with the above bounds. Canonical nested records retain wire representations. Use the following minimal state; no repository/storage interface is needed:

| Internal value | Fields and invariant |
|---|---|
| `CorePolicy` | `chainId`, `market`, `identityRegistry`, `validator`, `asset`, `policyVersion=1`, `minimumAcceptanceWindow=120`, `synthesisSlack=120`, `maxDepth=2`, `maxChildren=4`; projection of a verified deployment manifest, no RPC URLs |
| `TaskState` | `spec:TaskSpec`, `status`, `bids` keyed by AgentRef key, `topTwo` tuple of at most two admitted bids, `allocation:Allocation|null`, `ownWorkReserveAtoms`, `childrenCreated`, `activeChildren`, `reservedChildBudgets`, `committedChildPayouts`, `result:ResultCommitment|null`, `receipt:SettlementReceipt|null`, `escrowAtoms` |
| `CoreState` | `taskCount`, `tasks` keyed by TaskRef key, `credits` keyed by address, `usedNonces`, `reputation`, `depositedAtoms`, `withdrawnAtoms` |
| `ReputationState` | `checkpoints` keyed by `(AgentRef key, taskFamily)`, and `seenReceipts` keyed by TaskRef with the immutable receipt retained for conflicting-replay detection |
| `Checkpoint` | `blockNumber`, `successes`, `failures`; stored blocks strictly increasing, same-block writes replace the last checkpoint |

`taskCount` is the last assigned ID and starts at zero. A task's bid count is `len(bids)`, never a separately mutable counter. `topTwo` contains the two greatest admitted bids by §5, including non-positive scores. `allocation` remains null until allocation is attempted; a no-award allocation can be retained locally, but emits only TaskSettled. Other pre-award terminal paths keep it null.

Before settlement, escrow equals the full budget, even after an award or result submission. After settlement it is zero. Status SETTLED iff receipt is present. A result exists only in SUBMITTED or an appropriate terminal state. Awarded paths retain winner/signer/payout. Own-work reserve starts at zero and is fixed by acceptance. Initial child counters are zero.

The reference state owns admitted bid storage to detect duplicates and audit a frozen auction. Allocation closure reads topTwo only; a Python dictionary lookup/copy is not a requirement that Solidity scan or copy storage. Pure functions must not mutate input dictionaries, lists or nested records. Returned state/events must not share writable objects with input state/commands. Defensive copying is sufficient; no persistent collection dependency is required.

Terminal immutability applies to outcome, price, result and receipt. A terminal parent's child-accounting counters may still change when an outstanding child settles. This is required by the existing late-child rule and is not a second parent settlement.

### 4.2 Context and adapter facts

`CommandContext` contains `caller`, `blockNumber`, `blockTimestamp`, `valueAtoms`, `reentrant`, `identityObservation|null`, `signatureObservation|null`, `transferSucceeded|null`. Caller/time/block/value are explicit simulated execution facts. No helper infers them. All observations are from the same execution context, never public command fields.

* `IdentityObservation`: `agentRef`, `available:bool`, `owner:address|null`, `verifiedWallet:address|null`. Available observations require a nonzero owner; null wallet means unset. Lookup failure/nonexistent token is `available=false`. Require the observed AgentRef to equal the offered AgentRef. This is a registry adapter result, not an assertion by the agent.
* `SignatureObservation`: `primaryType` (`BidPermit` or `ValidationVerdict`), `chainId`, `market`, `signer`, `record` (the exact permit or validation record being checked), `signature`, `valid:bool`. Equality of all these bindings to the current command is mandatory before using `valid`. A correct signature for another record/domain cannot authorize this command. This value represents a verifier result; it is not a substitute wire authentication mechanism.
* `transferSucceeded`: used only to simulate the result of the attempted native withdrawal call, after withdrawal guards pass. False yields TRANSFER_FAILED and total rollback; true yields the debit/event. L1 performs no callback. Reentrant context is rejected before transition evaluation; actual EVM call ordering and the guard implementation belong to L3.

Require the needed observation on direct/permit bid, verdict and withdrawal paths; absence is an integration `ValueError`, not guessed success/failure. Registry unavailable and signature invalid are explicit observations and produce the prescribed protocol rejection. Identity observations are never required again to accept, execute or pay an admitted award.

Only crypto verification is delegated to the signature observation. The core still checks field equality, current owner, frozen actor, wallet, role separation, namespace, nonce scope and expiry itself. Never accept a generic `authorized=true` context. Exact ECDSA/ERC-1271 mechanics remain in L2/L3 and existing L0 signing checks.

### 4.3 Result

`applyCommand(state, command, context, policy) -> Applied | Rejected` is the only composed command entry point. `Applied` contains `state`, ordered `events:tuple[Event,...]`, and `evidence:tuple[ReputationEvidence,...]`. Evidence is a local projection, not an extra chain event. `Rejected` contains only `code`; input state remains unchanged. Each successful command emits exactly the event(s) specified below. Pure standalone arithmetic functions emit no events.

Return Event payloads, not EventEnvelope: the core cannot invent transaction hashes, log indices, block hashes or finality. Tests validate returned payloads against the existing Event schema. Do not add an event merely to expose an internal calculation.

## 5. Market arithmetic — L1-01

Required functions:

| Function | Contract |
|---|---|
| `calculateScore(p, alphaNum, alphaDen, bidAtoms)` | Return exact signed integer `p*alphaDen - Q*alphaNum*bidAtoms` after bounds/alpha checks |
| `updateTopTwo(topTwo, admittedBid)` | Return at most two bids ordered by descending score, ascending agentKey; caller has already rejected duplicate admission |
| `evaluateAuction(auction)` | Consume the market fixture's input shape and return its exact expected shape, including REJECTED/reason cases; this is a local arithmetic projection, not a public Allocation |
| `calculateAllocation(taskSpec, topTwo, allocatedAt)` | Return the canonical Allocation, using the same pricing primitive as evaluateAuction; never scan all admitted bids |

For a valid auction, calculate all scores from its frozen `p` and bid. Do not trust a caller-supplied score. In command admission, derive `p` from the creation snapshot, and store both counters and score in the canonical Bid. Later allocation never replaces them with current reputation.

If there is no bid or the best score is <= 0, return UNALLOCATED, null winner/scores/criticalAtoms and reservedAtoms=0. Otherwise let `second=max(0, runnerUpScore)`; a tied runner-up counts. Compute:

```text
criticalAtoms = (winner.p * alphaDen - second) // (Q * alphaNum)
reservedAtoms = min(budgetAtoms, criticalAtoms)
```

The canonical Allocation includes those null fields. The existing market fixture projection intentionally uses only `{outcome:"UNALLOCATED",winner:null,reservedAtoms:"0"}`; evaluateAuction returns that exact smaller shape so the reviewed L0 expectations remain unchanged.

The critical numerator is nonnegative on the awarded path. Do not divide individual score terms, round up, use the second distinct score or subtract one for a tie. A lower canonical identity wins a score tie; price still follows the formula. AwardId is 1, allocatedAt is supplied block time. For no award it is 0. A positive-score zero-price award is valid and can accept with reserve 0.

Check `winningBid <= reservedAtoms <= budgetAtoms`. The raw critical price fits uint96 because `p <= Q`, alphaNum >= 1 and alphaDen <= uint96 max. Replacing top-two insertion with sorting the whole bid collection during closure fails the L1 design even if examples pass.

## 6. Reputation — L1-02

`calculateProbability(successes, failures)` returns `Q*(1+successes)//(2+successes+failures)`. Widen before addition/multiplication. Each counter is uint64; accepted canonical histories also satisfy total observations <= taskCount <= uint64 max. The standalone probability helper can evaluate any individually bounded pair; the reducer enforces reachable-history bounds.

`snapshotCounters(reputation, agentRef, taskFamily, snapshotBlock)` returns the latest cumulative pair at or before the supplied block, or `(0,0)`. Creation pins `snapshotBlock=blockNumber-1`; block zero creation is INVALID_RANGE. Same-block settlements do not affect that task's admitted probabilities, even if bids arrive in a later block. A later-created task sees the last cumulative checkpoint of the earlier block. Ownership is not a reputation key.

`reduceReceipt(reputation, taskSpec, receipt, blockNumber, taskCount)` returns new reputation plus optional ReputationEvidence. It accepts canonical, mutually consistent task/receipt records, not arbitrary third-party feedback. Namespace, receipt/task binding, winner, terminal reason/counter effect and result/policy relationships must agree. Impossible or out-of-order histories are `ValueError`; the caller must supply canonical order or an earlier snapshot after rollback.

| Receipt reason | Counter delta | Evidence |
|---|---|---|
| SUCCESS | successes +1 | SUCCESS |
| VALIDATION_FAILED, NO_SHOW, EXECUTION_TIMEOUT | failures +1 | FAILURE |
| VALIDATOR_TIMEOUT, UNALLOCATED, ALLOCATION_EXPIRED, CANCELLED | none | none |

Record the seen receipt even when it has no observation. Identical repeat returns unchanged state and no new evidence. Conflicting receipt under the same TaskRef is an integration fault, never “last write wins.” A new observation increments once, writes/replaces that block's checkpoint and emits no synthetic prior. Evidence fields come only from task/receipt: receiptRef, winner, taskFamily, counter outcome, reason, pinned policy digest, optional result digest and optional accepted validation evidence digest. No-show and execution-timeout evidence have null result/evidence digests.

Do not perform ownership updates inside this reducer. Transfer fixtures change only the external owner observation; subsequent probability remains attached to the same AgentRef. Synthetic family-isolation fixtures test the keyed reducer without expanding the deployable family allowlist.

## 7. Commands, authority and accounting — L1-03

### 7.1 Guard order

Use [protocol.md](protocol.md)'s order: decoding/range boundary → wrong non-creation value → reentrancy → namespace/reference existence → already settled → state → actor → time → command-specific guards. `createChildTask` resolves/checks the parent as its target; `withdrawCredit` has no task target. Unknown/foreign TaskRef is UNKNOWN_TASK. AwardId mismatch is WRONG_STATE. A foreign AgentRef namespace is IDENTITY_UNAVAILABLE; permit TaskRef mismatch is UNKNOWN_TASK and permit AgentRef/offer mismatch is INVALID_SIGNATURE. These choices specialize the existing namespace/signature rules without adding error codes.

Apply task terminal checks before command-specific bindings, so duplicate settlement is ALREADY_SETTLED even if a supplied verdict is now expired. Unsuccessful transitions consume no nonce, ID, child slot, deposit or credit. Later observations must not overwrite an earlier guard failure. Invalid internal context/state is diagnosed separately before invoking the command contract.

### 7.2 Complete command effects

The table is a composition plan; all referenced public fields are already frozen in the schema. Each row additionally obeys §7.1 and the original command catalog's guards/errors.

| Command | Checks and effects | Event |
|---|---|---|
| createTask | Validate terms below; optional retry must be caller's terminal non-success root; exact fresh value=budget; taskCount below max. Allocate next ID, requester=caller, parent=null, root=self, depth=0, time/block from context, snapshot=block−1; increase depositedAtoms and escrow by budget | TaskCreated with complete TaskSpec |
| submitBid | OPEN and time<biddingClose. Resolve identity/current owner/wallet. Direct path: permit and signature both null, caller=current owner. Permit path: both present, all offer fields equal, signed owner=current owner, bound valid verifier result, nonce unused, time<expiry. Wallet nonzero and equals payout; validator distinct from owner/signer/payout. Bid<=budget, identity not previously admitted. Derive/freeze snapshot counters,p,score; append bid and update topTwo; consume permit nonce only on success | BidAccepted including nullable permitNonce |
| allocateTask | OPEN and biddingClose<=time<allocationBy. Use stored topTwo. Positive winner fixes Allocation and changes to AWARDED; no winner invokes terminal accounting as UNALLOCATED | TaskAwarded **or** TaskSettled, never both |
| acceptAward | AWARDED, executionRef matches award 1, caller=frozen executionSigner, time<acceptBy, 0<=ownWorkReserveAtoms<=reservedAtoms. Set RUNNING and fix own reserve | AwardAccepted |
| createChildTask | Parent RUNNING, caller=frozen parent signer, time<parent.resultBy. Validate terms, delegation/deadline/retry/envelope/funding rules below. Allocate fresh ID, requester=caller, parentRef=parent, rootRef=parent.rootRef, depth=parent.depth+1, own snapshot. Reserve full budget in parent and increment active/lifetime children | TaskCreated only |
| submitResult | RUNNING, matching executionRef, frozen signer, time<resultBy, activeChildren=0, valid artifact ref. Store ResultCommitment with supplied time and change to SUBMITTED | ResultSubmitted |
| settleVerdict | SUBMITTED and time<validationBy. Match executionRef/winner/result digest, then pinned policy digest, then pinned validator/bound verifier result, unused nonce and time<expiry. Store submitted record in receipt, consume verdict nonce and settle PASS as SUCCESS or FAIL as VALIDATION_FAILED | TaskSettled |
| expireTask | OPEN at/after allocationBy → ALLOCATION_EXPIRED; AWARDED at/after acceptBy → NO_SHOW; RUNNING at/after resultBy → EXECUTION_TIMEOUT; SUBMITTED at/after validationBy → VALIDATOR_TIMEOUT. Earlier is TOO_EARLY. No timeout runs automatically | TaskSettled |
| cancelTask | Requester, OPEN, time<biddingClose and no admitted bids. Settle CANCELLED. Accepted bids of any score prevent cancellation | TaskSettled |
| withdrawCredit | amount>0, nonzero receiver, caller credit>=amount. Simulate debit then external success/failure using explicit observation. On success reduce credit and increase withdrawnAtoms by amount; on failure preserve everything | CreditWithdrawn on success only |

Nonce key is `(market, signer, primaryType, nonce)` with chain scope supplied by CorePolicy. Bid signer for this purpose is permit.owner; verdict signer is pinned validator. Nonces are arbitrary unused values, not sequential counters. Direct bids use none. Signature expiry equality rejects. Transfer before admission rejects an old owner's permit with OWNER_CHANGED; after admission it cannot change frozen authority or payout. Transfer back to the original owner can re-enable a still-unused/unexpired permit, as explicitly frozen in L0. ERC-721 approval alone never grants direct bidding authority.

For competing signature-path errors, use protocol catalog order: bid owner/wallet checks, invalid signature/bindings, nonce, expiry, role conflict, budget, duplicate. Verdict checks result, policy, validator/signature, nonce, expiry. A valid signature does not bypass any economic or identity guard. Evidence URI differences are not cryptographic tampering (only its digest is signed); the observation must still refer to the exact submitted record to prevent accidental reuse across commands.

Term validation checks, in order: policyVersion/family and validator selection; native asset; budget; positive reduced alpha; deadline chain/gap; content refs and delegation relationships; validator role conflicts; retry; exact fresh value; ID availability. Unsupported version/family or nondeployment validator is UNSUPPORTED_POLICY; validator equal to requester/refund or child parent signer/payout is VALIDATOR_CONFLICT. Invalid content refs/relationships are INVALID_TERMS unless a more specific guard below applies. Full width errors have already been rejected at the boundary.

All times are explicit uint64 seconds. Enforce `now < biddingClose < allocationBy < acceptBy < resultBy < validationBy`, with acceptBy−allocationBy>=120 at creation. Existing task createdAt never changes. Artifact/policy availability and evaluator correctness are not checked here; missing validation follows the already-defined timeout path.

### 7.3 Settlement primitive and conservation

Use one `settleTask` composition internally for all eight terminal reasons. It creates the canonical receipt, zeroes escrow, adds address credits, updates reputation, updates the immediate parent's accounting and emits one TaskSettled atomically. It never transfers assets to a recipient, publishes ERC-8004 feedback or settles descendants recursively.

| Terminal reason | paidAtoms | refundAtoms | counterEffect |
|---|---:|---:|---|
| SUCCESS | reservedAtoms | budgetAtoms−reservedAtoms | SUCCESS |
| VALIDATION_FAILED, NO_SHOW, EXECUTION_TIMEOUT | 0 | budgetAtoms | FAILURE |
| VALIDATOR_TIMEOUT, UNALLOCATED, ALLOCATION_EXPIRED, CANCELLED | 0 | budgetAtoms | NONE |

Awarded failures preserve allocation reserve and frozen winner/payout in the receipt even though paidAtoms=0. Unawarded receipts have null winner/payout and reserve 0. Preserve a submitted result digest on validator timeout but set validation=null. Only PASS/FAIL settlement includes a ValidationRecord. Identical payout and refund addresses receive the **sum** of both deltas; never overwrite one credit with the other. Successful zero-price work still produces SUCCESS evidence.

For every reachable state:

```text
depositedAtoms = sum(task.escrowAtoms) + sum(credits.values()) + withdrawnAtoms
depositedAtoms = sum(task.escrowAtoms) + sum(receipt.paidAtoms + receipt.refundAtoms)
sum(credits.values()) + withdrawnAtoms = sum(receipt.paidAtoms + receipt.refundAtoms)
```

Deposits/withdrawals and receipts are cumulative; withdrawing never edits a receipt. All terms are nonnegative uint256 aggregates. The simulated ledger excludes unsolicited donations; L3's actual native balance may exceed these liabilities and must never be below them. No fee, automatic credit funding, cash advance, sweep or clawback is introduced.

### 7.4 Child envelope and retries

`availableChildBudget(parent)` is `price-ownWorkReserveAtoms-reservedChildBudgets-committedChildPayouts`. Reject a negative result as invalid internal state. Reserve a child's **budget**, not its price. Creating a child requires budget<=available capacity and `valueAtoms==budget`; credits/parent escrow are not implicitly spendable.

Ordered child-specific checks after valid terms: parent allows next depth and child absolute maxDepth is within `[child.depth,parent.maxDepth]` (DEPTH_LIMIT); child maxChildren<=parent.maxChildren and lifetime slot remains (CHILD_LIMIT); envelope capacity (ENVELOPE_EXCEEDED); child.validationBy+120<=parent.resultBy in widened arithmetic (CHILD_DEADLINE); retry validity (INVALID_RETRY); exact value (WRONG_VALUE); namespace capacity (TASK_LIMIT). At its maximum depth a task must have maxChildren=0. Global limits are depth 2 and 4 direct lifetime children; maxChildren=0 prevents child creation. Preserve more-specific errors for valid-width values that violate parent permissions.

On child creation: reservedChildBudgets+=budget, activeChildren+=1, childrenCreated+=1. On its first settlement: reservedChildBudgets−=budget, committedChildPayouts+=paidAtoms, activeChildren−=1. No lifetime count is released. Child price below budget releases only the unused portion after settlement, not at allocation. Capacity remains consumed by successful child payouts. Parent failure refunds its own budget independently; already-earned child credits remain.

Child submission is an ordinary task submission; it can itself have children within its permissions. Parent result submission waits for all immediate children to be terminal. Parent timeout does not require this and does not cancel them. A later expireTask on an overdue child still updates the terminal parent's counters exactly once.

Retry creates a new task and ID; it never reopens a receipt. Root retry requires same requester and failed/cancelled/unallocated/expired terminal root, but starts a new root. Child retry requires same requester, root and parent, non-success terminal predecessor, still-running parent, new lifetime slot, envelope and fresh funding. Terms may differ but independently validate. Reject success, unsettled predecessor, foreign predecessor, cross-parent/root or different requester with INVALID_RETRY.

## 8. Analytics — L1-04

Analytics is read-only and has no market authority. Use actual receipt payments and observed expenses, never bids/reserves/estimates as realized cost or cash received. All arithmetic uses integers or standard-library `fractions.Fraction`; signed analytics values are not serialized as the public schema's unsigned Rational.

### 8.1 Input and availability contract

`summarizeCosts(attributedReports, expectedReports, unit)` consumes canonical CostReport objects plus explicit attribution. Each attributed report has `operator:address` (the economic cost bearer supplied by the reporting boundary) and `report:CostReport`. Operator is not silently inferred from AgentRef ownership, execution signer or payout. `expectedReports` is a set of `(operator, ExecutionRef)` pairs declaring the complete scope being analyzed. This is an internal analytics input, not a new field in CostReport or a claim that telemetry is independently verified.

For hashable Python scope keys, pass `(operator, executionKey(executionRef))`; executionKey returns `(chainId:int, market:bytes, taskId:int, awardId:int)`. CostSummary retains the scope as a frozenset. Public ExecutionRef encoding is unchanged.

Require exactly one selected report per expected pair; identical repeat of the same `(operator,reportId)` is ignored. Different reports for the same expected pair are ambiguous corrections and return CONFLICTING_REPORT, not latest-wins. Deduplicate expenses globally by `(operator,expenseId)`. Identical CostItem values under that key count once; differing values return CONFLICTING_EXPENSE. Extra/unattributed reports outside expectedReports are INVALID_SCOPE. The caller must resolve corrections/scope before this function; no ingestion policy is invented here.

Return a `CostSummary` with `unit`, `expectedReports` (the immutable input scope), `executionAtoms`, `overheadAtoms`, `operatingAtoms`, `byOperator`, `unavailableReasons:tuple`. Numeric totals are nullable. Each byOperator entry has those same three nullable totals and reasons. All declared operators appear, even with zero expenses. The overall resource totals count each deduplicated item once. Per-operator totals include only that operator's expenses. A conflicting report/expense invalidates the affected operator's totals and the overall totals; an invalid scope invalidates all totals. Reasons are local analytics values, not ProtocolError codes.

For a complete scope: EXECUTION contributes to executionAtoms; FORECAST/VALIDATION/GAS contribute to overheadAtoms; operatingAtoms=executionAtoms+overheadAtoms. CHILD_PAYMENT contributes to neither: actual child payments come from receipts. Empty items with a COMPLETE report are known zero. Missing reports, INCOMPLETE/CENSORED reports, null required quantity/cost or incompatible unit make that operator's three totals null; overall totals are null if any operator is unavailable. Preserve all applicable reasons in this fixed order: INVALID_SCOPE, CONFLICTING_REPORT, CONFLICTING_EXPENSE, MISSING_REPORT, INCOMPLETE_REPORT, UNKNOWN_COST, UNIT_MISMATCH. No partial total is presented as a complete total.

Money units must match both currency and decimals exactly. No implicit rescaling, foreign exchange or inference from provider quantity. Malformed records reject at the domain boundary. Reports are claims: PROVIDER_ATTESTED versus OPERATOR_REPORTED provenance is retained but does not change arithmetic or confer protocol authority. Forecast estimates are excluded entirely.

### 8.2 Metrics

`calculateTaskMetrics(task, descendants, operator, costSummary)` requires a terminal task, its complete descendant set (including retries), terminal descendant receipts, and matching analytics scope for that task/operator. Validate uniqueness, parent/root links, and each node's lifetime child count against the supplied records; derive immediate children from parentRef. Missing records are INVALID_SCOPE; present nonterminal descendants give CHILDREN_UNSETTLED, and a nonterminal analyzed task gives TASK_UNSETTLED. `operator` attribution is explicit; no ownership transfer changes it retroactively. Every expectedReports execution must belong to an awarded node of this tree; require at least one declared report per awarded node and at least one for the analyzed operator. The caller declares which additional cost bearers belong in the scope: the core cannot independently prove telemetry completeness.

CostReport requires ExecutionRef. Pre-award or never-awarded expenses without a valid ExecutionRef cannot be fabricated into these reports. A request to include such expenses in whole-tree analytics yields INVALID_SCOPE until a later-layer ingestion design can represent them explicitly; do not label the resulting partial accounting as the full cost. Forecast/validation/gas expenses with valid execution attribution remain included even when work failed. This is a public L0 telemetry representation limit, not permission to treat unknown overhead as zero.

Return `actualPayoutAtoms`, `childPaymentsAtoms`, `cashProfitAtoms`, `userSpendingAtoms`, `executionUtility:Fraction|null`, `unavailableReasons`. Monetary fields are `int|null`. Facts from available receipts can remain known when expense-dependent metrics are null. Child payments are the sum of direct child paidAtoms; never grandchild payments again. Task userSpendingAtoms is budget−refundAtoms, equal to paidAtoms; it excludes gas and is not system resource cost. Merge cost-summary reasons in their declared order, followed by TASK_UNSETTLED, CHILDREN_UNSETTLED, UNKNOWN_CORRECTNESS, omitting duplicates. Unknown correctness alone makes only utility unavailable, not known cash-flow facts or complete-cost profit.

```text
ownCost = operator's EXECUTION expenses in the declared task scope
overhead = operator's FORECAST + VALIDATION + GAS expenses in that scope
cashProfitAtoms = actualPayoutAtoms - childPaymentsAtoms - ownCost - overhead
executionUtility = correctness - (alphaNum / alphaDen) * executionAtoms
```

Cash profit is available only in the settlement asset unit (MON,18) with complete matching expenses and all children settled. For a solo worker childPaymentsAtoms=0. For a failed manager actualPayoutAtoms=0 but successful child payments still subtract. Refunds of the manager's unused child budgets are not profit; subtracting net child payments already accounts for their return. Receiving a protocol credit counts as an earned payment; withdrawing it changes liquidity, not profit.

For utility, correctness is 1 for a validated PASS and 0 for a validated FAIL. It is unknown for no-show, execution timeout, validator timeout and unawarded/cancelled outcomes: reputation penalties are not evidence of a completed output's correctness. The execution expense scope includes all unique underlying EXECUTION expenses for the requested task subtree, including failed attempts, but excludes all transfer and overhead categories. Require the settlement asset's atomic unit so alpha is dimensionally valid. If correctness, scope or cost is unknown, utility is null. Report total operating cost separately so overhead is never hidden.

`calculateTaskMetrics` therefore receives a CostSummary for the complete task subtree: `byOperator[operator]` supplies that operator's costs across the declared subtree, and the overall executionAtoms supplies the utility's resource cost. An operator performing multiple nodes must use a scope containing only the expenses economically borne for this analyzed obligation. Do not sum per-node manager profits as system cost. External funder/validator expenses may appear as separate operators and count in system totals, not worker profit.

### 8.3 Exact additional examples

Record these as reviewed data in `specs/fixtures/layer-1.json` during implementation, with initial/input, action, expected values/events or rejection. Integers below are atoms; use MON,18 for metric examples. These are required expectations, not generated by the tested functions.

| ID | Input/action | Exact expected result |
|---|---|---|
| analytics-solo-success | Paid 50, own execution 20, gas 2, forecast 1, validation 3, no children; PASS; alpha=1/100 | execution=20, overhead=6, operating=26, cashProfit=24, userSpending=50, utility=4/5 |
| analytics-solo-fail | Same expenses, FAIL and paid 0 | cashProfit=−26, userSpending=0, utility=−1/5 |
| analytics-manager | Parent paid 100; child paid 30; manager execution 10 and gas 5; child operator execution 18 and gas 2; PASS, alpha=1/100 | Parent profit=55; system execution=28, overhead=7, operating=35; root userSpending=100; utility=18/25; child transfer 30 excluded from resource totals |
| analytics-parent-fails | Same expenses/child payment; parent FAIL and paid 0 | Parent profit=−45; system operating=35; root userSpending=0; utility=−7/25 |
| analytics-duplicate-expense | Two selected reports share identical expenseId E/cost 7 for same operator; no other items | Deduplicated execution=7; changing the second E to cost 8 yields CONFLICTING_EXPENSE and null totals |
| analytics-distinct-operators | Same expenseId E/cost 7 under two different operators | execution=14 |
| analytics-missing | Required report absent, or present incomplete/censored/null cost | null totals/profit/utility with corresponding reasons; no zero substitution |
| analytics-units | MON,18 target with USD,6 or MON,6 item | UNIT_MISMATCH; no automatic conversion |
| analytics-unknown-verdict | VALIDATOR_TIMEOUT, own execution 20 and overhead 6 | payout=0, cashProfit=−26, utility=null |
| alias-credit | SUCCESS budget 100, price 50, payout=refund address | One address credit increases by 100; escrow becomes 0 |
| checkpoint-at-ceiling | taskCount=uint64 max, prior successes=max−1, failures=0, one unseen SUCCESS receipt | successes=max without overflow; next creation rejects TASK_LIMIT |
| expired-permit-rollback | Valid otherwise, now=permit.expiry before biddingClose | SIGNATURE_EXPIRED; no bid/nonce/event |
| nonce-type-separation | Pure nonce-key helper: same market/signer/nonce with different primaryType | Distinct keys; this is not a valid live dual-role scenario because the pinned validator cannot bid |
| wrong-observation-binding | Valid verifier observation for a different record/domain/primaryType or signer | INVALID_SIGNATURE; no state/event effects |
| terminal-parent-late-child | Parent already EXECUTION_TIMEOUT; child still RUNNING and overdue, budget 30 reserved in parent | Child expireTask pays 0/refunds 30, parent reservedChildren−30 and activeChildren−1, parent receipt/credits unchanged |

## 9. Fixture consumption and known projection differences

Fixture loaders read checked-in expected values; implementation functions must never write/recompute expected results. The added L1 fixture file follows the existing scenario grammar with `schemaVersion=1`, `synthetic=true`, `domain="layer-1"` and explicit cases; extend the test-only domain enum to admit it. Use `initial`, `action`, `expected`, `events`, `error` exactly as in that grammar. Analytics unavailable reasons belong inside expected with error=null; do not add them to the protocol error catalog. Encode expected signed metrics as canonical decimal strings and utility as `{numerator,denominator}` decimal strings with positive denominator. Extend the checker to validate this file and its unique IDs; do not change the L0 acceptance map into a claim that L1 has run.

| Existing source | L1 consumption |
|---|---|
| market.json | All 18 cases directly through evaluateAuction; compare full expected object, no chain events |
| lifecycle.json | All 34 cases through applyCommand after explicit projection expansion; compare all projected expected fields, stable error and event names; separately schema-check full event payloads |
| reputation.json | Probability/observe/snapshot cases through pure reducer APIs; transfer case changes external owner only; creation-namespace-exhausted through creation guard |
| delegation.json | All 17 projected cases through envelope/child guards or composed transitions as applicable; preserve expected balances/counts/errors |
| replay.json | L1 admission checks: transferred-new-bid-unset-wallet, validator-role-conflict, approved-operator-cannot-bid, restored-wallet-new-owner-bid; transfer-after-bid verifies frozen fields survive changed observation |
| objects.json | Reuse canonical sample records; round-trip and schema-check all examples through the domain boundary, without claiming their synthetic addresses/content are live |
| signatures.json | Existing L0 verification remains; L1 additionally tests owner/nonce/expiry/record binding using explicit verifier observations, not reimplementing cryptography |
| Remaining replay, a2a and validation fixtures | Continue existing L0 structure checks; behavioral execution belongs to L2/L3/L4/L7, not this layer |

Projection expansion rules are test infrastructure, not runtime branches:

* Start from the canonical TaskSpec/Bid/Allocation/ResultCommitment/ValidationRecord object examples and distinct actor addresses. Replace only fields specified by fixture initial/action values. Default lifecycle alpha=1/100, bid=20, prior counters=(0,0), p=500000 produces stored score=30000000 and price=50, matching allocate-positive. REQUESTER/OWNER/SIGNER/PAYOUT use their respective example addresses; ANY/OTHER use distinct synthetic nonzero addresses, not the validator.
* Use creation block 10/snapshot 9 for lifecycle examples; subsequent actions use a block >=10. Supplied `now` is action block time. Unspecified signatures represent valid bound observations with expiry strictly later than action time; no cryptographic work runs in these tests.
* `p`, `hasWinner`, `hasResult`, `managerFunds` and symbolic actor fields are projection metadata, not extra command fields. Canonical Bid derives p from counters; arithmetic vectors may supply unattainable synthetic p directly. External manager funds are test harness accounting: only supplied value enters core deposits. Success/failure does not invent a balance query.
* Expand active-child/lifetime-count projections with explicit linked child records or call the dedicated envelope guard being tested. Historical credits/checkpoints omitted by the projection are supplied explicitly by the test builder; never synthesize them from expected output. Full-state tests must satisfy conservation. Narrow guard tests need only the guard's declared input.
* `child-permissions-cannot-expand` passes childMaxDepth=3 and expects DEPTH_LIMIT. This tests the semantic depth guard directly: the public schema rejects that value before command execution. Keep that L0 vector and separately test INVALID_RANGE at the public decoding boundary. Do not bypass the schema or alter the expected L0 code to make one test path fit both contracts.
* Delegation `settleChild`, `expireParent` and `retryChild` are scenario operation labels, not public commands. Map them to the existing settlement/expiration/createChildTask paths. The fixture supplies child budget/paid/refund projections; positive settlement requires an explicit child allocation/result/verdict in the expanded case, never a public “set payout” action.
* Compare all supplied projection fields and prove input unchanged on rejection. New full-command tests must also compare exact complete payloads against independently written expected records, so checking event names alone cannot hide missing fields.

## 10. Minimal test suite with complete behavioral coverage — L1-05

Minimize duplicated setup and redundant tests, not required assertions. Use parameterized test families rather than one function per fixture or property getter. Do not impose an arbitrary count that forces unrelated behaviors into a single unreadable test. Start with the following ten families; add a case when coverage or a discovered bug exposes a genuinely missing path.

| Family | Required cases/assertions | Requirements |
|---|---|---|
| T1 domain | All object examples; missing/extra/null fields, duplicate keys, BOM/UTF-8, bool/float/NaN, decimal grammar, numeric min/max/one-over, lowercase refs, URI limits, unsupported version, round-trip; no schema/file/network lookup inside core | shared boundary |
| T2 auction goldens | All 18 existing cases; no bids, nonpositive, tie/identity bytes, one bid, cap, floor, zero price, maxima and invalid input | L1-01 |
| T3 reputation goldens | All 15 existing projections; missing history, same/prior block, NONE receipts, repeated/conflicting receipt, range/total bounds, no ownership-key reset | L1-02 |
| T4 lifecycle goldens | All 34 cases; eight terminal paths, all ten command paths, exact money/receipt/event/evidence effects, blocked and successful withdrawal | L1-03 |
| T5 admission/binding matrix | Direct/permit paths; all role conflicts; nonexistent/foreign identity; owner/wallet errors; approved operator; mismatched permit fields/observation; valid/invalid signature; consumed and out-of-order nonces; expiry equality; transfer before/after/back; reentrancy/wrong value | L1-02/03 |
| T6 transition boundary matrix | Every task command in every inappropriate state, terminal precedence, unknown/foreign refs, wrong award/actor, cutoff−1/cutoff/cutoff+1, term/budget/alpha/policy errors, task-count max, zero/insufficient withdrawal, alias credits and zero-price SUCCESS | L1-03 |
| T7 delegation goldens | All 17 existing projections; ancestor permissions, fresh funds, exact envelope/slack, reserve/active/lifetime counts, root/child retries, parent failure, late child, duplicate release and active-child submission block | L1-03 |
| T8 analytics | All §8.3 analytics examples; empty COMPLETE report, repeated/conflicting report, scope gaps, null quantities/costs, units, duplicate expenses, zero/negative profit, operator attribution, direct versus grandchild transfers | L1-04 |
| T9 bounded properties | The six invariant groups below, including rollback/input immutability and serializable outputs | L1-01–05 |
| T10 composed scenario | The complete sequence below using real L1 functions, with adapters represented only by explicit facts | L1-01–05 |

T5/T6 are tables with one isolated failure per row plus a few competing-failure rows to verify guard precedence. Include every catalog error reachable at this layer; distinguish INVALID_ENCODING boundary tests and TRANSFER_FAILED/REENTRANCY simulations from an actual EVM integration test. No blanket “invalid input” assertion: assert the exact code and no effects. Intentionally test a terminal task plus wrong actor/expired signature to establish ALREADY_SETTLED precedence, and a wrong-value task call plus wrong state to establish WRONG_VALUE precedence.

Property groups use Hypothesis with deterministic CI settings (`derandomize=True`, `max_examples=100` per group, bounded lists/sequences; no live providers). Small input universes plus explicit maximum-width examples make failures diagnosable. Do not suppress health checks or filter almost all generated inputs.

1. **Market ordering:** valid bid permutations preserve allocation; incremental topTwo agrees with a test-only brute-force sorted list; increasing a bid never increases its score. The reference sort must not call production comparison/top-two helpers.
2. **Payment:** every award obeys bid<=price<=budget and strict positive winning score. With uncapped critical c, independently assert `c*d <= numerator < (c+1)*d`; include cap/tie/zero cases. This tests rounding without calling the production pricing function to calculate expected values.
3. **Reputation:** success cannot lower p; failure cannot raise p; p remains in [0,Q]; duplicate receipt changes nothing; future checkpoint/ownership changes cannot change an earlier snapshot. Same-block reduction preserves all observations and one cumulative checkpoint.
4. **Ledger sequences:** bounded valid/invalid command sequences preserve all §7.3 equalities, nonnegative balances, frozen records, at-most-once receipt/counter/parent effects and no input mutation. Expected deltas come from terminal outcome tables, not calling settleTask twice as its own oracle.
5. **Delegation:** after each create/settle/retry action, reservedChildBudgets equals outstanding direct child budgets, committedChildPayouts equals settled direct child payments, active count matches outstanding children, lifetime count never decreases and envelope never exceeds price. Terminal parent receipts remain fixed.
6. **Analytics:** input order and identical retransmission do not affect deduplicated totals; adding a CHILD_PAYMENT cannot inflate resource cost; splitting distinct expenses preserves sums; unknown data cannot yield a known zero metric.

The composed scenario starts with an empty state, creates a root, admits two bids from historical snapshots, allocates/accepts, funds a child from the frozen manager signer, executes/settles the child successfully, submits/settles the parent, withdraws both earned/refund credits and calculates matching analytics. Assert exact canonical events/receipts, state, counter snapshots and conservation at each step. Use one parameterized alternate ending where the parent times out after paying the child; child credit survives and manager profit includes that loss. Add identity transfer between admission and acceptance in this sequence to prove the original signer/payout obligation survives. The transfer is an external observation change, not an invented protocol command.

Use these exact composed inputs/expectations; store the two endings as `tree-success` and `tree-parent-timeout` cases in the new L1 fixture file:

| Step | Input | Expected effect |
|---|---|---|
| Root creation | ID 1, createdAt=1000/block=10, budget=100, alpha=1/100, deadlines 1200/1300/1500/2500/3000, maxDepth=2/maxChildren=4 | Deposit/escrow=100, snapshotBlock=9 |
| Two direct bids | Both agents fresh; IDs 7 and 8 bid 20 and 30 before 1200, distinct permitted owners/signers/payouts | p=500000 each; scores=30000000/20000000; no nonce consumption |
| Root award/accept | Allocate at 1200; identity 7 transfers externally; original frozen signer accepts at 1400, own reserve=5 | Winner 7, critical/price=30, RUNNING; new owner cannot replace signer/payout |
| Child creation | ID 2 at 1410, budget=20, alpha=1/20, deadlines 1500/1600/1720/2000/2200, maxDepth=2/maxChildren=0; parent signer supplies 20, child refundAddress=that signer | Cumulative deposits=120, parent reserved children=20, active/lifetime=1/1 |
| Child market | Fresh agent 9 bids 2 at 1420; allocate 1500; accept 1600 with own reserve=0 | score=8000000, price=10; child RUNNING |
| Child success | Submit 1900, PASS at 2000, fresh bound verdict | Child paid/refund=10/10; parent reserved children=0, committed payouts=10, active/lifetime=0/1; agent 9 successes=1 |
| Success ending | Root submit 2400, PASS at 2600 | Root paid/refund=30/70; agent 7 successes=1; total credits=120, total escrow=0 |
| Timeout ending | Skip root result; expire at 2500 | Root paid/refund=0/100; agent 7 failures=1; child receipt unchanged; total credits=120, total escrow=0 |
| Withdrawals | Each credited owner withdraws its full credit to a successful receiver | Credits=0, withdrawnAtoms=120, receipts unchanged; root loser 8 has no observation |
| Analytics | Manager execution=4/gas=1, child operator execution=3/gas=1; complete MON,18 reports with known quantities | System execution=7, overhead=2, operating=9; success manager profit=15/root spending=30/utility=93/100; timeout manager profit=−15/root spending=0/utility=null |

Use increasing block numbers after creation (timestamps above are seconds, not block numbers). Child creation pins its own prior-block snapshot. All verdict expiries exceed their submission times and are still subject to task deadlines. Addresses and digests come from deterministic synthetic builders; they must not be presented as deployment facts.

## 11. Verification and exit gate

During L1 implementation configure pytest to discover the colocated module tests and `tests/`, and coverage.py with branch measurement over `modules/domain` and `modules/market_core`. Exclude test files from measurement, not production failure paths. Require 100% statements **and** zero missing branches in the coverage JSON report; a rounded headline percentage is insufficient. Do not use `pragma: no cover`, broad omit patterns, skipped tests or dead fallback code to reach the target. Tooling/scripts retain their existing tests and are not counted as economic-core coverage.

After adding the dev dependencies/configuration, run from repository root:

```sh
uv sync --locked
pnpm install --frozen-lockfile
uv run --locked python scripts/check_specs.py
uv run --locked python -m unittest discover -s scripts -p 'test_*.py'
uv run --locked coverage run --branch --source=modules/domain,modules/market_core -m pytest modules tests
uv run --locked coverage report --fail-under=100
uv run --locked coverage json -o .scratch/l1-coverage.json
uv run --locked ruff check .
uv run --locked ruff format --check .
```

Create the ignored `.scratch` directory for reports when needed. Initial dependency installation may need network; all tests run offline after dependencies are installed. Add the JSON missing-statements/missing-branches assertion to the normal test/check command during implementation so the gate is mechanical. Do not add a type-checker/build system solely for this layer; schema validation, type annotations, Ruff and behavior tests are the selected checks.

L1 is complete only when:

* All five requirement rows and T1–T10 have passing executed evidence; every applicable checked-in expected result was consumed unchanged, with any fixture defect explicitly reviewed rather than regenerated.
* All ten command effects, eight terminal outcomes, ownership changes, nonce/retry/receipt duplicates and independent child accounting can be exercised without external services or hidden context.
* Outputs conform to existing schemas; returned event payloads contain sufficient canonical fields without fabricated chain provenance.
* Pure core imports have no clock, filesystem, network, crypto/provider, framework or persistence side effects. Integration code cannot supply p/price/payment as command authority.
* Arithmetic, conservation, snapshot, delegation and analytics properties pass; coverage reports zero missing production statements/branches and the input/error requirement matrix has no uncovered row.
* Existing L0 schema/ABI/signature checks and checker regressions still pass; lint and formatting pass; no later-layer implementation was added.

Deployment-time addresses, registry implementation verification, validator key verification and finalized-RPC support remain in the existing manifest/deployment checklist. None blocks offline L1 implementation. This specification selects no live address and claims no deployed verification.

## 12. Executed acceptance review

Verified with `uv run --locked python scripts/check_layer1.py` on 2026-10-05. The single command runs all existing L0 checks, the new L1 suite, an exact coverage gate and Ruff. No expected L0 market/lifecycle/reputation/delegation/signature values were changed.

| Requirement | Executed evidence | Result |
|---|---|---|
| L1-01 | All 18 market goldens; ordering/top-two/monotonicity/payment properties; canonical allocations in complete command flows | Pass |
| L1-02 | All 15 reputation projections; snapshot, same-block, duplicate/conflicting receipt, namespace/money integrity, transfer and counter-ceiling cases | Pass |
| L1-03 | All 34 lifecycle and 17 delegation cases; applicable replay authority fixtures; state/actor/binding/term matrices; ledger/delegation properties; alias credits, zero-price success and final task ID | Pass |
| L1-04 | Reviewed cost/profit/utility fixtures; report/expense deduplication, missing/censored/unknown inputs, operator scope and incompatible units; cost invariants | Pass |
| L1-05 | Both reviewed tree endings through real domain/core functions, frozen ownership obligations, child payment independence, complete withdrawals, counters and analytics; exact coverage gate | Pass |

Results: **270 pytest cases**, **16 checker regression tests**, **700/700 production statements** and **212/212 branches**. Coverage contains no excluded production lines. The checker also validates 37 canonical object examples, 115 scenario cases and 18 market vectors; ethers independently verifies the two signing vectors, nine cryptographic mutations and three content hashes. The existing Python verifier retains nonce/expiry/owner authorization negatives. Ruff lint and format checks pass.

Review findings resolved during implementation:

* Domain validation/JSON/URI helpers are shared with the existing checker; signing and filesystem code remain outside domain/core.
* Arithmetic no-award projection differs intentionally from the full canonical Allocation, as documented in §5; no protocol fields were changed.
* Reputation reduction rejects contradictory receipt money, unawarded execution fields and cross-market receipt mixing rather than accepting schema-valid but inconsistent observations.
* CorePolicy rejects unsupported v1 economic constants. Synthetic test identities remain fixtures; none were installed as deployment facts.
* Coverage gating checks actual missing statements/branches and the complete source-file set, so rounding or an omitted module cannot produce a false pass.

Boundary limits remain intentional: signature/registry/transfer results are supplied observations; reentrancy and blocked receivers are reference simulations, not EVM tests. Canonical history acquisition, finality/reorg handling, portable feedback export, A2A and evaluator execution remain in their designated later layers. Monetary analytics cannot invent conversion rates or unrepresented pre-award expenses. L1 satisfies its offline exit gate; deployment readiness is not claimed.
