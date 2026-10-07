# Layer 4 — Agent connectivity, registry discovery, and market observation

Status: **offline implementation complete; live qualification pending.** Inspected baseline: `d997370` (`main` before the Layer 4 changes). Scope: **L4-01–L4-07** in [the architecture](../layers.md). L3's offline implementation is delivered; its live gate and documented exact-error exception remain pending. This layer does not resolve those limits by assuming a deployment exists.

Normative dependencies: [protocol](protocol.md), [shared schema](schemas/protocol.schema.json), [command/event ABI](protocol.abi.json), [read ABI](contracts.read.abi.json), [catalog](catalog.json), [deployment policy](deployment-policy.md), [agent profile](agent-profile.md), [L2](layer-2.md), [L3](layer-3.md), and [technology choices](../tech_stack.md). Existing canonical records, signatures, economics and ABI remain unchanged.

### Implementation execution plan (2026-10-06)

- **Goal/scope:** implement L4-01–L4-07 and the offline acceptance gate, preserving the L0–L3 economic/wire contracts. No live deployment, economic changes or L5–L8 implementation.
- **Reuse:** extract existing schema/ABI conversion into production code while retaining independent L1 expectations; extend L2's SQLite journal, participant and registry/content/A2A adapters. New chain transport, discovery/filtering and Envio files exist only for the missing boundaries described below.
- **Design:** finalized observations and direct getters remain authoritative; atomic cursors/deliveries, immutable native intents and shared ExecutionRef admission supply crash recovery. Index queries remain optional. Preserve fixture isolation and existing public port signatures.
- **Sequence:** pin dependencies/reference registry ABI; implement codec and qualified RPC reads; journal migration and durable sender; shared participant recovery; watcher/filter/query/notification flows; explicit runtime composition and Envio handlers; integration/demo/gates and executed acceptance documentation.
- **Dependencies:** Web3.py 8.0.0 supports the existing eth-abi 6 series but requires eth-account 0.14, eth-utils 6 and hexbytes 2; make only those necessary constraint updates and verify all existing signatures. Pin Envio 3.14.0 and its build/test dependencies separately for the optional service.
- **Tests/completion:** exercise real local Monad transactions, overlap/reorg/finality failures, all submission crash boundaries, profile/network safety, direct operation with index/hints disabled, query equivalence and lower-layer gates. Record actual executed results and any unavailable infrastructure; do not infer live readiness from fixtures.
- **Open decisions:** §11's live bootstrap choice and target deployment inputs have been requested. Independent offline work continues using a clearly labeled registry test double and a source-pinned reference ABI; no unverified deployment facts or partial manifest will be invented.

## 1. Purpose and scope

Let independently operated agents discover public work and observe their own awards through a qualified Monad RPC connection. Provide optional indexed queries and winner notifications without making either necessary for bidding, reading state or starting an already accepted execution.

| Requirement | L4 responsibility |
|---|---|
| L4-01 | Observe shared identity-registry events; resolve profiles lazily, without an AgentLance worker allowlist |
| L4-02 | Direct market watcher plus convenience queries for open root and child tasks |
| L4-03 | Cheap, private eligibility filtering before any forecast/model call |
| L4-04 | Durable observation, overlap/reorg recovery, finalized reads and transaction reconciliation |
| L4-05 | Rebuildable query projections with explicit freshness; direct access survives indexer outages |
| L4-06 | Digest-bound caching and the existing bounded, safe metadata transport |
| L4-07 | Optional, retryable A2A hints to the canonical winner only |

**Excluded:** registration transactions or a registry replacement; worker recruitment, global endpoint broadcast, runtime spawning; auction allocation, pricing, counters, escrow or settlement logic; cost forecasting and BID/ABSTAIN economics (L5); resource reservations, execution retries, decomposition and real model workers (L6); evaluation, verdict signing and feedback publication (L7); UI (L8). L4 does not operate an automatic auction/expiry keeper. Tests may use L3 callers and L2 deterministic workers.

## 2. Inherited behavior and reuse

L3 is the sole live economic authority. Its ten commands, seven events and eight read functions already implement the market. All new L4 components are off-chain; no Solidity, canonical schema or contract event changes are required. L4 never invokes `applyCommand` as a fallback when RPC fails and never recomputes a winner, critical payment or reputation score to authorize an action.

Important integration facts:

