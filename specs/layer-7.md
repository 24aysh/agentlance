# Layer 7 — Objective validation, reconciliation and portable evidence

**Status:** implemented and offline acceptance cross-checked on 2026-10-08 (UTC), based on merged Layer 6 `f825362`; executed evidence is recorded in §13. Live activation remains pending the explicit gates in §12. Requirements **L7-01–L7-07** in [the layer plan](../layers.md) govern the scope. [Protocol semantics](protocol.md), [deployment/evaluator policy](deployment-policy.md), [canonical schemas](schemas/protocol.schema.json), [signatures](signing/README.md) and [Layer 6](layer-6.md) remain authoritative.

## 1. Purpose and scope

Complete the path from a submitted artifact to an objective verdict, canonical settlement, portable outcome feedback and private cost reconciliation. Implement the existing `structured-output-v1` evaluator, including both reference templates used by L6. No model judgment is needed.

| L7 owns | Existing or later owner |
|---|---|
| Fetching committed artifacts, isolated evaluation, reproducible evidence and validator signatures | L6 execution, capacity, child planning, fallback, result submission and metering |
| Relaying verdicts and observing timeout/settlement | L3 authorization, deadline rules, credits, refunds and atomic reputation updates |
| Receipt-keyed ERC-8004 publication and export status | L4 finalized observation, native transaction recovery and replaceable discovery |
| Joining canonical receipts with selected private usage and saved forecasts | L5 prices, estimates, bid decisions, report ingestion/corrections and forecast evaluation |
| Readable validation/export/reconciliation diagnostics | L8 task creation, explorer and user interface |

The MVP adds one validator/publisher application, one small immutable feedback-publisher contract, and an agent-local reconciliation component. These are responsibility boundaries, not separate services for every job. The publisher contract is already required by `protocol.md`; a SQLite flag alone cannot guarantee at-most-once external feedback.

Exclude new market contracts or settlement rules, subjective arbitration, validator consensus/rotation, automatic root allocation, automatic withdrawals, provider invoice verification, a general accounting platform, paid reflection, additional task families and the ERC-8004 Validation Registry. L7 must not re-run worker execution or refill its limits. Arbitrary third-party feedback never changes AgentLance allocation counters.

## 2. Inspected baseline and reuse

- [`AgentLanceMarket`](../contracts/src/AgentLanceMarket.sol) already verifies `ValidationRecord`, settles PASS/FAIL, expires tasks and writes receipts/counters atomically. `settleVerdict` is present in the frozen ABI but has no production `MonadMarket` wrapper yet. L6 demos currently construct fixture verdicts in [`tests/layer6_market_support.py`](../tests/layer6_market_support.py).
- [`parseJson`, `checkJson`, `RecordValidator`](../modules/domain/records.py), canonical `OutputShape`/`ValidationPolicy`/`EvaluationEvidence`, and [validation vectors](fixtures/validation.json) are reusable. There is no complete predicate evaluator. L6's `checkOutput` checks shape/limits only; it is not verdict authority.
- [`DockerExecutor`](../modules/adapters/execution/docker.py) already bounds subprocess output and isolates containers. Reuse its process/container controls with a separate reviewed evaluator image. Extract only the shared container boundary needed by this second caller; do not give the validator an L6 execution identity, capacity reservation or cost envelope.
- [`ContentStore`](../modules/adapters/storage/content.py) provides safe HTTPS/IPFS retrieval, exact-byte hashing and local content retention. It has no IPFS upload adapter. Its byte-limit exception does not prove that an oversized result matches the committed digest.
- [`MarketWatcher`](../modules/agent_client/discovery.py), [`ChainConnection`](../modules/adapters/chain/rpc.py) and [`MonadMarket`](../modules/adapters/chain/market.py) supply finalized logs, qualified reads and persisted signed-transaction recovery. Reuse them; add independent L7 consumer checkpoints instead of stealing agent delivery state.
- [`Journal`](../modules/adapters/storage/journal.py) v4 already enforces one database writer, one bound transaction sender and persistent finality halts. Add a migration for private L7 jobs/projections without changing existing claims, outboxes or native operations.
- [`ExecutionHistory`](../modules/economics/history.py) already implements `recordUsage`, `attachSettlement` and `evaluateForecasts`. [`EconomicsStore.saveReport`](../modules/adapters/storage/economics.py) selects immutable report versions and rejects changed content under an existing expense ID. L6 already delivers OWN_EXECUTION reports; consume that selection, not a duplicate ingestion path.
- [`reduceReceipt`](../modules/market_core/reputation.py) and [`summarizeCosts` / `calculateTaskMetrics`](../modules/market_core/analytics.py) already define evidence and financial semantics. Reuse them only with their complete required inputs; do not manufacture complete costs from L6's narrower report scope.
- The identity adapter pins ERC-8004 source revision `b9e466c250744a7e06b13dff9d3c2844ed64f825`. Use the matching reputation implementation and its actual ABI/authorization behavior, not an unpinned current draft or an invented reputation API.

## 3. Inputs, outputs and interfaces

Consume finalized `ResultSubmitted` / `TaskSettled` observations and fresh full task views; exact input/schema/policy/result bytes; a qualified deployment; operator-owned validator/relayer credentials; finite runtime/storage/gas limits; and, in the agent process only, selected L5 reports and L6 usage context.

Produce canonical `EvaluationEvidence` bytes and a retrievable `ContentRef`; the existing signed `ValidationRecord`; existing market operation results and `SettlementReceipt`; a publisher feedback index; and local versioned reconciliation/status records. No private cost record is required for a verdict, payment or feedback.

| Interface to implement or extend | Contract |
|---|---|
| `evaluateArtifact(task, result, inputBytes, shapeBytes, policyBytes, resultBytes)` | Pure evaluation → exact `EvaluationEvidence`, or an operational/unsupported reason with **no verdict**; no network or keys |
| `publishEvidence(raw, publicationId)` | Persisted immutable evidence → verified IPFS `ContentRef`, or retained pending publication; never a changed payload under the same ID |
| `buildValidationVerdictTypedData(record, domain, types)` | Existing v1 flattened EIP-712 fields; reuse `eth-account`, `verifyEoa`, catalog enums and signing fixtures |
| `MonadMarket.settleVerdict(command, operationId)` | Existing zero-value command → durable native operation; cutoff is `min(validationBy, record.expiry)` |
| `ValidatorRuntime.tick()` | Finalized submissions and saved jobs → bounded evaluation, publication, signing, relaying or eligible expiry; return without waiting for a long-running job |
| `FeedbackPublisher.publish(taskRef)` / `readPublication(taskRef)` | Canonical receipt → at most one registry feedback entry; publicly readable publication state (§6) |
| `Reconciler.tick()` / `readReconciliation(executionRef)` | Agent's selected usage plus finalized receipt → local receipt attachment and versioned comparison; no worker execution/signing |
| `readOutcome(taskRef)` | Public canonical task/receipt/evidence references plus separate validation/export status and observation stamp; no private reports |

The public reader can be a library/diagnostic CLI over the bounded projection; a new HTTP API is not required. L8 must be able to display canonical settlement alongside `feedback=PENDING`, unavailable evidence or missing private costs. Include `observedAt`/finalized provenance; local status is not a new protocol task state.

Run the validator/publisher with its own role-bound journal and explicitly funded transaction sender. Keep its validator key outside all worker/evaluator containers. A single configured EOA may sign verdicts and relay transactions; that disclosed operator still cannot be a task's requester/refund/worker role under L3. If different transaction senders are configured, give each its own journal and nonce queue. Never open an agent's private journal concurrently from the validator application. Compose private reconciliation inside the agent's existing process/journal without giving it a validator key.

## 4. Objective evaluation and attestation — L7-01–L7-02

### Admission and execution

1. Persist a job from a verified submission log. Re-read the finalized task: require SUBMITTED, matching `ExecutionRef`, winning `AgentRef`, immutable result, supported family/policy version and configured pinned validator. A notification, caller-supplied policy or an L6 COMPLETED status is insufficient.
2. Fetch exact committed input, schema, policy and result through the existing safe transport. Recheck all digests before treating bytes as evaluable. Missing/malformed input, schema or policy means no verdict. Validate policy/schema binding and the restricted schema's semantic constraints, including required-property completeness and consistent integer bounds.
3. Evaluate in a digest-pinned, prebuilt Docker image: non-root, no network/mounts/credentials, read-only root, dropped capabilities, finite PID/tmpfs/log/stdout limits, **5 seconds CPU, 128 MiB memory**, and a 10-second wall ceiling further bounded by the job deadline. Use exact byte framing, not host paths or model-supplied commands. CPU quota alone is not a cumulative CPU-time limit; enforce the stated CPU budget too.
4. Persist completed evidence and evaluator/image/config provenance before publication or signing. Independently validate the returned evidence schema and every reference/digest against the job. Container exit success alone cannot authorize a signature. A failed binding halts that job visibly.