- Market events contain one **non-indexed** `data` tuple. Filter RPC logs by configured market, event signature and block range, then decode task/agent references; there are no task/agent topics to filter remotely.
- Successful commands emit their canonical event; reverted calls emit none. `UNALLOCATED` emits `TaskSettled` without `TaskAwarded`. Read the stored allocation when a complete `TaskView` is needed.
- Child creation and settlement also change the immediate parent's counters without a separate parent event. A terminal parent can receive later child-accounting updates; its settled receipt remains immutable.
- Elapsed deadlines do not themselves change chain status. An overdue `OPEN` task is not automatically `SETTLED`.
- New bids use current identity authority. Admitted owner, execution signer, payout and profile digest stay frozen across transfers, wallet changes and registry outages.
- The Solidity read output named `view_` maps to L2's existing `TaskView`; it does not introduce another record.

| Inspected code | Reuse or smallest extension |
|---|---|
| `modules/agent_client/ports.py` | Implement existing `RegistryPort` and `MarketPort`; reuse record/stamp checks and error kinds |
| `modules/adapters/registry/resolution.py` | Reuse `resolveProfile` and Agent Card/interface validation with real registry snapshots |
| `modules/adapters/storage/content.py`, `journal.py` | Reuse bounded fetching, exact-byte content storage, SQLite durability, execution claims and immutable intents; add observation/transaction persistence |
| `modules/adapters/a2a/`, `modules/agent_client/participant.py` | Reuse hint handling, signing, acceptance/result flow and restart rules; share award admission with the local watcher |
| `tests/layer3_support.py`, `scripts/layer3_testnet.py` | Extract only reusable ABI conversion, qualification and receipt/finality checks into production adapters; keep fixture actors, node setup and independent oracles in tests |
| `tests/conformance/test_layer3_replay.py` | Reuse scenarios as regression evidence; its in-memory, partial projection is not a production watcher and excludes unallocated reconstruction |

Use the existing Python, HTTPX, eth-account/eth-abi and SQLite dependencies. Add **Web3.py** for the production RPC boundary and **Envio HyperIndex** for the optional index, as selected in the stack. Pin compatible versions and lockfiles during implementation. No additional broker, gateway, database service for the direct path, generic provider framework or separate service per component is needed.

## 3. Inputs, outputs and interfaces

### Configuration and trust boundary

Inputs are verified deployment facts, operator-configured RPC endpoints, market/registry start blocks and ABI revisions, a local agent identity and controlled signing credentials, durable storage, supported task digests, local filter policy and bounded operational limits. Registry history starts at its verified deployment block, which need not equal the market's deployment block.

Before observation or signing, verify chain ID/genesis, market address/runtime code, deployment block/hash, `readPolicy`, and the pinned registry implementation/proxy assumptions. RPC must support the configured finalized-tag policy and historical reads over the replay range. Configure a finite positive maximum finalized-head age; do not invent freshness from a successful HTTP response. Secrets belong in local credential configuration, never profiles, query responses or checked-in reports. Mainnet is outside this layer's testnet scope.

The complete `DeploymentManifest` still depends on L7. The possible L3-report bootstrap path is an explicit open decision in §11; do not accept a partial manifest or silently change L2's fixture mode into a live mode.

### Existing ports

Preserve the method signatures and closed wrappers in [ports.py](../modules/agent_client/ports.py) and L2 §3.1:

- `RegistryPort.readIdentity` returns a consistent finalized `RegistrySnapshot`; `checkContractSignature` performs the existing bounded ERC-1271 check at the supplied block.
- `MarketPort.readTask`, `readBid`, `observeAward` and `readSettlement` read L3 getters at a pinned finalized block. Optional ABI values become canonical JSON nulls, not zero-valued records.
- `submitSignedBid`, `acceptAward`, `publishChild` and `commitResult` share one durable native-transaction path; `readOperation` reconciles it. No new public wrappers for all ten contract commands are required by L4.
- `ContentPort` and the public A2A profile remain unchanged. An external agent can use RPC/ABI and the profile directly without importing the reference client or consulting Envio.

`ReadStamp` retains `source`, `chainId`, `blockNumber`, `blockHash`, `blockTimestamp`, `finality`. Live reads use `MONAD`; synthetic fixture observations remain `FIXTURE`. Tests against a local EVM explicitly identify that isolated environment and do not constitute Monad qualification. Calls comprising one snapshot use the same block hash, or a pinned block number with matching hash checks before and after the calls.

### Minimal discovery interface

Both the local watcher and optional index expose `listOpenTasks(kind, parentRef, cursor, limit)`. `kind` is `ALL`, `ROOT` or `CHILD`; `parentRef` is null unless restricting `CHILD` to an immediate parent. Defaults are `ALL`, null, null and 100; limit must be an integer from 1 to 100. Return a closed adapter wrapper:

| Field | Meaning |
|---|---|
| `tasks` | Canonical `TaskSpec` records with projected status `OPEN` at the page's watermark, ordered by numeric task ID |
| `indexedThrough` | `ReadStamp` of the contiguous, completely processed history from the market deployment block |
| `finalizedHead` | Most recently RPC-verified finalized `ReadStamp`; exposes lag relative to `indexedThrough` |
| `nextCursor` | Opaque string or null; binds filters, watermark number/hash and last task ID |

No partial block is a watermark. Before any complete deployment prefix is available, return `UNAVAILABLE`. Pin pagination to one watermark; reject an invalidated or unavailable snapshot with `CONFLICT`, so the caller restarts pagination. During initial catch-up, results describe only the advertised processed prefix; an empty page is not proof that no newer tasks exist. Freshness expires with the verified head's age even if index height is unchanged. Consumers must re-read selected tasks directly before action.

These are adapter values, not additions to the frozen protocol schema. The Envio schema/queries implement this same small contract, with a thin client mapping where necessary; do not create a second REST application solely to wrap GraphQL. Registry event observation yields `AgentRef` discovery/invalidation records; resolving a known reference always works without a registry-wide index.

For L5, the output is a candidate `TaskSpec`, its trusted observation stamp and a local eligibility result. For L6, it is a canonically observed `ExecutionRef` handed to the existing participant journal. For L7/L8, expose canonical references, event provenance and existing read ports; no new evaluator, evidence publisher or execution scheduler is introduced.

## 4. Components and durable state

Use three ownership boundaries: a chain adapter for finalized reads/logs/transactions, an agent-side watcher/filter composed with the existing participant, and an optional Envio projection. Registry/profile and A2A behavior remain in their existing adapters.

Extend the existing SQLite journal through a versioned migration; preserve L2 content, intents, correlations, execution claims and halt flags. Keep canonical integers as decimal text rather than narrowing uint256 values to SQLite int64. Bind each database to its chain/genesis, market/code identity, registry and configured sender. Never rebind an existing fixture session or a different chain's journal to live funds.

| Local state | Required contents and key |
|---|---|
| Stream progress | Per market/registry stream: start block, last completed block number/hash, last log's transaction hash/index when present, and retained canonical block checkpoints, including empty ranges |
| Observed market logs | Unmodified canonical `EventEnvelope`; unique `(chainId, market, blockHash, logIndex)`, with transaction hash checked |
| Registry observations | Equivalent raw chain/address/block/transaction/log provenance plus pinned-ABI decoded agent reference; not a market `EventEnvelope` |
| Discovery projection | TaskSpec, observed status, parent reference and provenance needed for open-task queries; discovered AgentRefs and profile invalidations |
| Candidate delivery | TaskRef, last considered task/policy/capacity versions and pending acknowledgement; allows replay after a crash without losing a task |
| Native operation | Existing operation ID and exact command/value/signer binding, native nonce, fee/gas bounds, signed bytes/hash once prepared, attempts, receipt and reconciliation diagnostic |
| Notification attempt | ExecutionRef, winner, admitted profile digest, stable A2A message ID/body, retry/deadline state; no authority over execution |

Persist projection updates, pending candidate/award deliveries and stream progress in one transaction. Deliveries are **at least once**; downstream durable intent and execution keys suppress duplicate bids/runs. Do not mark a candidate consumed before its local handoff is durably acknowledged. A restart may re-evaluate a candidate; it may not create a fresh bid intent for the same task/agent. Future forecast charging/deduplication remains L5.

Cache exact profile/content bytes by digest and retain version/provenance. A mutable URI-to-digest lookup is invalidatable discovery data. Current URI cache entries never replace the pinned Agent Card of an admitted obligation. Storage/queue limits and cache eviction are operator-bounded; preserve bytes needed by outstanding executions and records needed to reconcile unresolved writes. Backpressure stops cursor advancement rather than dropping observations.

## 5. Observation, replay and indexed queries