The evaluator implements the frozen policy, not Python's default loose equality: ordered predicates, strict JSON integers, Unicode scalar strings, RFC 6901 escaping, object-order-independent and array-order-dependent equality, and `true != 1` at every depth. Missing pointers fail their predicate; malformed policy pointers make the policy unsupported/invalid. Empty pointer addresses the whole document; array indices reject leading zeros, `-` and negatives. With verified prerequisites, check result byte length, JSON validity, structural limits, output shape and then predicates, in that order; retain the first failed predicate index. No `$ref`, regular expressions, code, remote schema resolution or semantic-similarity model.

Use the existing 1 MiB input/result/evidence, depth 16, 10,000-node and 64 KiB schema limits. Keep a 64 KiB policy-fetch cap for compatibility with the reference participant; a policy beyond that operator support cap gets no verdict. Check scalar validity as well as JSON parsing. Detect structural limits with bounded traversal/decoding; an unexpected parser/process crash is operational failure, not proof of bad output.

| Evidence established | Action |
|---|---|
| Verified prerequisites and result; shape and all predicates pass | PASS / `PASS` |
| Complete, digest-matching result violates deterministic byte/depth/node limits | FAIL / `RESULT_LIMIT` |
| Complete, digest-matching result has invalid JSON, duplicate keys, non-integer numbers or invalid Unicode | FAIL / `RESULT_INVALID_JSON` |
| Valid bounded result violates output shape | FAIL / `SCHEMA_MISMATCH` |
| Shape matches; predicate fails | FAIL / `PREDICATE_FAILED`, first failing index |
| Unavailable/mismatching bytes, invalid prerequisites, unsupported evaluator, OOM, wall/CPU timeout or crash | No signature; bounded retry or wait for canonical timeout |

Distinguish the **semantic** result limit from a transport ceiling. The MVP permits fetching at most 2 MiB of result bytes, so a complete 1 MiB+1 result can be hash-verified and fail correctly. Crossing the 2 MiB fetch ceiling or terminating before the complete digest is known produces no verdict; a prefix, Content-Length or HTTP error cannot establish FAIL. Retain exact verified bytes used for evaluation, including bounded oversized results. A base64/framed container request may need up to 8 MiB; do not force it through L6's 1 MiB execution-request cap or raise worker limits globally.

### Evidence and signing

`EvaluationEvidence` is the existing closed schema: execution reference, input/result/policy/schema digests, evaluator version 1, verdict, reason and nullable failed-predicate index. Emit deterministic UTF-8 JSON with fixed schema-field order and stable nested-reference order, then hash the exact bytes with the existing Keccak helper. Keep operational logs/image hashes in private job metadata, not extra fields in the canonical evidence.

Publish and read back the evidence before signing. Re-read the task and deadline after publication. Persist one randomly generated uint256 verdict nonce, `expiry=validationBy`, the exact evidence reference and typed-data binding before signing; persist the resulting signature before exposing or relaying it. Restart reuses that record. Never issue a competing verdict, a fresh nonce to bypass an unknown send, or an attestation for a changed result. A lost signature response may be reproduced only for the identical persisted typed message; no second evaluation decision is made.

Expose the saved attestation as a diagnostic/exportable JSON record so another caller can relay it. Use the existing domain, enum mapping and strict EOA checks. The signature binds **evidence digest, not evidence URI**, as already specified: a third-party relayer may supply another valid locator. Reconciliation must compare signed fields and the actual canonical receipt, not reject an otherwise matching settled verdict solely because its locator differs. Never replace that canonical locator locally.

Relay through `MonadMarket` with persisted signed transaction bytes and a stable scope derived from the job ID. Reconcile TaskSettled, the exact signed-field bindings and finalized receipt before marking success. On/after the validation deadline, stop signing/broadcasting a verdict and permit the existing `expireTask` path. A competing verdict/expiry wins only through L3; neither local evaluation nor IPFS availability overrides its receipt.

## 5. Evidence storage and isolation