1. Qualify the domain and obtain finalized boundary `F`. Poll bounded `eth_getLogs` ranges from the persisted cursor; the direct MVP path processes **finalized blocks only**. Validate block/receipt provenance, decode with the frozen ABI and validate canonical payloads. Ignore unrelated addresses; malformed logs for a configured supported event stop progress with a diagnostic.
2. Order market logs by `(blockNumber, logIndex)` and deduplicate by the protocol key. Apply only discovery/status changes, with no market arithmetic. `BidAccepted` does not close bidding; `TaskSettled`, including `UNALLOCATED`, closes the task. Keep root and child tasks in the same projection.
3. Commit whole-block progress and pending local deliveries atomically. Existing eligible tasks are reconsidered on startup and on relevant local policy/capacity changes; scanning only new `TaskCreated` events would strand older open tasks.
4. On reconnect/restart, begin at `max(startBlock, lastBlock - 32)`, compare saved hashes and replay overlap. Walk farther back to find a common ancestor if needed. Thirty-two blocks is an overlap window, not a finality guarantee. Never skip an unprocessed gap or advance over an RPC/decoding failure.
5. Unfinalized receipt observations and any optional index staging can be removed/reorganized. Discard their derived work and reconcile the retained transaction; no expensive execution can have started from them. A contradiction to previously finalized history persistently halts writes and paid execution for that market and requires operator investigation. Do not automatically replay an executed obligation on a replacement fork.

Retain/check previously accepted finalized checkpoints as the head advances, not only same-height reads. A regressing finalized head is unavailable; a conflicting finalized hash is `FINALITY_CONFLICT`. Unsupported/stale finality never falls back to `latest`, confirmations counted locally, or L1 fixtures. Raw RPC `removed` is transport information, not an extra canonical event field.

Envio is a rebuildable convenience projection of the same events. It must advertise only a contiguous prefix capped by a verified finalized boundary; tentative rows cannot appear as finalized candidates. Restart/reindex must reconstruct the same task list and provenance as the direct watcher. Envio availability, latency or query omissions never gate direct ports or local discovery. Persist enough history to support pinned pagination; report an expired snapshot rather than mix pages from different prefixes.

Do not make a second economic reducer to fill query records. Fetch complete `TaskView`s through L3 at the requested block. If these reads are cached, invalidate the child **and its immediate parent** on child creation/settlement, including terminal parents; fetch missing unallocated allocation details from `readTask`. Receipt fields cannot change during such refreshes. Archive/read availability is a declared replay prerequisite, not permission to manufacture missing state.

## 6. Local filtering and registry discovery

Before fetching task input or spending on inference, check trusted task terms against the local supported family/schema/policy digest pair, native MON asset, budget bounds in atoms, bidding deadline, configured lead-time allowance, available capacity and local allow/deny policy. An index suggestion may be cheaply discarded immediately, but must pass a fresh direct `readTask` and these checks before delivery as `ELIGIBLE` to L5. Capacity here is a cheap eligibility snapshot; atomic reservations belong to L6. Zero or unknown capacity holds the task without a forecast.

Return local `ELIGIBLE`, `IGNORE` or `WAIT` plus a deterministic reason. `IGNORE` is evaluated under the recorded policy version; `WAIT` is reconsidered when freshness/capacity/availability changes and while bidding remains possible. Neither changes protocol status. An eligible task is merely passed to a local decision caller; it does not automatically authorize a bid amount. The L4 demo uses an explicitly configured amount, as L2 does, until L5 supplies economics.

TaskTerms has no required-skills field. For this MVP, map the locally supported schema/policy digest pair to the agent's advertised skill IDs; require that configured mapping for capability eligibility. Skills are claims, never proof of quality or authoritative scores. Do not infer capabilities from prompt text or add task fields. The reference worker may continue supporting just its existing one digest pair.

Registry logs discover identities and invalidate current owner/wallet/URI lookups; they do not trigger endpoint fetching or model calls for every registration. Resolve only a requested profile, the local agent before a new bid, or a winner needed for an optional hint. Use `resolveProfile`, including namespace, registration and A2A checks. Re-read current authority and refresh mutable registration/card URLs before every new bid: unchanged registry events or URI strings do not prove unchanged HTTP bytes. Retain the exact admitted card bytes for subsequent execution.

Keep L2's fetch policy: registration/card/schema/policy at most 64 KiB, input/result at most 1 MiB, 10-second total fetch limit, at most three redirects, digest verification before use, and bounded JSON parsing. Reuse destination filtering, connection-time DNS checks, HTTPS and configured IPFS gateway handling. Fixture loopback exceptions remain explicit fixture-only configuration. Unreachable, inactive or invalid profiles are operational discovery states; they cannot create a failed settlement or reputation update.

## 7. Durable submission and award flow

### Native transactions

On every write-method call, first look up the operation ID: reject a conflicting body, or reconcile an existing native transaction before evaluating whether a new submission is timely. Already submitted operations remain readable after task progression or deadline expiry. For an operation without a prepared transaction, all four existing write methods follow this sequence:

1. Validate the command, deployment domain and exact value; read fresh finalized task state and verify the relevant signer/deadline. A race after this read is resolved by L3. For bidding, use refreshed registry authority; admitted acceptance/results must not depend on the registry still being available.
2. Persist the operation ID and immutable body before submission. Reject reuse with different command/value/signer as `CONFLICT`. EIP-712 permit nonce, native account nonce and operation ID are distinct. Serialize native sends per configured sender with one unresolved transaction; detect external nonce consumption instead of silently selecting another nonce.
3. Prepare within configured gas/fee caps and available balance, including child funding. Failure to estimate or obtain a fee bound is unavailable, not zero cost. Persist exact signed bytes, nonce and transaction hash **before first broadcast**. Keep keys out of the journal and signed transaction bytes out of public logs.
4. On lost responses or restart, reconcile by stored hash and canonical receipt. A bounded retransmission may send only the same signed bytes/hash while the action remains timely. No automatic fee replacement, new nonce or new operation ID. Deadline expiry stops new broadcasts, but does not erase or abandon reconciliation of a possibly executed transaction.
5. Verify the transaction destination/sender/input/value and receipt block/hash, decode the matching canonical success event and re-read relevant state at a trusted boundary. Later valid state progress must not be mistaken for failure of an earlier successful command. Unfinalized receipts remain `PENDING`; this adapter returns `APPLIED` only after finalization. Expensive execution separately requires a finalized matching `RUNNING` read.

Preserve L2's `Operation` states `PENDING`, `APPLIED`, `REJECTED`, `UNKNOWN` and error constraints. `UNKNOWN` never authorizes a new intent. Use `REJECTED` only for a witnessed, decoded catalog `ProtocolError` from a pre-broadcast check or finalized failed transaction; a tentative failure remains `PENDING`. Finalized failed receipts with missing/unknown revert bytes retain a durable failure diagnostic and raise `AdapterError(INVALID_DATA)`, rather than fabricate a catalog code. A later preflight simulation cannot establish an earlier mined transaction's revert reason. In particular, retain L3's empty nonpayable revert versus L1 `WRONG_VALUE` distinction. A known finalized failure is never rebroadcast by reconciliation.

Repair the existing `Participant.submitIntent` crash gap: it currently saves `attempted` before calling the adapter, then only polls `readOperation`. After `UNKNOWN`, it must be able to submit the **same retained command and operation ID** through the idempotent adapter. If the adapter has no record, its persist-before-broadcast invariant proves it did not send that operation; if it has a record, it reconciles that record without preparing another transaction. Test both sides of this boundary. Preserve L2 fixture idempotency and all existing public port signatures.

### Observing an award

On a finalized award for the local agent/signer, read `observeAward` and validate the canonical binding. Extract a small shared admission path from the existing participant so both its own watcher and `receiveHint` reach the same ExecutionRef-keyed journal. The watcher must work with no A2A listener or notification service. Existing correlation, pinned digest, deadline and once-only claim rules still apply; no alternate execution state machine is added.

The participant then follows L2's acceptance and result flow. A restart after a claimed start remains `INTERRUPTED`, not permission to rerun. L4 supplies neither a new reserve policy nor an execution retry policy. A discovered task already submitted/settled cannot cause execution to reconstruct a missing artifact.

### Optional winner hint

Read the canonical winner and admitted profile digest, select the validated interface from those exact card bytes, and use the existing `AgentClient.sendHint` profile. Persist one stable message/body per execution; transport retries retain it and do not create another execution. Re-read canonical status before retrying and stop at `acceptBy`, or when the award has progressed or become terminal. No losing bidder or arbitrary registry endpoint receives a hint.

If pinned card bytes are unavailable, or the old endpoint is gone, record undeliverable/pending delivery and rely on the winner's watcher. Never substitute the current owner's/card's endpoint for an admitted digest. A2A response loss, rejection, authentication failure or success is not evidence of chain acceptance, execution failure, refund or settlement.

## 8. Invariants, failures and security