Add one bounded IPFS publication adapter using an operator-configured **Kubo RPC** node. Upload only persisted evidence bytes as one file; pin deterministic add options (`cid-version=1`, raw leaves, fixed chunking/hash, no directory wrapping or filesystem metadata), retain the CID, and verify retrieval against the AgentLance Keccak digest. A CID is a content locator, not that digest. Repeating an ambiguous upload uses identical bytes/options and must not create a different evidence record. The [Kubo RPC documentation](https://docs.ipfs.tech/reference/kubo/rpc/#api-v0-add) defines this boundary; pin the tested Kubo release/image during implementation.

Kubo RPC is a trusted administration endpoint, configured separately from untrusted artifact URLs. Permit explicit local infrastructure endpoints only through that adapter; do not weaken `NetworkPolicy` or give task data control of upload URLs/authentication. Live evidence retrieval still uses safe public HTTPS gateways. Offline tests may use explicitly isolated local gateway origins. Do not depend on a paid pinning provider or add a second upload integration to complete the MVP.

Retain evaluated input/result/schema/policy bytes and evidence in the validator's bounded content store, with read access to retained public artifacts by digest for reproducibility. Do not serve arbitrary local paths or republish worker prompts, execution journals, private reports, keys or provider request bodies. Reserve retention capacity before admitting a job; storage exhaustion stops new evaluation and preserves existing signatures/transactions. No silent eviction of referenced evidence. IPFS pinning/readback is an availability observation, not a perpetual availability guarantee.

## 6. Receipt-keyed feedback publisher — L7-03–L7-06

Implement a new **immutable** `FeedbackPublisher` contract, leaving the L3 market/ABI unchanged. Its constructor binds the chain, canonical market, identity registry and reputation registry; verify the market's `readPolicy` and the reputation registry's identity binding. No owner-controlled outcomes, upgrade hook, key custody, fund forwarding, revocation function or arbitrary external-call method.

Freeze a separate publisher ABI during implementation:

- `publish(TaskRef) returns (uint64 feedbackIndex)` is permissionless, nonpayable and reentrancy-guarded.
- `readPublication(TaskRef) returns (bool published, uint64 feedbackIndex)` distinguishes absence from a published index.
- `ReceiptPublished(TaskRef,uint64 feedbackIndex)` is emitted exactly once for a newly exported receipt. Namespace/binding getters expose the immutable configuration.

Reject foreign chain/market references and nonexistent/unsettled tasks. Read TaskSpec and SettlementReceipt directly from the bound market, including the frozen winner/family; no caller-supplied verdict, target agent, value, tag or evidence URL. An already-published key returns its saved index without another registry call/event. A receipt with `counterEffect=NONE` returns zero with no feedback/event and remains NOT_APPLICABLE in the local projection. Never export an unawarded task or a synthetic prior.

For eligible receipts, use the already-frozen mapping: SUCCESS → value 1; FAILURE (`VALIDATION_FAILED`, `NO_SHOW`, `EXECUTION_TIMEOUT`) → value 0; `valueDecimals=0`, `tag1=agentlance-v1`, `tag2=taskFamily`, empty endpoint/feedbackURI and zero feedbackHash. The publisher event/mapping links the registry feedback to the canonical receipt and its policy/result/evidence references. IPFS evidence publication does **not** replace these fields with a new metadata convention.

Use the reputation implementation at the identity adapter's pinned revision. It rejects feedback from an identity owner/approved operator, returns no index from `giveFeedback`, and exposes `getLastIndex`/`readFeedback`. Read the publisher's prior index, call once, require an increment of exactly one and matching stored value/decimals/tags with `isRevoked=false`, then save the receipt mapping and emit the event in the same transaction. Registry rejection or inconsistent return state reverts all publication effects. The global reentrancy guard prevents another receipt from interleaving that sequence. See the [pinned source](https://github.com/erc-8004/erc-8004-contracts/blob/b9e466c250744a7e06b13dff9d3c2844ed64f825/contracts/ReputationRegistryUpgradeable.sol).

The **contract's address** is the feedback client; the external gas payer is not substituted for it. Verify current owner/operator restrictions against the pinned identity implementation. A later transfer/approval that makes the publisher an owner/operator leaves export pending until eligible again; do not switch publishers to evade the rule. A configured relayer can retry within its gas budget, and anyone else can call `publish` without access to private state.

The off-chain adapter qualifies source/ABI, proxy/implementation/admin observations, identity binding and publisher bytecode/configuration at finalized blocks. Unexpected registry changes suspend export and new registry-dependent admission, not canonical settlement/refunds. Pinning a proxy address alone is insufficient. An external proxy's governance remains a trust assumption: off-chain qualification cannot prevent every third party from calling the publisher after an upgrade.

Persist publication operations before broadcast, then reconcile both the publisher mapping/event and actual registry feedback at finalized state. A lost response, different successful caller or deleted local export queue must still converge to the same index. A second publisher instance is outside that guarantee; deployment configuration must select one canonical publisher per market and recovery must not redeploy it to clear state.

## 7. Settlement and private cost reconciliation — L7-03, L7-07

A receipt consumer reuses finalized L4 logs plus fresh market reads. In one SQLite transaction, retain the immutable task/receipt/event provenance, create the necessary export/reconciliation jobs and advance its own consumer checkpoint. Exact event replay is a no-op; conflicting finalized data uses the existing persistent halt. Public receipt projection and private cost delivery are separate: failure to open a private history never blocks global receipt/export progress.

For an agent's own awarded task, call the existing `ExecutionHistory.attachSettlement(executionRef, receipt, stamp)`. If L6 usage has not arrived, expose MISSING_USAGE and retry the join later. If usage predates settlement, retain it and attach the receipt later. Never create a zero-usage report from a settlement alone; only L6's durable execution ledger can establish zero usage.

Build a versioned local reconciliation record from the canonical receipt, **currently selected** report/context, saved estimate/runtime/pricing, and canonical direct-child receipts. Key each version by these input IDs/digests; old versions remain immutable and a local pointer selects the newest complete input selection. Include:

- Execution/task/operator references, receipt and finalized provenance, selected report/estimate IDs, OWN_EXECUTION scope, run status and report provenance/completeness.
- Canonical credited payout/refund and actual direct-child paid amounts. Unsettled or incompletely discovered children remain explicitly unknown. Deposits, refund credits, child payments and withdrawals are distinct; a credited payout is not proof of withdrawal.
- Own reported execution costs and `evaluateForecasts([executionRef])` output, including saved-rate comparison and exclusion reasons. Repricing quantities at forecast-time prices is not a provider invoice or a change to reported actual cost.
- Separate missing/incomplete data reasons. L6 COMPLETE describes its own execution inventory only; it does not establish complete gas, forecasting, validator, child-worker or whole-agent costs.

Keep ingestion/corrections inside L5. Retry a saved delivery with identical report/context bytes and ID. An explicit correction must name the active predecessor, retain unchanged expense IDs and use new IDs for changed expense contents, selecting one complete replacement report rather than adding old and new versions. Never mutate L6's frozen outbox, relabel local telemetry as PROVIDER_ATTESTED, or ingest a child's private expenses as the parent's execution. L5 currently rejects provider-attested items; invoice authentication remains outside this MVP.

When complete, explicitly attributed cost scope is supplied, reuse L1's analytics functions with their full subtree/report requirements. Otherwise publish only the available scoped observations and leave whole-agent profit/system utility unavailable. In particular, missing validator costs are not zero; validator infrastructure is operator-funded and never deducted from the worker's fixed reward. Resource-cost analytics exclude CHILD_PAYMENT transfers and deduplicate `(operator, expenseId)`.

No public reader or feedback payload exposes these private cost records by default. Sharing remains an explicit operator action for L8.

## 8. Durable state and retry behavior

Add closed, versioned private records to the existing journal with bounded counts/bytes. Do not add them to canonical protocol schemas or create another settlement ledger.

| Record / stable key | Required retained state |
|---|---|
| Validation job / digest of execution + four content digests + evaluator version | Canonical task/result snapshot, stamp, pinned image/config, stage, verified content refs, evidence bytes/ref, nonce/expiry/record/signature, operation ID, per-stage attempts, next attempt, deadline and diagnostic |
| Receipt projection / TaskRef | Exact TaskSpec/receipt, event identity and finalized stamp; consumer progress committed atomically |
| Export job / publisher address + TaskRef | PENDING / PUBLISHED / NOT_APPLICABLE, operation ID, index/provenance, attempts, remaining gas budget, next attempt and diagnostic |
| Reconciliation version / operator + ExecutionRef + selected input digests | Immutable selected inputs, scoped observations, missing reasons and active-version pointer; history delivery state |

Validation stages: DISCOVERED → FETCHED → EVALUATING → EVIDENCE_READY → EVIDENCE_PUBLISHED → SIGNED → RELAY_PENDING → SETTLED. WAITING retains the resumable stage/reason. EXHAUSTED stops automatic work; EXPIRED stops verdict work at its cutoff. Both still observe settlement and permit bounded eligible expiry. Mark SETTLED only from a canonical receipt, including a competing caller's result or timeout; retain local evidence without claiming it won.

Unlike paid L6 model/tool calls, an interrupted **pure evaluator** may run again on identical frozen bytes within its total attempt/CPU budget after the previous named container is confirmed stopped. It has no external side effects or signing authority. Saved evidence resumes publication/signing without evaluation. Never repeat a completed or ambiguously submitted market operation under a fresh identity.

Default to at most three automatic attempts per fetch/evaluation/upload/delivery operation, polling no faster than two seconds, with ten-second network attempts and finite per-tick/concurrency/storage/gas ceilings. Persist attempts before dispatch so restart does not refill them. Count each evaluator retry against a persisted total CPU allowance. Configuration may lower limits; raising budgets for retained jobs requires an explicit operator action and does not change signed payloads or task deadlines.

Reuse L4's signed-byte/nonce reconciliation for both market and publisher writes. A second destination is the first real need to factor a shared native-send boundary from `MonadMarket`; retain one nonce lock/queue per sender and destination-specific preflight/receipt verification. Do not serialize `publish` as a fake canonical market `Command` or weaken frozen-command validation. An ambiguous send blocks a fresh nonce until reconciled; a mined rejected transaction can get a bounded new attempt only after canonical failure and unchanged publication eligibility are established. Check the on-chain publication mapping first.

| Failure | Required behavior |
|---|---|
| Validator unavailable, bad prerequisites, exhausted evaluator/IPFS attempts | No invented FAIL; after `validationBy`, existing `expireTask` can release escrow with unknown correctness |
| Chain outage/stale finality | Preserve jobs/intents; no signing or authoritative status changes from a stale read |
| Finalized history conflict | Persist halt; stop new signatures/transactions and local containers; retain evidence for investigation |
| Relay loses response or another caller settles/publishes | Reconcile canonical receipt/mapping before retry; do not duplicate outcome or feedback |
| Registry outage, owner/operator conflict or insufficient exporter funds | Show settled + feedback pending; no effect on payout/refund/counters |
| Usage missing, corrected, censored or ingestion exhausted | Retain the receipt; expose scoped uncertainty or a new selected reconciliation version; no effect on settlement/export |
| Storage full or signer unavailable | Stop admitting work requiring that resource; preserve existing signed bytes and bounded recovery state |

Export/reconciliation have no invented economic deadline after settlement. Automatic work is bounded; exhaustion leaves a visible pending record for an explicit retry. Validator expiry uses the lower cutoff with no fabricated upper deadline, reusing L6's native expiry wrapper. Provide scoped expiry for observed SUBMITTED tasks; L6 still handles child progression, and root allocation/other root keepers remain separately operated.

## 9. Invariants and trust assumptions

- Only L3 establishes success, failure, payout/refund and reputation effects. Local evaluation, A2A completion, cost data or publication failure cannot substitute for a receipt.
- A signature binds the canonical execution, winner, result, policy and exact evidence digest. No worker key or untrusted task data selects the validator, criteria, signer, registry or upload endpoint.
- Exactly one accepted market outcome and at most one feedback per receipt through the configured publisher survive replay/restart and concurrent callers. No synthetic priors or `counterEffect=NONE` exports.
- Settlement, artifact publication, SQLite ingestion and registry publication are separate recoverable effects; there is no assumed distributed transaction.
- Credits are conserved by unchanged L3 rules. Successful child payment survives parent failure; parent observation must retain later child settlements.
- Failed/unavailable telemetry remains unknown, not free. Transfers, own execution, validator overhead and forecast costs retain distinct scope/provenance.
- The pinned validator is trusted to run the reviewed evaluator and protect its key; a signature is not trustless proof of execution. Docker/kernel, RPC finality, registry governance and content availability retain their disclosed trust assumptions. Distinct addresses do not prove independent operators.
- No public evidence package contains private journals, keys, provider credentials or hidden cost history. An expired or changed manifest never silently selects a new validator/publisher for existing obligations.

## 10. Implementation plan

**Goal:** satisfy L7-01–L7-07 and the acceptance gate below, with no changes to market economics, canonical schemas or L6 execution semantics. The explicit new contract is the already-specified receipt publisher. Use Python 3.12, existing HTTP/SQLite/Web3/eth-account/JSON Schema dependencies, Docker and the pinned Foundry workflow; no additional AI framework or message broker.

Implement in this order:

1. Add golden/adversarial evaluator and publisher fixtures under `specs/fixtures/layer-7/`. Freeze only the new publisher/registry ABI and private record/config schemas. Pin the matching upstream registry build dependencies/source and a reproducible Kubo image; document added dependencies/checksums.
2. Add pure evaluation in `modules/validation/`, adjacent tests and a reviewed validator image/entry point under `apps/validator/`. Extend shared Docker controls narrowly for cumulative CPU limits and larger validation transport frames; retain L6 regressions.
3. Add evidence publication under `modules/adapters/storage/`, validation/reconciliation journal records and migration tests. Keep exact-byte evidence and every preexisting L2–L6 claim/intent/outbox across restart.
4. Add the verdict typed-data builder and native `settleVerdict` wrapper; factor only the shared native transaction sender needed by the publisher destination. Add the bounded validator application, role configuration and scoped expiry. No worker API gains signing authority.
5. Implement `contracts/src/FeedbackPublisher.sol` and tests against both adversarial stubs and the pinned real ERC-8004 implementation on local EVM. Add its Python adapter and finalized export projection. Update deployment/check scripts for a separate contract ABI without changing `protocol.abi.json` or the market.
6. Compose agent-local reconciliation using L5's existing selected reports/receipt/evaluation APIs; add bounded read projections, diagnostics and late-report/late-child handling. Do not create a second usage-history database.
7. Add `setup-l7`, `check-l7`, `demo-l7` targets and corresponding scripts, operator/config/environment documentation and README status. Use encrypted keystore references, no raw private-key environment examples. Extend qualification tooling to produce complete digest-bound publisher/registry/validator evidence for the existing manifest format; never fill missing facts with fixture addresses.

Implementation decisions: build the evaluator from a narrow subset of the existing locked dependencies and reviewed source; reuse Docker's existing container boundary with an optional cumulative CPU limit. Keep validation records in a v5 journal table, separate from L6 execution claims. Share the existing native sender's nonce lock and durable operation queue across market and publisher destinations, with destination-specific verification. No new Python dependency or canonical market schema is needed.

Private versioned records and operator configuration use closed field sets in their owning Python modules, following the existing journal conventions; no duplicate public JSON schema is introduced. Retained gas caps and delivery attempt counters determine remaining export exposure. Receipt projections retain the submitted result even when submission and settlement share a block, without admitting redundant evaluation.

Required tests cover meaningful public behavior, not private method shapes. Completion requires executed evidence and a final diff review.

## 11. Acceptance criteria

| Requirement | Required evidence |
|---|---|
| L7-01 | Existing validation golden vectors plus strict JSON, Unicode, deep equality, pointer escaping/indexing/missing values, schema semantics, ordered failures and exact byte/depth/node boundaries; real Docker isolation/CPU/OOM/hang/log tests; digest-matching oversized FAIL versus unverified transport failure |
| L7-02 | Real local-EVM PASS/FAIL via the production evaluator, evidence publisher and attestation builder; unauthorized worker, wrong winner/task/chain/market/result/policy, bad signature/nonce, duplicate settlement and equality-at-deadline rejection; validator outage releases escrow through canonical timeout |
| L7-03 | Atomic projection/checkpoint replay, receipt-before-usage and usage-before-receipt, canonical conflicts, restart at fetch/evaluation/evidence/signature/send/receipt boundaries, and parent failure followed by late child settlement; no lost or doubled credits |
| L7-04 | Concurrent callers and relayer restart create one feedback index through the real new publisher; a second eligible receipt creates another; registry revert rolls back the flag; malformed/reentrant registry behavior rejects; actual pinned owner/approved-operator restrictions and transfer/approval conflicts are tested |
| L7-05 | Public receipt → publisher event/index → registry feedback → exact policy/result/evidence chain is reproducible; correct tags/values and empty optional metadata; NONE outcomes and synthetic priors produce no feedback |
| L7-06 | Stop exporter after settlement and after broadcast, restart/replay, recover the same index and keep payout/refund/counters unchanged; another caller can finish publication; requalification failure visibly suspends export |
| L7-07 | Actual L6 metering delivered once to L5, canonical receipt attachment, saved-rate comparison, missing/censored/zero-usage distinctions, correct report correction selection, no duplicated expense/forecast/child transfer and no fabricated provider attestation or whole-cost completeness |

`make check-l7` must run the full inherited `check-l6` gate, evaluator/property tests, new Solidity contract/security tests, real local-EVM registry/publisher integration, real isolated Docker evaluation and Kubo add/retrieve/restart, journal/native recovery tests, lint/format/ABI/diff checks, and a requirement-to-test report. Required checks must fail clearly when tools are missing; no silent skips or fake IPFS/registry success in the end-to-end gate. Unit tests may isolate external transport, and registry stubs supplement rather than replace the pinned implementation test.

`make demo-l7` must complete an L4-discovered, L5-priced, L6-executed solo task and parent with two public child markets using separate worker/validator processes. Replace direct fixture verdict creation in this path with the real validator. Include deterministic FAIL and validator-timeout/refund cases, exporter downtime/restart, actual feedback reads and an agent-local cost comparison. Reuse L6's independent-worker setup and disclose shared fixture ownership. Produce transaction hashes, immutable content references, receipts, feedback indices, provenance and explicit pending/missing states. No model API key, paid inference, public IPFS service or live funds are required for the local gate.

Offline completion demonstrates the real software boundaries on isolated infrastructure. Live activation additionally requires a fully verified existing `DeploymentManifest`, funded roles, retrievable evidence and qualified registry/proxy/publisher/validator configuration. A local test registry or synthetic address is never a verified Monad deployment. Publication/signing tests do not establish the truthfulness of arbitrary tasks outside the pinned evaluator.

## 12. Open Questions and live activation gates

1. **Deployment facts and external governance:** which Monad testnet identity/reputation deployment matches the pinned source and tested proxy/admin assumptions, and which single publisher address is canonical for this market? The repository has no verified answer. Implement qualification/probe tooling; do not select a public address from an unverified list or silently upgrade the identity pin.
2. **Operational ownership and funding:** who controls the immutable validator key, supplies relay/expiry/export gas, and operates root allocation and non-validation keepers? Local fixtures can supply distinct explicit roles; production ownership, limits and availability commitments must be configured and disclosed before live use.
3. **Evidence retention:** what live retention duration, storage quota, backup/pinning arrangement and public gateway satisfy the operator's availability promise? The MVP retains references and stops on exhaustion; it does not invent an eviction policy or perpetual guarantee. Managed Pinata support is a separate deployment choice, not required for the Kubo-backed MVP.
4. **Broader cost claims:** authenticated provider invoices, complete validator/participation overhead and shared-operator attribution are not established by existing L5/L6 telemetry. What proof and scope would justify them? Until separately specified, keep current provenance restrictions and scoped/unavailable profit results.

These questions gate live configuration or broader claims, not implementation of the specified offline lifecycle.

## 13. Executed acceptance

The full `make check-l7` component suites were cross-checked on 2026-10-08 (UTC): the inherited `check-l6` gate passed through its demo, all six publisher Solidity tests passed, and all **54 Layer 7 Python tests** ran without skips. The first combined run then exposed a 30-second process-control timeout in the parent/child demo; after raising only that harness timeout to 60 seconds, the affected three process tests and the standalone four-scenario Layer 7 demo passed. Frozen ABI comparisons, Ruff lint/format, Forge format and whitespace checks passed before that test-only timeout adjustment and are rechecked for the final diff. The market contract and its canonical ABI/schema remain unchanged.

The publisher tests use the pinned real identity/reputation implementations and cover owner/approved-operator restrictions, transfer conflicts, replay fuzzing, frozen outcome mappings and adversarial rollback/reentrancy. Python tests exercise exact evaluation and signing, real Docker limits, real Kubo restart/readback, retained attempts, manual retry grants, wrong evaluator bindings, durable nonce recovery, same-block submission/settlement, atomic receipt checkpoints, corrections and late child settlement.

The final separate-process demo report is `.scratch/layer7/demo-33dv7hzc/demo.json`:

| Scenario | Canonical outcome | Feedback |
|---|---|---|
| Solo execution, with validator killed after verdict and publisher broadcasts | SUCCESS | PUBLISHED once |
| Parent with two independently bid public child markets | SUCCESS for parent and both children | PUBLISHED for each eligible receipt |
| Deterministic invalid result | VALIDATION_FAILED | PUBLISHED with value 0 |
| Validator unavailable until the deadline | VALIDATOR_TIMEOUT, full refund | NOT_APPLICABLE |

The demo retains transaction hashes, evidence references, actual feedback reads and private cost comparisons. It uses isolated artifact/model transports and shared fixture ownership; Docker, Kubo, the local Monad EVM, production validator, market and pinned reputation/publisher are real. There were no paid model calls or public transactions. The live qualification command's CLI was checked, but no live qualification or deployment was performed; §12 remains unresolved. See the [operation guide](../docs/layer-7.md) for setup, configuration, diagnostics, explicit retries and qualification.