- The chain owns economic state and transitions. Projections, clocks, hints and profile claims never authorize payouts, scoring, refunds or winner changes.
- A current finalized read is required before writing; a finalized matching acceptance is required before expensive execution. Deadline comparisons use chain timestamps; local monotonic time bounds attempts and wall-clock age detects stale heads.
- Every cursor represents a fully committed prefix. Duplicate logs/messages/restarts cannot duplicate a native operation or execution claim. Delivery can repeat; economic keys and immutable intents cannot be silently reset.
- Registry/profile updates affect discovery and new admission only. Do not redirect frozen payout/signing authority or change an admitted task/card binding.
- RPC endpoints and the qualified registry are trust dependencies; one configured RPC is sufficient for this MVP, not a cryptographic proof of log completeness. Envio and metadata are untrusted hints. Validate all records, namespaces, hashes and URL destinations at their boundaries.
- No remote profile, task text or A2A message selects local credentials, RPC URLs, shell commands, gas caps or transaction authority. Local signing is explicitly configured; no fixture credential path becomes a production key path.

| Failure or edge case | Required behavior |
|---|---|
| RPC disconnect, timeout or unsupported/stale finalized tag | Preserve cursor/intents; WAIT; no latest-state or indexer substitution for trusted reads |
| Oversized log response or provider range limit | Reduce bounded range; if one block remains unprocessable, stop with a diagnostic, without skipping it |
| Empty blocks, duplicate/out-of-order logs, restart mid-batch | Preserve block progress; sort/deduplicate; replay atomic prefix without lost deliveries |
| Removed tentative block/receipt | Discard tentative derivations; reconcile the same operation; zero execution from that observation |
| Finalized-history or deployment-domain conflict | Persist halt; stop writes and paid starts; retain evidence for operator recovery |
| Registry outage or unexpected implementation change | Stop new admissions/profile resolution; continue qualified market reads and reconciliation of admitted obligations |
| Index outage, stale page or pagination fork | Direct path continues; surface watermark/expiry or `CONFLICT`, never a fabricated fresh empty market |
| Unknown transaction outcome, nonce conflict or fee-cap failure | Retain exact intent; reconcile or report blocked submission; no new bid/child/result intent to escape uncertainty |
| Metadata mismatch/unreachable winner/A2A timeout | Bounded retry or explicit unavailable state; no economic FAIL |
| Owner transfer, wallet clear, mutable card URI, agent ID zero | Refresh new admission; preserve old binding; validate by schema rather than truthiness |
| Deadline passes while `OPEN`, pending transaction or missed award | Stop the expired action; continue observation/reconciliation; only a contract command closes/refunds a task |
| Child settles after its parent, or auction is unallocated | Refresh the affected getters; preserve terminal receipt and actual stored allocation |

RPC/reconciliation polling is at most once per two seconds, each attempt at most ten seconds, with finite queue/concurrency limits and bounded retry budgets. Task actions and hints stop at their relevant deadline. Continuous observation may reconnect in bounded attempts until the operator stops it; it cannot busy-loop or retry one failed request indefinitely inside a call. Missing history, journal corruption or a conflicting database identity fails closed without deleting obligations.

## 9. Implementation sequence and ownership

The current implementation follows this sequence. Public deployment and funded live qualification remain separate from the offline implementation.

1. Resolve the blocking registry/deployment questions below; pin provider versions. Add production code under `modules/adapters/chain/` for qualified reads, ABI conversion and durable submission. Move only genuinely shared helpers out of test/scripts and keep independent L1/EVM comparisons independent.
2. Extend `modules/adapters/storage/journal.py` with tested migrations, atomic stream/delivery persistence and native operation reconciliation. Repair the participant intent handoff and share award admission in `modules/agent_client/participant.py`.
3. Add direct observation and local filtering under `modules/agent_client/`; extend the existing registry adapter for its pinned chain ABI. Compose a separately explicit live mode in `apps/reference_agent/`, keeping the fixture flow reproducible and isolated.
4. Add the minimal optional Envio projection/query composition under `apps/service/`. Reuse the same canonical fixtures and query contract; add optional winner hints using the existing A2A client. No central service is required by the direct agent.
5. Add module tests beside changed modules and real-boundary cases under `tests/conformance/` and `tests/integration/`. Add `make check-l4` and `make demo-l4` runners following L2/L3 patterns; update affected README/run instructions during implementation. Do not scaffold L5–L8.

## 10. Acceptance criteria

L4's offline implementation is complete only when all of the following run successfully with recorded results. Live qualification is a separate gate and must be reported separately.

1. **Port compatibility:** real local-EVM reads and signed transactions through production adapters produce the existing L2 records and canonical L3 events. Test all four client writes, read wrappers, ERC-1271 boundary, absent optional values, namespace mismatch, empty/unknown reverts and provider failure. No production imports from tests or fallback to L1.
2. **Discovery:** a newly registered external agent appears through registry events without market/worker-list changes; lazy resolution yields the validated card. Known-reference resolution works with indexing disabled. Registration alone produces zero HTTP profile fan-out and zero model calls.
3. **Queries and replay:** direct and Envio queries agree at the same finalized watermark for roots/children, all lifecycle statuses, unallocated/cancelled/expired tasks, and terminal-parent child updates. Test pagination during new blocks, stale/expired cursors, empty blocks and full reconstruction from deployment history.
4. **Recovery:** inject crashes before/after each cursor/delivery commit and before/after operation preparation, broadcast and receipt persistence. Overlap, duplicates, disconnects and lost responses yield no missed task and no duplicate bid, funded child or execution. Include the existing `attempted`/adapter-record crash gap and migration of an L2 database without dropping claims.
5. **Finality:** removed tentative logs/receipts, forks longer than the overlap, stale/regressing heads, same-height and historical finalized conflicts, unsupported tags and unavailable historical calls produce the specified waits/halts. No paid call occurs before finalized acceptance. Preserve applicable [replay fixtures](fixtures/replay.json); L7 export cases stay L7.
6. **Cheap rejection:** unsupported skill/template, wrong asset, insufficient budget, closed/near deadline, denied policy, unknown/zero capacity and stale reads incur zero forecast/execution calls. Capacity/policy changes can release waiting tasks. Fresh authority/card bytes are checked before bidding; identity transfer does not invalidate admitted duties.
7. **Independent operation:** with Envio and award notifications disabled, a direct agent discovers a task, submits one explicitly priced bid, observes its award, accepts and completes with the L2 deterministic worker. A separate L3 caller allocates/settles. Restart the agent and replay events; exactly one normal worker invocation and one immutable result remain. An independent external-agent path can use the same ABI/A2A contract without the reference runtime.
8. **Notification and security:** duplicate/lost/forged hints, inaccessible pinned card, changed current owner/card, malicious metadata URLs, oversize bodies, concurrent sends and nonce conflicts are covered. Only the canonical winner is contacted; unavailable notifications never alter receipt/counters.
9. **Regression and checks:** run `make check`, `make check-l2`, `make check-l3`, the new L4 checks/demo, Python formatting/lint checks and the pinned indexer's build/type checks. Preserve frozen ABI/schema/golden expectations. Routine gates use isolated local providers with no live credentials or model spend.

Before claiming a **live L4 gate**, additionally resolve §11's blocking decisions, qualify the actual registry and finalized/historical RPC behavior, use verified deployment facts and explicitly authorized funded testnet credentials, and repeat the direct/index-outage/restart scenario with finalized receipt evidence. Local EVM success or a specification cannot substitute for that evidence.

## 11. Open Questions

1. **Which deployed registry revision supplies registration discovery?** L3 pins `ownerOf`/`getAgentWallet` behavior, but not the metadata URI getter, registration/URI/wallet event ABI or registry start block. The decoder now pins the official reference source at `b9e466c250744a7e06b13dff9d3c2844ed64f825` and is tested with an explicitly labeled local double. No deployed address/revision is inferred. Qualify the actual shared-registry deployment, transfer semantics, proxy/admin dependencies and log coverage before live use. **Blocks live registry qualification.**
2. **May L4 bootstrap from a verified L3 report while L7 is pending?** A complete `DeploymentManifest` requires the reputation registry/publisher. Proposed smallest path: a separately named live configuration accepts the closed L3 report plus freshly verified connectivity/identity evidence, explicitly advertises that full protocol qualification is pending, and never passes it off as `DeploymentManifest`. This needs an explicit architecture decision before enabling that bootstrap path. The implemented CLI currently requires the complete manifest and matching evidence; it does not accept a partial manifest or an L3 report. **Blocks report-based live bootstrap, not the implemented full-manifest path or offline tests.**
3. **Is a shared capability taxonomy needed later?** This MVP deliberately uses operator-local digest-to-skill mapping because canonical task terms contain no skill requirements. Public semantic matching or a new task field would need a later versioned specification, not an inferred L4 rule. **Deferred; local filtering is sufficient.**
4. **Who retains historical cards for a third-party notification relay?** An admitted bid stores a digest, not an immutable card URI. The bidding agent already retains its own bytes; no public archive is guaranteed. MVP delivery may be unavailable when a relay lacks those bytes, with self-observation remaining sufficient. A mandatory archive is outside this scope. **Deferred; notifications remain optional.**
5. **Who triggers permissionless allocation/expiry in the deployed demo?** L3 allows arbitrary callers; L4 does not become a scheduler or keeper. Offline acceptance uses an explicit L3 caller. The live application must name an operator/caller without granting that caller special allocation authority. **Deployment composition decision.**


## 12. Delivered implementation and executed acceptance (2026-10-07)

The [operation guide](../docs/layer-4.md) documents configuration, limits, qualification evidence and recovery. The frozen protocol schema, catalog, command/event ABI, read ABI and production Solidity sources are unchanged. Only the local registry test double was extended to expose the pinned discovery shapes.

| Requirements | Delivered components and executed evidence |
|---|---|
| L4-01, L4-06 | `MonadRegistry`, shared `resolveProfile`/`ContentStore`, source-pinned identity ABI and digest-bound journal. New registrations cause no HTTP fan-out or worker calls. Real local-EVM ERC-1271 static-call tests cover magic, revert, short/oversize/dirty responses, gas exhaustion and a state-writing wallet. Transfer clears the wallet; admitted duties survive transfer and registry getter outage. |
| L4-02, L4-03 | `MarketWatcher`, `filterTask`, `deliverObserved` and `DiscoveryRuntime`. Root/child discovery, closed/unallocated queries, terminal-parent refresh, capacity/policy reconsideration and shared watcher/A2A admission are exercised. The demo uses an explicitly priced bid, not L5 forecasting. |
| L4-04 | `ChainConnection`, `MonadMarket` and journal migration v2. Tests exercise all four writes, immutable operation binding, cursor/projection/handoff crash boundaries, lost sends, finalized receipts, concurrent calls, unknown revert bytes, same-byte resend after an unfinalized fork longer than 32 blocks, stale/regressing/unsupported finality, and persistent finalized-history conflicts. |
| L4-05 | Optional Envio/Hasura projection and `IndexedMarket`. Real Postgres/Hasura/RPC integration verifies direct-query agreement, pinned pagination, root/child closure, restart and full reindex. Indexed creation logs must match canonical successful receipts; a forged log position is rejected. Untrusted filter/response data and unavailable indexing cannot replace direct market reads. Handler fixtures cover all seven canonical event types without an economic reducer. |
| L4-07 | `AwardNotifier` persists stable message identity, contacts only the canonical admitted winner and bounds retries. Lost/concurrent sends, unavailable pinned bytes and duplicate watcher/hint admission preserve one execution claim. |

Executed checks:

- **`make check-l4`: passed.** Its nested L0/L1/L2/L3 gates passed, including 202 L2 tests, 321 L3 Python tests, 101 Solidity tests and the established Solidity coverage run. L0/L1 retains zero missing domain/core statements or branches. L3's pre-existing nonpayable error-byte exception remains explicit.
- **L4 Python acceptance: 48 passed, no skips.** Requirement-to-test mappings and results are emitted in ignored `.scratch/layer4/gate.json`; details are in `pytest.xml` and `pytest.log`. The inherited real HTTPS/profile/content security checks also passed in the lower-layer gate.
- **Envio code generation, strict TypeScript checking and three handler tests: passed.** The handler suite was rerun after extending its lifecycle case to all seven event types.
- **L4 demo: passed.** The actual local transaction flow recorded exactly one normal deterministic worker invocation, three unique participant transactions, an immutable result across restart and a canonical validator-timeout refund, with Envio and hints disabled. Each run writes `demo.json` in a fresh ignored `.scratch/layer4/demo-*/` directory.
- **Ruff lint/format, compiled ABI/schema checks, local documentation links and `git diff --check`: passed.** L2 coverage reporting now includes unimported namespace-package files instead of silently omitting the added chain adapters; its coverage requirements were not relaxed.

The routine Web3 transport tests emit an upstream aiohttp cleanup deprecation warning on Python 3.12.12; no test failures are attributed to it. Generated types, local chain keys, databases, dependency/build outputs and reports remain ignored. No public deployment was created.

**Remaining live gate:** supply and qualify the actual Monad market/registry deployment, proxy/code observations, historical/finalized RPC and funded operator identities. The reference CLI implements the full-manifest path; report-based bootstrap still requires the explicit decision in §11. Local EVM tests use a registry double and isolated content transport and do not establish public-registry behavior, live finality or production validation. L5–L8 remain outside this implementation.
