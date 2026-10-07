# Layer 3 — Monad protocol implementation plan

Status: **offline implementation delivered; final acceptance results are recorded in §16. Live gate pending.** Implementation baseline: `main` at `e1dbe63` (the committed L3 plan on top of the L2 baseline `b86197f`). Scope: **L3-01–L3-09 only**. The Solidity implementation and local transactions do not imply live deployment or resolution of the exact-error exception in §1.1. This document does not replace the frozen protocol or authorize deployment.

Implementation uses the existing checkout, initially clean on `main`. The execution plan below records reuse and implementation order. Generate Solidity wire declarations deterministically from the existing schema (checked for drift, never fixture expectations); pin downloaded development tools/libraries by checksums and keep installed copies ignored. Preserve the documented nonpayable ABI boundary and executable L1 verdict guard order. Live deployment remains dependent on verified registry/RPC/key/funding inputs and explicit deployment authorization; no synthetic manifest will substitute for them. Record executed acceptance separately when the gate has actually run.

### Implementation execution plan (2026-10-06)

- **Goal/scope:** implement L3-01–L3-09's contract, local transaction flow and independent conformance tooling, in the sequence in §11. Preserve L0–L2 records and behavior; L4–L8 and unauthorized live broadcasts are excluded.
- **Inspected reuse:** the existing pure `applyCommand`, math, checkpoint and ledger modules are the independent Python oracle; domain validation, signing types, ABI generation helpers, HTTPX/eth-account/eth-abi and existing check runners supply the test boundaries. There is no existing Solidity implementation to extend. The new contract owns all economic writes; test RPC helpers are not a production MarketPort.
- **Design:** one immutable market, one canonical wire declaration set, bounded top-two admission and parent bookkeeping, checkpoint snapshots, strict owner/validator signatures and pull credits. Preserve complete records and ordered events. Rejected transactions must leave all money, records, nonces and logs unchanged.
- **Changes/sequence:** first pin tools and the Python 3.12.12 URI oracle, then generate/check wire declarations; implement pure arithmetic, checkpoint, URI and signature boundaries; compose ten commands, views and settlement; add focused Solidity adversarial tests and independent RPC differential tests; integrate gate/demo and document actual results. File ownership remains §11, with new helpers only where used.
- **Tests/completion:** run existing L0/L1/L2 gate, formatter, optimized build/size and ABI checks, Solidity golden/fuzz/invariant tests, full-record Python/EVM comparisons and real local funded scenarios. Record fixture IDs, coverage, tool versions, seeds and limitations. Live evidence is a separate gate and cannot be inferred from local success.
- **Open decisions:** retain §1.1's nonpayable error-byte exception and executable verdict signature-first guard order. Do not change economic rules or golden expectations. Verify toolchain availability and bytecode size early; report any constraint requiring a specification decision instead of weakening its requirement. A qualified registry, RPC finality, controlled roles/funds and explicit deployment authorization are required for testnet.

## 1. Authority, scope and reuse

Implement one immutable AgentLance market on Monad. The chain admits bids, chooses the winner and critical payment, holds escrow, enforces execution/delegation deadlines, verifies verdict authorization, credits settlement and updates allocation reputation atomically. Anyone can finalize an eligible auction, submit a valid verdict, or expire an overdue task.

The normative inputs are [protocol.md](protocol.md), [protocol.abi.json](protocol.abi.json), [catalog.json](catalog.json), [the shared schema](schemas/protocol.schema.json), [signing definitions](signing/types.json), [signing rules](signing/README.md), [deployment policy](deployment-policy.md), [agent profile](agent-profile.md), [L0](layer-0.md), [L1](layer-1.md), [L2](layer-2.md), [layers.md](../layers.md) and [tech_stack.md](../tech_stack.md). Preserve all existing golden data. An implementation discrepancy is a failing test, not permission to regenerate its expected output.

| Existing component | L3 use |
|---|---|
| `modules/market_core/market.py` | Independent reference for score, ordering, best two and price; port the arithmetic, never call Python from the contract |
| `reputation.py`, `accounting.py`, `transitions.py`, `state.py` in that module | Reference for checkpoints, guards, receipts, credits and delegation; compare complete transitions |
| `modules/domain/records.py` | Reference for canonical encodings, AgentRef ordering, ranges and URI validation |
| Existing market-core tests and `tests/test_layer1.py` | Reuse fixture expansion rules and actors; preserve separation of pure guard tests and public-command tests |
| `modules/agent_client/ports.py` and L2 signing/registry adapters | Preserve their record/authority boundary; supply contract reads suitable for later adapters |
| `scripts/check_abi.py`, `check_specs.py`, signature checkers | Extend verification of compiled ABI and Solidity hashes; keep the existing independent Python/ethers checks |
| Existing `eth-account`, `eth-utils`, HTTPX, pytest and Hypothesis | Signing, ABI/RPC test transport and differential tests; no new production Python service |
| New Solidity + Foundry files | Necessary chain implementation, cryptographic boundary, storage and adversarial EVM tests |

Excluded: agent registration service, owned agent fleet, auction relayer authority, A2A servers, discovery/indexing, production `MarketPort` transport/reconciliation, Jev/cost decisions, LLM execution, objective evaluator execution, ERC-8004 feedback publisher, UI, ERC-20s, fees, staking, appeals, upgrades, task edits and bid replacement. L3 uses fixture artifacts and fixture validator signatures. Their validity tests signature binding, not production correctness evaluation.

### 1.1 Baseline compatibility issues must stay visible

There are two existing cross-artifact boundaries. Do not silently rewrite a frozen file to make an L3 test pass.

1. **Nonpayable rejection bytes.** The frozen ABI declares eight commands `nonpayable`. Solidity rejects nonzero value before their bodies execute, whereas L1 and `lifecycle/nonzero-call-value` return `WRONG_VALUE`. Preserve the frozen ABI. Test the actual EVM rejection, zero state/log effects, and the separate L1 error; report this exact error-encoding difference in conformance results. It is not an economic difference. Do not claim byte-for-byte parity for this case, normalize an empty revert into a witnessed `ProtocolError(13)`, or add a handwritten dispatcher to hide it. If acceptance requires the literal custom error for this ABI-invalid call, an explicit baseline decision is required before that acceptance item can pass. The implementation work otherwise proceeds.
2. **Complete deployment manifest depends on L7.** `DeploymentManifest` requires a verified reputation registry and feedback publisher. L3 cannot fabricate those fields or implement the publisher to fill them. Section 12 defines a verified L3 deployment report and the exact remaining full-manifest conditions. A report is not a partially populated canonical manifest. Full protocol deployment readiness remains pending until those conditions are met.

There is also a guard-order precision issue: the protocol prose lists verdict nonce/expiry before signature, while executable L1 verifies the signature first. For ABI-valid commands reproduce `applyCommand` and its tested guard order below. Do not reorder expensive checks merely to change which stable error wins. Record this source/prose discrepancy in the acceptance review; invalid-signature-plus-used-nonce and invalid-signature-plus-expired-verdict regressions must expose the executable order. No economic or authorization rule changes.

## 2. Contract architecture and fixed configuration

Deploy **one `AgentLanceMarket`**, with internal libraries compiled into it. No proxy, delegatecall router, administrator, owner-only action, pause switch or mutable policy configuration. The only external state dependencies are the configured identity registry and the current bid owner's signature contract during admission. Withdrawal is the only outbound value transfer. Verdict verification is ECDSA and does not call the validator.

Constructor arguments: `address identityRegistry_`, `address validator_`. Both must be nonzero; the registry must have code. These checks do not establish registry conformance or validator key control; deployment verification does. Constructor failure uses the relevant existing `ProtocolError` (`INVALID_RANGE` for zero roles, `IDENTITY_UNAVAILABLE` for missing registry code). No constructor value.

Fixed values:

| Configuration | Value |
|---|---|
| schema / policy / mechanism version | 1 / 1 / 1 |
| Native asset | `{kind: "NATIVE", symbol: "MON", decimals: 18}` |
| Task family | Exact string `structured-output-v1` |
| Q | 1,000,000 |
| Minimum acceptance window / synthesis slack | 120 / 120 seconds |
| Maximum depth / lifetime children per task | 2 / 4 |
| Identity registry / validator | Constructor immutables |
| Namespace | Actual `block.chainid`, `address(this)`; no caller-selected chain or market |
| EIP-712 domain | `AgentLance`, string version `1`, actual chain ID, this market |
| ERC-1271 staticcall cap | 50,000 gas |

Do not cache a domain separator across a different chain ID. Use the actual chain ID when checking references and producing EIP-712 digests. Stored historical TaskSpecs retain their original references; an unexpected network chain-ID/genesis change invalidates the deployment qualification and is not an automatic migration of those tasks. Cross-chain relocation is outside v1.

Use internal helpers with camelCase names: `validateTerms`, `admitBid`, `updateTopTwo`, `calculateAllocation`, `snapshotCounters`, `recordOutcome`, `validateChildEnvelope`, `settleTask`, `verifyBidPermit`, `verifyValidationVerdict`. Libraries are implementation boundaries, not separately governed services.

### 2.1 Toolchain

Initial reproducible pins: Solidity **0.8.30**, OpenZeppelin Contracts **5.4.0**, Foundry **1.8.0**, forge-std **1.9.7**. Resolve each release to a full revision/checksum in the implementation's dependency lock. Use OpenZeppelin `ECDSA.tryRecover` and the ERC-1271 interface; do not inherit governance or upgrade machinery. A generic signature checker must not broaden the frozen signature acceptance rules.

Set compiler `evm_version = "paris"`, optimizer enabled with 200 runs and `via_ir = true`. Explicit Paris bytecode avoids reliance on newer compiler-default opcodes; it does not select the runtime gas schedule. Set Foundry `network = "monad"` and explicitly pin the qualified Monad hardfork in the execution profile. The initial local profile is `monad:MonadTen`; verify the live revision before running the testnet gate. Never silently use a newer default. The compiler release changed its default EVM target, so the explicit compiler setting is deliberate. [Solidity release](https://soliditylang.org/blog/2025/05/07/solidity-0.8.30-release-announcement/), [OpenZeppelin release](https://github.com/OpenZeppelin/openzeppelin-contracts/releases/tag/v5.4.0), [Foundry release](https://github.com/foundry-rs/foundry/releases/tag/v1.8.0), [forge-std release](https://github.com/foundry-rs/forge-std/releases/tag/v1.9.7).

Monad's official guidance specifies upstream Foundry with Monad execution support and `anvil --network monad`. Record exact tool versions and execution hardfork in every acceptance report. Do not substitute a chain-ID change on an ordinary Ethereum Anvil instance for Monad execution. [Monad Foundry guide](https://docs.monad.xyz/tooling-and-infra/toolkits/foundry).

## 3. Storage and read model

Solidity wire structs must have the frozen ABI component order/types. Define them once in `ProtocolTypes.sol`; commands, events and getters reuse them. Constants/nullable wrappers are not new JSON models. Store enough data for bounded reads without relying on a future indexer.

### 3.1 Market storage

Declare the following state in order; retain the resulting compiler storage-layout artifact with the build report. No manual slot numbers or upgrade gaps are needed.

| Field | Solidity storage type | Rule |
|---|---|---|
| `taskCount` | `uint64` | Last assigned task ID; zero initially; roots and children share it |
| `writeEntered` | `bool` | One guard across every external write |
| `tasks` | `mapping(uint64 => TaskData)` | Existence determined by `1 <= id <= taskCount`, not OPEN's zero enum |
| `credits` | `mapping(address => uint256)` | Withdrawable native atoms; no transferable credit token |
| `usedNonces` | `mapping(address => mapping(bytes32 => mapping(uint256 => bool)))` | Signer, primary-type hash, nonce; this contract supplies market scope |
| `histories` | `mapping(uint256 => mapping(bytes32 => Checkpoint[]))` | Agent ID, `keccak256(bytes(taskFamily))`; chain/registry implicit in market namespace |
| `depositedAtoms`, `escrowTotalAtoms`, `creditTotalAtoms`, `withdrawnAtoms` | Four `uint256` values | O(1) aggregate accounting, updated with each economic transition |

No enumerable task, bidder, identity, credit-owner or descendant arrays. Enumerating history is an off-chain concern. Checkpoint arrays are the one necessary historical sequence.

`TaskData` declaration order:

1. `TaskSpec spec`: complete immutable canonical record, including three content refs, deadlines, ancestry and snapshot.
2. `uint8 status`, `uint8 topCount`, `bool hasAllocation`, `bool hasResult`, `bool hasReceipt`, `uint32 childrenCreated`, `uint32 activeChildren` (packed metadata).
3. `uint96 ownWorkReserveAtoms`, `uint96 reservedChildBudgets`, `uint96 committedChildPayouts`.
4. `uint256[2] topAgentIds`; only the first `topCount` entries are meaningful. Agent ID zero is valid.
5. `mapping(uint256 => StoredBid) bids`; `StoredBid` contains `bool exists` and canonical `Bid value`.
6. Canonical `Allocation allocation`, `ResultCommitment result`, `SettlementReceipt receipt`, each gated by its presence flag.

Use complete canonical records initially, rather than a second compact representation with an independent reconstruction policy. Some reference/version fields are repeated; this is bounded storage, paid by that command's sender. Do not store the winner's Bid a second time: resolve it from the allocation and immutable bid mapping. Task escrow is derived as `status == SETTLED ? 0 : budgetAtoms`; `escrowTotalAtoms` tracks its aggregate. A failed allocation stores the UNALLOCATED Allocation before its receipt, matching L1; cancellation/allocation expiry have no Allocation.

`Checkpoint` is `{uint64 blockNumber; uint64 successes; uint64 failures;}` and fits one slot. Use native packing, not unchecked bit manipulation. Internal narrower types must never leak into a frozen ABI field with a different width.

### 3.2 Read-only extension ABI

Keep `protocol.abi.json` unchanged. Add a separate `specs/contracts.read.abi.json` during implementation for these view functions; verify it against the build. It is a contract read interface, not a new definition of TaskSpec/Bid/Receipt.

| View | Inputs → output |
|---|---|
| `readTask` | `TaskRef taskRef` → `TaskView view` |
| `readBid` | `TaskRef taskRef, AgentRef agentRef` → nullable `Bid bid` |
| `readSettlement` | `TaskRef taskRef` → nullable `SettlementReceipt receipt` |
| `readCounters` | `AgentRef agentRef, string taskFamily, uint64 snapshotBlock` → `uint64 successes, uint64 failures` |
| `readCredit` | `address owner` → `uint256 amountAtoms` |
| `isNonceUsed` | `address signer, bytes32 primaryTypeHash, uint256 nonce` → `bool used` |
| `readPolicy` | no inputs → `uint256 chainId, address market, address identityRegistry, address validator, uint8 policyVersion` |
| `readAccounting` | no inputs → `uint64 taskCount, uint256 depositedAtoms, uint256 escrowTotalAtoms, uint256 creditTotalAtoms, uint256 withdrawnAtoms` |

`TaskView` ordered fields: canonical `TaskSpec task`; `uint8 status`; nullable canonical `Allocation allocation`; nullable canonical `Bid winningBid`; `uint96 ownWorkReserveAtoms`; `uint32 childrenCreated`; `uint32 activeChildren`; `uint96 reservedChildBudgets`; `uint96 committedChildPayouts`; nullable canonical `ResultCommitment result`; nullable canonical `SettlementReceipt receipt`. This is exactly the L2 view's economic data. Its `stamp` comes from the RPC block/receipt, never a synthetic contract-provided finality claim.

All task views validate namespace/existence (`UNKNOWN_TASK`); identity views validate the configured namespace (`IDENTITY_UNAVAILABLE`) without reading current registry ownership. Missing bid/receipt returns absent, not an error. `readCounters` accepts only the production family (`UNSUPPORTED_POLICY` otherwise); pure other-family isolation uses a test harness. Future snapshots return the latest checkpoint at or below the supplied height, as L1 does. Nonzero role arguments retain Address validation. Unknown primary-type hashes return false; this does not authorize a new signed command.

A future L4 adapter reads all components at one pinned finalized block and uses `checkTaskView` unchanged. Operation IDs, transaction hashes, confirmation polling and restart reconciliation remain adapter concerns; do not store them as task authority. There is no `observeAward` transaction: it is a verified read of the Allocation and frozen winning Bid.

## 4. ABI and input validation

`ProtocolError(uint16 code)` uses [catalog.json](catalog.json) exactly. Terminal reasons are receipt values, not transaction errors. Use explicit `uint8` wire values for enums, validate membership and map to constants; Solidity enum panics must not replace reachable protocol errors.

| Wire detail | Required handling |
|---|---|
| Commands | Exact function/argument names, tuple order, outputs and mutability in frozen ABI; only creation functions return TaskRef |
| References | Nested tuples in ABI; EIP-712 alone flattens them |
| Nullable field | `(bool present, T value)`; absent recursively contains zero/empty fields; reject nonzero hidden payload with `INVALID_ENCODING` |
| Present values | Task ID/chain ID/role address nonzero; agent ID and digest zero are valid |
| Numeric widths | Money/alpha uint96; timestamps/task IDs/counters uint64; chain/agent/nonce uint256; p uint32; score int256 |
| Award ID | `ExecutionRef.awardId` and EIP-712 use uint8; `Allocation.awardId` uses **uint32** in the frozen ABI; do not homogenize them |
| Constant strings | Asset kind/symbol and task family remain strings, not invented enum replacements |
| Signatures | Present bytes length 1–4096; EOA path additionally requires exactly 65 |
| JSON adapter | Existing schema rejects unknown fields, null omissions, noncanonical decimals and invalid Unicode; Solidity does not parse JSON |

Reject ABI-decodable but invalid constants/ranges/addresses before state transitions, using the existing boundary rules (`INVALID_ENCODING` for encoding, `INVALID_RANGE` for ranges). Business violations that reach L1 guards retain their specific errors. ABI-decodability failures, unknown selectors, out-of-gas, and nonpayable value rejection are separately reported EVM entry failures; none may alter storage or emit retained events. Do not add protocol error values for them. Absent optional structs contain zeros even for fields whose *present* form requires version 1/nonzero IDs.

### 4.1 Bounded URI validation

Every stored ContentRef remains a UTF-8 URI of at most 2048 bytes. Check task input/output-schema/policy URIs, result URI, and verdict evidence URI. No URL fetch occurs on-chain. Store original bytes; do not percent-decode, normalize, rewrite or hash a URI in place of artifact bytes.

Port the observable acceptance of `modules/domain/records.py:isContentUri` and the schema boundary, including Python 3.12 URL parsing, rather than implementing an ASCII-prefix approximation. The bounded parser must account for scheme/authority, hostname, optional port 1–65535, userinfo, fragments, Unicode whitespace, malformed UTF-8/scalars, bracketed IP literals and the authority delimiter checks performed by `urlsplit`. No DNS lookup or requirement that a CID/hostname currently resolves. Query/path bytes do not gain an interpretation on-chain.

Before the Solidity parser is accepted, add reviewed boundary fixtures for valid HTTPS/IPFS, uppercase scheme, empty fragment/userinfo, IPv6, empty/out-of-range/nonnumeric port, invalid bracket syntax, Unicode authority/path, normalization-sensitive authority delimiters, leading controls, whitespace, and 2048/2049-byte encodings. These expose Python's actual behavior, including permissive edge cases; do not silently strengthen it to match a prose shorthand. Keep parser behavior tied to the pinned Python 3.12 oracle and record its patch/Unicode-data version in differential reports. Any necessary Unicode classification table is a small, provenance-recorded implementation table, not an imported URL runtime. If a discovered baseline parser/prose conflict requires changing acceptance, record it explicitly before changing either baseline or production validation.

## 5. Ordered command behavior

All timestamps use `block.timestamp`, all snapshots/checkpoints `block.number`. Reject values beyond uint64 before downcasting. Creation at block zero rejects `INVALID_RANGE`. No block-count approximation of a deadline.

For ABI-valid calls reproduce L1 order: context range → value rule → cross-command reentrancy guard → target reference → settled/state/award guard → actor → time → command-specific checks. `submitBid` checks a present permit's TaskRef against the resolved offer task **before** task state checks (`UNKNOWN_TASK` on mismatch). Roots and withdrawals have no target-task lookup. `expireTask` accepts any nonterminal status. Public JSON schema failures and narrow helper tests remain separate test surfaces.

| Command and frozen inputs | Authority / ordered behavior | Writes / event |
|---|---|---|
| `createTask(terms)` payable | Any caller; validate terms, retry, exact budget value, namespace capacity | Assign next ID; OPEN root; requester=caller, root=self, parent absent, depth=0, snapshot=block−1; increase deposits/escrow; `TaskCreated` |
| `submitBid(offer, permit, signature)` | OPEN; before biddingClose; resolve identity; direct owner or matching permit; wallet/signature/nonce/expiry; role conflicts; budget; duplicate; derive snapshot and score | Append immutable Bid, update best two, consume permit nonce only on success; `BidAccepted` |
| `allocateTask(taskRef)` | Anyone; OPEN; biddingClose ≤ now < allocationBy | Store Allocation from best two; positive → AWARDED, `TaskAwarded`; otherwise settle UNALLOCATED, `TaskSettled` only |
| `acceptAward(executionRef, ownWorkReserveAtoms)` | AWARDED, awardId=1; frozen executionSigner; now < acceptBy; reserve ≤ reservedAtoms | RUNNING; fix own-work reserve once; `AwardAccepted` |
| `createChildTask(parentRef, terms)` payable | Parent RUNNING; frozen parent executionSigner; now < parent.resultBy; terms → depth/count/envelope/deadline → retry → exact value → namespace capacity | New OPEN child; fresh escrow; increment parent's lifetime/active counts and reserved budgets; `TaskCreated` only |
| `submitResult(executionRef, artifact)` | RUNNING, awardId=1; frozen signer; now < resultBy; activeChildren=0; bounded artifact | Store first ResultCommitment with submittedAt; SUBMITTED; `ResultSubmitted` |
| `settleVerdict(record)` | Anyone; SUBMITTED, awardId=1; now < validationBy; winner/result/policy/validator binding; valid signature, unused nonce, unexpired | Consume verdict nonce and settle SUCCESS/PASS or VALIDATION_FAILED/FAIL atomically; `TaskSettled` |
| `expireTask(taskRef)` | Anyone; not SETTLED; now ≥ status-specific cutoff | Corresponding timeout settlement; `TaskSettled` |
| `cancelTask(taskRef)` | OPEN; requester; now < biddingClose; no admitted bids, including nonpositive bids | CANCELLED settlement; `TaskSettled` |
| `withdrawCredit(receiver, amountAtoms)` | Credit owner is msg.sender; amount > 0; receiver nonzero; sufficient credit; successful value call | Debit caller credit, update aggregates, transfer requested amount; `CreditWithdrawn` |

### 5.1 Creation checks

`validateTerms` order matches L1:

1. PolicyVersion, exact family and validator equal deployment policy → otherwise `UNSUPPORTED_POLICY`.
2. Exact native MON asset → `INVALID_ASSET`; positive uint96 budget → `INVALID_BUDGET`; positive, coprime uint96 alpha terms → `INVALID_ALPHA`.
3. `now < biddingClose < allocationBy < acceptBy < resultBy < validationBy`, and `acceptBy - allocationBy >= 120` → `INVALID_DEADLINES`.
4. Block number nonzero; three valid ContentRefs; maxDepth ≤ 2, maxChildren ≤ 4; a task at its declared maximum depth must have maxChildren=0. Term failures use `INVALID_TERMS` after public range validation.
5. Validator distinct from requester and refundAddress; for a child also distinct from the parent's frozen executionSigner and payout → `VALIDATOR_CONFLICT`.
6. For a child run the envelope checks in §8; then retry validation; `msg.value == budgetAtoms` → `WRONG_VALUE`; `taskCount < 2^64-1` → otherwise `TASK_LIMIT`.

Perform additions such as child deadline + slack in uint256. No truncation before comparison. Creation ID starts at 1 and can reach `2^64-1`; exhaustion blocks further creation, never settlement or withdrawal. New root retry gets its own rootRef; child inherits the same rootRef as its parent. Failed creation rolls back the value, ID and every parent counter.

### 5.2 State, deadline and failure matrix

| From | Normal action before cutoff | Permissionless timeout at equality or later | Counter effect |
|---|---|---|---|
| OPEN | Bids/cancel before biddingClose; allocation in [biddingClose, allocationBy) | ALLOCATION_EXPIRED at allocationBy | NONE |
| AWARDED | Accept before acceptBy | NO_SHOW at acceptBy | FAILURE |
| RUNNING | Child creation/result before resultBy | EXECUTION_TIMEOUT at resultBy | FAILURE |
| SUBMITTED | Verdict before validationBy | VALIDATOR_TIMEOUT at validationBy | NONE |
| SETTLED | Withdrawal is independent of task status | No further task transition | No second observation |

Wrong live state/award → `WRONG_STATE`; wrong actor → `UNAUTHORIZED`; command at/after its exclusive cutoff → `DEADLINE_PASSED`; early allocate/expire → `TOO_EARLY`; any repeated terminal command → `ALREADY_SETTLED`. Result with active children → `CHILDREN_ACTIVE`. Cancellation with any bid → `INVALID_TERMS`. Acceptance reserve above price → `INVALID_RESERVE`.

Never expire automatically while processing another command. A late verdict reverts; a separate `expireTask` transaction settles its refund. There is no reallocation, automatic fallback winner or reopened auction. Failure/retry is a new funded task. No on-chain action follows from an A2A terminal status.

## 6. Immutable bids, top two and exact arithmetic

Resolve `AgentRef.chainId == block.chainid` and `identityRegistry == configured registry` before registry access. Admission never accepts caller-supplied owner, counter, p or score as authoritative. Direct bids have both permit/signature absent and require the current owner as msg.sender. Relayed bids have both present. An approved ERC-721 operator is insufficient.

After identity and permit checks (§7), reject validator-role conflict, then bid > budget (`BID_OVER_BUDGET`), then an existing task/identity bid (`DUPLICATE_AGENT`). Note that the pure `evaluateAuction` fixture runner checks duplicate before budget when validating its list projection; that helper's error precedence is not permission to reorder `submitBid`.

Read the family counters at the task's fixed snapshot. Store the full Bid including `ownerAtBid`, `snapshotBlock`, successes, failures, p, score and the unmodified offer. No edits, withdrawals, bid resubmission or owner-transfer deletion. Losing and nonpositive bids are still admitted, emitted and immutable.

```text
Q = 1_000_000
p = floor(Q * (1 + successes) / (2 + successes + failures))
score = p * alphaDen - Q * alphaNum * bidAtoms
order = descending score, then ascending canonical AgentRef bytes

if no best bid or best.score <= 0:
    UNALLOCATED, winner=null, awardId=0, scores/critical=null, reservedAtoms=0
else:
    secondScore = max(0, runnerUp.score) if runnerUp exists else 0
    criticalAtoms = floor((best.p * alphaDen - secondScore) / (Q * alphaNum))
    reservedAtoms = min(budgetAtoms, criticalAtoms)
    AWARDED, winner=best.agentRef, awardId=1
```

Canonical AgentRef bytes are 32-byte big-endian chain ID + 20 registry bytes + 32-byte big-endian agent ID. Because this market admits only one chain/registry, comparing unsigned agent IDs is equivalent **after** namespace validation. A reusable pure ordering harness must still compare all three fields for cross-namespace golden vectors. Never sort by hash, textual address/ID, insertion order, or score alone. Exact ties retain two distinct bidders with the same score.

Update best two with at most two comparisons: candidate beats best → shift best to second; otherwise candidate beats second/missing second → replace second. Keep topCount 0/1/2; never use an all-zero identity as absence. Store only two IDs and read those bid records at close. Once bidding closes these pointers do not change.

Widen operands before multiplication: `uint256(p) * alphaDen` and `uint256(Q) * alphaNum * bidAtoms`, then cast each bounded product to int256 and subtract. Bounds are below `2^116` and `2^212`, respectively. Compute nonnegative price numerator/denominator in uint256, then integer divide once. With p ≤ Q, critical ≤ alphaDen/alphaNum ≤ uint96 max. Counter additions and probability numerator also widen first. No floats, fixed-point libraries, unchecked narrow multiplication or ceil division.

For every awarded auction: bid ≤ reserved ≤ budget. A strictly positive zero-price award is valid and progresses through acceptance/result/verdict; SUCCESS still increments reputation with paidAtoms=0. Zero winning score is UNALLOCATED.

## 7. Identity, signatures, validator and transfer

### 7.1 ERC-8004 boundary

The minimal production interface is `ownerOf(uint256) returns (address)` and `getAgentWallet(uint256) returns (address)`, both read using STATICCALL. Metadata/Card retrieval remains L2/L4. No proprietary identity minting, no registry-approved-agent allowlist and no reputation registry read in allocation.

Handle no code, reverted/malformed owner/wallet response or nonexistent identity as `IDENTITY_UNAVAILABLE`; a successfully decoded zero verified wallet is `WALLET_UNSET`; a nonzero wallet different from offer.payout is `WALLET_MISMATCH`. Return-data validation must reject dirty address padding and copy only bounded return data. Do not add an arbitrary registry gas cap that narrows admitted identities; an unavailable/malicious registry can prevent new admissions, but cannot block an already admitted obligation's completion. Resolve both values in the transaction, not from an off-chain observation supplied as calldata.

ERC-8004 remains a draft. The deployed implementation must be qualified for ownerOf/getAgentWallet, wallet control verification, operator behavior and wallet clearing after transfer. Record its exact implementation/proxy revision and upgrade authority. Code hash of a proxy alone does not freeze its implementation. [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004).

`profileDigest` commits the exact Agent Card bytes already selected by L2. The contract binds those bytes' digest in the signed offer; it cannot verify card availability, A2A declarations or executionSigner fields inside remote JSON. This separation does not let an unauthenticated caller replace the digest: only the current owner/direct transaction or matching valid owner permit can admit it.

### 7.2 Exact EIP-712 types

Use these exact encodeType strings, with hashes checked against `signing/types.json` and the stored vectors:

```text
EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)
BidPermit(uint64 taskId,address identityRegistry,uint256 agentId,address owner,address executionSigner,address payout,uint96 bidAtoms,bytes32 profileDigest,uint256 nonce,uint64 expiry)
ValidationVerdict(uint64 taskId,address identityRegistry,uint256 agentId,uint8 awardId,bytes32 resultDigest,bytes32 validationPolicyDigest,uint8 verdict,bytes32 evidenceDigest,address validator,uint256 nonce,uint64 expiry)
```

Use `keccak256(abi.encode(TYPEHASH, ordered fields...))` for struct hashes. Domain uses hashed name/version and standard ABI words. Final digest is keccak256 of the fixed 66-byte `0x1901 || domainSeparator || structHash`. There is no packed dynamic encoding, nested reference hash, extra salt, task-terms digest, personal-sign prefix or JSON serialization hash. Hash artifact/card/policy/evidence **exact bytes** using Keccak-256, not SHA3-256.

`verifyBidPermit` after identity reads:

1. Both optional permit/signature present or both absent; mismatch → `INVALID_SIGNATURE`.
2. Direct caller must equal current owner, or signed permit.owner must equal current owner (`OWNER_CHANGED`).
3. Require verified wallet and match payout.
4. Every permit offer field equals offer, including AgentRef, executionSigner, payout, bid and profileDigest (`INVALID_SIGNATURE` on mismatch; TaskRef mismatch was checked earlier).
5. Verify signature → `INVALID_SIGNATURE`; then unused `(owner, BID_TYPEHASH, nonce)` → `NONCE_USED`; then `now < expiry` → `SIGNATURE_EXPIRED`.
6. Continue admission checks and consume nonce only after every guard succeeds.

For a code-free owner require signature length=65, low-s, v=27/28 and nonzero recovered signer equal current owner. For an owner with deployed code use only ERC-1271 STATICCALL with 50,000 gas; require exactly the 32-byte ABI-encoded bytes4 magic `0x1626ba7e` with zero padding, matching L2. Revert, short/extra/malformed response, wrong magic, out-of-gas or stateful verification attempt rejects. Copy at most 32 response bytes, check return size, and never ECDSA-fallback. Use OpenZeppelin's ECDSA primitive rather than reproducing elliptic-curve checks. No compact, ERC-6492 or arbitrary ECDSA fallback paths.

Nonce is a used-value set, not a monotonic sequence. Zero and out-of-order unused values are valid. Same numeric nonce may be used by another signer or primary type; another market has separate storage/domain. No permit nonce is consumed by direct bids, rejected bids or reverted settlement. There is no nonce cancellation API.

### 7.3 Transfer behavior

| When identity changes owner | Required outcome |
|---|---|
| Before old permit consumption | Signed old owner differs → OWNER_CHANGED, even if signature remains cryptographically valid |
| New owner's wallet was cleared | New bid → WALLET_UNSET until registry verification restores it |
| After admission, before allocation | Existing bid/top-two/reputation snapshot unchanged; do not read ownership again at close |
| After award/acceptance | Old admitted executionSigner retains acceptance/result/child authority; new owner does not acquire those permissions |
| Before settlement/withdrawal | Credit still goes to frozen payout; identity counters remain keyed to the same AgentRef |
| NFT returned to old owner | Unused, unexpired old permit can become valid again; v1 has no transfer epoch |

Endpoint/profile changes likewise do not rewrite admitted data. Tests must use actual registry transfer behavior, not merely rename a local signer.

### 7.4 Validator boundary

Pinned validator is an ECDSA-controlled address, including delegation-code accounts; always verify it as EOA, never ERC-1271. Role conflicts at creation/admission remain address-based. Validator key loss causes VALIDATOR_TIMEOUT; no replacement signer for an existing task.

After SUBMITTED/state/time guards, require record.agentRef == awarded winner and resultDigest == stored result (`RESULT_MISMATCH`), policy digest == task's pinned policy (`POLICY_MISMATCH`), validator == configured validator (`INVALID_SIGNATURE`), valid strict ECDSA signature, unused verdict nonce, then unexpired signature. ExecutionRef is fully task/award bound. PASS=1, FAIL=0; other values fail public range validation. Consume nonce and settle in one transaction.

Evidence digest is signed; evidence URI is a bounded locator supplied by the relayer and stored on first valid settlement. URI substitution cannot change the signed digest but can affect retrieval. No contract fetch, evaluator execution, LLM correctness inference or self-reported A2A success. Fixture PASS/FAIL records must use exact stored artifact/policy/evidence digests. Production structured-output-v1 policy/evidence generation belongs to L7 under the existing deployment policy.

## 8. Accounting, delegation, retries and reputation

### 8.1 Settlement primitive

All terminal paths call one internal `settleTask`. It is not externally callable and cannot choose a payment supplied by calldata. With budget B and awarded reserve P (zero without award):

| Reason | Paid / refund | Counter | Required prior state |
|---|---|---|---|
| SUCCESS | P / B−P | SUCCESS | SUBMITTED + valid PASS |
| VALIDATION_FAILED | 0 / B | FAILURE | SUBMITTED + valid FAIL |
| NO_SHOW | 0 / B | FAILURE | AWARDED + timeout |
| EXECUTION_TIMEOUT | 0 / B | FAILURE | RUNNING + timeout |
| VALIDATOR_TIMEOUT | 0 / B | NONE | SUBMITTED + timeout |
| UNALLOCATED | 0 / B | NONE | OPEN + eligible failed allocation |
| ALLOCATION_EXPIRED | 0 / B | NONE | OPEN + timeout |
| CANCELLED | 0 / B | NONE | OPEN + eligible cancellation |

In one transaction: set SETTLED and store the complete receipt; remove B from escrowTotal; add paid/refund to address credits and creditTotal; record the one outcome checkpoint if applicable; update the immediate parent's child counters; emit exactly one TaskSettled. Any failure reverts all changes, including nonce consumption. No external publication/payment/registry call occurs here.

An awarded failure retains winner, frozen payout and reservedAtoms in its receipt even though paidAtoms=0. Submitted validator timeout retains resultDigest but has validation=null. Unawarded receipts have null winner/payout/result/validation and reserve=0. Only PASS/FAIL settlements contain the accepted ValidationRecord, including its signature/evidence locator. settledAt is the transaction timestamp. If payout equals refundAddress, add both deltas; never overwrite one. Skip zero-valued deltas without altering the receipt.

The receipt is permanent even after full withdrawal. `hasReceipt`/SETTLED is the once-only guard for both money and reputation, so a second receipt-deduplication mapping is unnecessary on-chain. The Python projector's duplicate receipt no-op is a different surface from a repeated transaction, which rejects ALREADY_SETTLED.

Conservation, checked after every sequence in tests:

```text
depositedAtoms = escrowTotalAtoms + creditTotalAtoms + withdrawnAtoms
escrowTotalAtoms = sum(nonterminal task budgets)
creditTotalAtoms = sum(address credits)
depositedAtoms = escrowTotalAtoms + sum(receipt.paidAtoms + receipt.refundAtoms)
address(market).balance >= escrowTotalAtoms + creditTotalAtoms
```

Only the test harness enumerates actors/tasks for these sums. Runtime updates aggregates in O(1). Full budgets remain locked until settlement; allocation does not credit the budget surplus. Forced native donations are excess balance, not deposits/credits, and have no sweep function. No reward/fee deduction, cost reimbursement, child-payment netting or automatic reuse of credits as msg.value.

### 8.2 Child envelopes

For a RUNNING parent:

```text
available = reservedAtoms - ownWorkReserveAtoms
            - reservedChildBudgets - committedChildPayouts
```

Check child constraints in L1 order:

1. `parent.depth + 1 <= child.maxDepth <= parent.maxDepth` → DEPTH_LIMIT.
2. `child.maxChildren <= parent.maxChildren` and `parent.childrenCreated < parent.maxChildren` → CHILD_LIMIT.
3. Child budget ≤ available → ENVELOPE_EXCEEDED.
4. `uint256(child.validationBy) + 120 <= parent.resultBy` → CHILD_DEADLINE.

Depth limits are absolute from root, not a fresh per-child allowance. Task terms at their max depth require zero children. Parent signer supplies **new MON** exactly equal to child budget; parent escrow/reserve is not advanced, borrowed or transferred. Parent own-work reserve is fixed at acceptance and cannot be decreased later.

On creation: reservedChildBudgets += child B; activeChildren += 1; childrenCreated += 1. On the child's first terminal settlement: reservedChildBudgets -= B; committedChildPayouts += actual child paid; activeChildren -= 1. childrenCreated never decreases. Failed/unallocated children release budget capacity, successful children retain their actual payment as lifetime consumption and release only unused budget capacity.

Only update the direct parent, even if it is already SETTLED. Never recurse, settle siblings, revoke child credits, or block a parent timeout on active children. Parent result submission requires activeChildren=0; parent expiration does not. Late child settlement after parent timeout remains valid according to that child's deadlines/state. With valid nested deadlines, every child's validationBy is already past when its parent reaches resultBy: a still-active child's late settlement is therefore an expiration, not a newly accepted PASS/FAIL. A completed child can remain SUBMITTED past its validation deadline until someone calls expireTask; the parent still regards it as active until settlement.

### 8.3 Retry links

`terms.retryOf` references a known SETTLED non-SUCCESS task requested by this caller. Root retry must reference a root; child retry must reference the same parent and root. Otherwise INVALID_RETRY. A retry is a normal newly funded task with new ID, snapshot, deadlines and auction; it consumes another lifetime child slot and current envelope capacity. Do not copy the old winner, refund, reputation, nonce, or signature. There is no special retry command or requirement that only one retry can reference a failed task; ordinary funding/envelope/lifetime guards apply.

### 8.4 Reputation checkpoints

Every identity starts lazily with Beta(1,1) in each family. Counters count canonical market outcomes, not ERC-8004 feedback and not validator-confidence scores. At task creation store block−1. At bid admission binary-search the last checkpoint with blockNumber ≤ snapshot; absent history yields (0,0). All settlements in the task's creation block are excluded regardless of transaction order.

Each counted settlement increments success or failure once and writes cumulative counters at the current block. Multiple outcomes for the same identity/family in one block overwrite that last checkpoint with the new cumulative counters. Next-block tasks see the final cumulative values. Uncounted terminal outcomes append no checkpoint. Transfer changes neither the history key nor earlier snapshots.

Use uint256 intermediates for `Q*(1+s)/(2+s+f)`, then narrow the proven [0,Q] result. At most `taskCount <= 2^64-1` observations exist market-wide, so uint64 counters cannot overflow under reachable state. Creation capacity prevents future overflow without saturation or blocking a valid terminal settlement. Test the ceiling through a harness, including a final success at the counter maximum. No external method seeds or edits counters.

Canonical ReputationEvidence is derived from TaskSpec + receipt (receiptRef, winner, family, counter effect, reason, policy/result/evidence digests). Emit the existing TaskSettled receipt; do not invent a second reputation event or call giveFeedback. L7 can read that receipt and export after canonical settlement. An unavailable feedback registry cannot affect L3 finalization.

## 9. Events, ABI conformance and replay

Use the seven frozen event signatures, each with **one nonindexed `data` tuple** containing schemaVersion=1, policyVersion=1 and its payload. No extra indexed task IDs, omitted nested fields, renamed components or parallel event vocabulary. All ten successful commands emit exactly one protocol event; failure emits none. Constructor/read operations emit none.

| Event | Payload / reconstructable effects |
|---|---|
| TaskCreated | Full TaskSpec; initial escrow, ancestry, retry, snapshot and parent envelope reservation |
| BidAccepted | Full Bid + nullable permitNonce; immutable offer, owner/counters/p/score and used bid nonce |
| TaskAwarded | Full positive Allocation; winner, runner-up threshold, critical/capped price |
| AwardAccepted | ExecutionRef, ownWorkReserveAtoms, acceptedAt; RUNNING and envelope floor |
| ResultSubmitted | Full ResultCommitment; artifact and SUBMITTED status |
| TaskSettled | Full SettlementReceipt; credits/refund, outcome, nonce, validation, reputation effect and parent reservation release |
| CreditWithdrawn | owner, receiver, amountAtoms; debit and paid native credit |

UNALLOCATED emits no TaskAwarded. Its TaskSettled plus preceding bids/terms determine its failed Allocation; retain allocatedAt internally for readTask. Creation and result/settlement timestamps come from the actual transaction block. No transactionHash/blockHash is invented inside these structs: the RPC receipt supplies `EventEnvelope` provenance.

Extend `check_abi.py` with a compiled-ABI comparison: remove only compiler `internalType` metadata, compare named fields/order/types, mutability, returns, event indexing and error codes against the frozen artifact. Allow only the constructor and explicitly specified read functions as additions. Reject unexpected external writes/events; do not update the frozen ABI from compilation as the expected value. Derive selectors/topics independently and verify decoded event bytes against canonical JSON schema objects.

A test-only event replay consumer folds actual chain logs into observable task/credit/counter state and compares getters and L1. Deduplicate chainId/market/blockHash/logIndex; replay overlap must not credit twice. Anvil snapshot/revert tests demonstrate that orphaned logs are not canonical. This bounded integration consumer is not an L4 indexer, database or watcher. Conflicting finalized history and unavailable finality must stop the testnet driver; no latest-block fallback.

## 10. Security and gas bounds

All writes enter the same storage guard before external calls. Use a small explicit guard returning ProtocolError(REENTRANCY); do not expose a library-specific guard error in place of the frozen code. State/value prechecks retain §5 ordering. STATICCALL does not replace the guard.

Withdraw with checks-effects-interactions: validate, subtract credit and update aggregates, then CALL receiver with the requested value and empty calldata. Forward available gas rather than a 2300 stipend. Ignore return payload without allocating unbounded returndata. Failure reverts with TRANSFER_FAILED and restores the credit/aggregates; success emits CreditWithdrawn. A receiver may catch a rejected nested call and let the outer withdrawal succeed once. Receiver hostility never blocks settlement because settlement does not call it. Unknown-selector/empty-data native transfers revert; forced transfers remain possible and are accounted as excess.

Read-only callbacks during withdrawal observe the already debited, internally consistent ledger. Registry/ERC-1271 callbacks cannot write through STATICCALL and cannot bypass the cross-command guard. No tx.origin authentication, arbitrary delegatecall, unchecked external returndata decode, arbitrary token call or approval-based payout diversion.

| Operation | Work bound |
|---|---|
| Creation | Fixed fields; three URI checks of ≤2048 bytes; fixed-width Euclidean gcd; at most one parent/retry lookup |
| Bid | Two registry reads; at most one capped ERC-1271 call; one checkpoint binary search; at most two ranking comparisons |
| Allocation | At most two stored bids; no registry call, bidder scan or signature recheck |
| Acceptance/result | Fixed records/guards; one bounded URI for result; no child enumeration |
| Settlement/expiry/cancel | One receipt, ≤2 credit accounts, ≤1 checkpoint append/overwrite, ≤1 direct parent update |
| Withdrawal | One credit owner, one bounded-output value call |
| Views | One task/bid/receipt or checkpoint binary search; no unbounded returned arrays |

Checkpoint binary search is logarithmic in ≤2^64−1 observations (≤64 search steps). Namespace checking reduces identity comparison to uint256 IDs. URI loops are byte-bounded; signatures are ≤4096 bytes. Do not introduce a bidder-count cap or shorten public URI limits to rescue gas usage.

Measure maximum-size valid creation/result/verdict, first/subsequent bid, cold/warm settlement and 1271 paths under the pinned Monad execution profile. Compare closure with 1, 2, 16, 256 and 1024 admitted bids; allocation's storage-access count and control-flow bound must not grow with count. Compare child settlement for terminal/nonterminal parents and different tree sizes. Synthetic setup may be outside measured gas; measured calls must use production entrypoints.

The deployed runtime/initcode must fit the **verified target revision's** code-size and transaction limits; do not override Anvil/Forge limits to force a pass. Current documentation distinguishes Monad code-size/gas behavior from Ethereum, and fees can depend on gas limit. Qualify limits and estimate conservatively before broadcast; do not present an Ethereum gasUsed calculation as the sender's Monad fee. [Monad differences](https://docs.monad.xyz/developer-essentials/differences), [deployment limits](https://docs.monad.xyz/developer-essentials/summary). A change of target hardfork requires rerunning these checks, not changing market economics.

## 11. File ownership and implementation sequence

Final file ownership follows the original sequence with the small consolidations below. Contract-local tests stay under contracts; cross-runtime/process tests stay under tests. No new app or long-running service was introduced.

| File/path | Ownership |
|---|---|
| `foundry.toml`, `contracts/toolchain.lock.json`, ignored `.scratch/layer3/toolchain/lib/` dependencies | Toolchain/profile/remapping/lock; generated out/cache ignored |
| `contracts/src/ProtocolTypes.sol` | Single Solidity wire structs, numeric catalog constants, nullable wrappers |
| `contracts/src/IAgentLanceMarket.sol` | Exact ten commands/seven events/ProtocolError; separate view declarations reuse the same types |
| `contracts/src/ProtocolMath.sol` | Shared production score/order/price and probability arithmetic; checkpoint reads/writes remain with market storage |
| `contracts/src/ProtocolSignatures.sol` | Exact type/domain hashes, strict ECDSA and bounded ERC-1271 |
| `contracts/src/ContentUri.sol` | Bounded URI/UTF-8 validation matching L1 |
| `contracts/src/AgentLanceMarket.sol` | Storage, bounded ownerOf/getAgentWallet reads, checkpoints, commands, accounting, delegation, receipts, events and views |
| `contracts/src/IAgentLanceViews.sol`, `scripts/generate_contract_types.py` | Separate generated read interface and deterministic schema/catalog-to-Solidity declarations |
| `contracts/test/MarketProperties.t.sol`, `ReputationCheckpoints.t.sol` | Production ranking/signature properties, checkpoint boundaries and probability fuzz tests |
| `contracts/test/MarketLifecycle.t.sol` | Lifecycle and exact event/error tests |
| `contracts/test/Signatures.t.sol` | Signing vectors, registry/ownership and malicious signer tests |
| `contracts/test/AccountingDelegation.t.sol` | Escrow, child/retry and hostile withdrawal tests |
| `contracts/test/MarketInvariants.t.sol`, `MarketBounds.t.sol` | Stateful invariant handler, bounded storage/gas measurements and maximum wire records |
| `contracts/test/MarketLayerOne.t.sol`, `MarketSecurity.t.sol`, `ContentUri.t.sol` | Original funded trees, adversarial boundaries and URI conformance |
| `contracts/test/support/` | Shared fixture loader, test-only harness, registry/1271/receiver test doubles; no production seeding functions |
| `contracts/script/DeployMarket.s.sol` | Deploy only the market from qualified registry/validator inputs; no publisher |
| `tests/conformance/test_layer3.py`, `tests/layer3_support.py` | Frozen-vector consumption, RPC codec, independent Python/Solidity comparisons |
| `tests/integration/test_layer3_transactions.py`, `tests/conformance/test_layer3_blocks.py`, `test_layer3_replay.py`, `test_layer3_uri.py` | Local lifecycles/rollback, same-block ordering, independent event reconstruction and Python/EVM URI comparisons |
| `scripts/layer3_testnet.py`, `layer3_deployment.py`, `layer3_tools.py` and their tests | Qualified live runner, evidence validation and isolated checksummed tool installation |
| `tests/integration/test_layer3_live_driver.py`, `tests/test_layer3_testnet.py` | Four signed local runs of the live-driver scenarios and configuration/finality failure tests |
| `specs/fixtures/layer-3.json`, `specs/acceptance-layer-3.json` | Reviewed new regression data and exact executed-test mapping; references existing fixture IDs |
| `specs/contracts.read.abi.json` | Reviewed view ABI only; canonical record tuples reused |
| `scripts/check_layer3.py`, `scripts/demo_layer3.py` | Offline gate and explicit local/testnet scenario runner; reusable RPC mechanics stay in test support |
| `scripts/check_abi.py`, `scripts/check_layer2.py`, Makefile, README | Exact compiled ABI checks, isolated lower-layer test selection and documented commands |
| `deployments/monad-testnet/` | Only actual verified L3 report/evidence and, when possible, complete canonical manifest |

Sequence and completion evidence:

1. **Compatibility harness first.** Implement wire types, exact ABI checks, fixture inventory, numeric/error mappings and view ABI. Add the nonpayable-boundary regression and expose rather than conceal its mismatch. Verify maximum wire-record encoding and deployed code size early.
2. **Pure math and checkpoints.** Consume all market/reputation vectors, including max ranges, zero-price, ties and same-block histories. Use the same production library functions through test harnesses.
3. **Input/identity/signing.** Implement URI validation and exact type hashes/EOA/1271 checks. Verify original signing vectors and transfer/nonce failures before monetary entrypoints depend on them.
4. **Funded roots and bids.** Creation, bid admission and top two, allocation, acceptance, read views and exact events. Keep settlement internal with one planned implementation, not temporary dummy receipts.
5. **Complete settlement.** Result/verdict, all expiry/cancel paths, atomic counters/credits, withdrawal/reentrancy. Execute solo lifecycle and every terminal accounting vector.
6. **Children and retry.** Add parent envelope/depth/lifetime/deadline checks and O(1) terminal parent updates; run all delegation and full-tree regressions.
7. **Cross-runtime conformance.** Differential full-command sequences, fixed vectors, adversarial transactions, event reconstruction, gas tests and invariant campaigns.
8. **Local gate then qualified testnet.** Run all lower-layer checks, Anvil solo/tree/timeout/malicious cases; collect genuine testnet receipts and L3 deployment evidence when credentials/funding/registry facts are available. Record unrun live items as pending.

No production seeding of p/counters, unrestricted settlement, fabricated validator verdict, or alternate fixture market may survive into the deployed bytecode.

## 12. Deployment qualification and manifest

### 12.1 Facts required before broadcast

Target only the official Monad **testnet**. Obtain network chain ID, genesis hash, active execution revision and RPC endpoints from current official network information and verify RPC answers. Do not hardcode an unverified registry address from a fixture, social post or remembered deployment. [Official testnet information](https://docs.monad.xyz/developer-essentials/testnet).

Deployment checks must establish:

- Identity registry address/code hash, exact source version, implementation and proxy/admin details; probe ownerOf/getAgentWallet against controlled registered identities. Demonstrate transfer clearing, restored wallet control and contract-owner behavior on the selected implementation.
- Validator address control using a fresh domain-bound challenge; the private key remains outside source/manifests. Verify it is separate from demonstration requester/owner/signer/payout roles. Code presence alone neither establishes nor rejects ECDSA key control.
- Reproducible build: source commit/dirty status, dependency revisions, compiler/settings, execution profile, constructor arguments, creation bytecode and runtime hash. Verify runtime including constructor immutables and compare deployed code to the build.
- Native MON funding for caller value plus fees. No wrap/approval step. Broadcasting requires explicit deployment authorization, real funded accounts and the qualified registry; local fixture keys must never be funded on testnet.
- RPC supports finalized block queries, receipt/block-hash verification and the historical reads needed by the driver. Snapshot counters themselves use contract checkpoints and do not require historical registry RPC reads during execution.

After a successful deployment receipt, record its transaction/block/hash, actual market address and runtime code hash; verify readPolicy. Wait for a matching canonical block at or below the finalized height. Source verification should publish the exact constructor/settings when explorer infrastructure supports it; inability to verify code independently blocks qualification even if an explorer UI is unavailable.

The frozen client finality rule stays `FINALIZED`: request `eth_getBlockByNumber("finalized", false)`, verify the transaction's canonical block hash, then read relevant state at that finalized block. Receipts may precede finality. Unsupported/stale/error RPC means WAIT/ABSTAIN; no fixed number of blocks or `latest` substitute. Poll no faster than every 2 seconds, bound each request to 10 seconds, stop the action at its task deadline. [Monad RPC block states](https://docs.monad.xyz/reference/json-rpc/overview).

### 12.2 L3 report versus complete canonical manifest

Use `deployments/monad-testnet/<market>/layer-3-verification.json` as an operational evidence report only after its fields are verified. It is not consumed as `DeploymentManifest` by L2. During implementation give this report a closed test/deployment schema with these required top-level fields:

| Field | Exact representation |
|---|---|
| `reportVersion`, `layer` | Integer 1, string `L3` |
| `manifestFacts` | Closed subset of DeploymentManifest listed below, with identical field types/constraints |
| `genesisHash`, `deploymentTxHash` | Canonical 32-byte lowercase digests |
| `executionRevision` | Nonempty observed revision string, ≤128 characters |
| `build` | Closed object: sourceCommit (40 lowercase hex), sourceTreeDigest (Digest), solcVersion/foundryVersion/openZeppelinRevision/forgeStdRevision (nonempty strings ≤128), evmVersion=`paris`, optimizerRuns=200, viaIR=true, creationCodeHash (Digest), storageLayoutDigest (Digest) |
| `identityVerification`, `rpcVerification`, `validatorVerification`, `scenarioEvidence` | ContentRefs to exact retained verification bytes; locally staged report may use separately stored files until published, but a verified published report requires valid canonical URIs |
| `pendingManifestFields` | Exactly `["reputationRegistry", "reputationCodeHash", "reputationVersion", "feedbackPublisher", "publisherCodeHash", "registryVerification"]` when these have not all been qualified |

`manifestFacts` required keys: schemaVersion, policyVersion, network, chainId, market, marketCodeHash, deploymentBlock, deploymentBlockHash, identityRegistry, identityCodeHash, identityVersion, validator, asset, rpcUrls, confirmation, minimumAcceptanceWindow, synthesisSlack, maxDepth, maxChildren, profileUri. These retain their frozen schema definitions; RPC list must contain at least one tested endpoint for qualification. Report verification refs are evidence for the corresponding eventual manifest fields; the final registryVerification must additionally cover reputation/publisher integration. Retain proxy implementation/admin data and exact probes inside identityVerification, not invented top-level protocol fields.

If all full-manifest dependencies are already independently deployed and verifiable, validate and publish the existing complete `DeploymentManifest` instead, with the L3 report retained as supporting evidence. Otherwise keep the six fields pending exactly as above. Do not substitute zero addresses, identity registry for reputation registry, market address for publisher, nulls, or a fixture verifier. L7 supplies/verifies the publisher and final registry report. Until then, L3 chain tests can pass, but a full deployment manifest and live L2 integration requiring it are not claimed complete.

An external registry upgrade changes a trust dependency, not market policy. Verification tooling must flag unexpected implementation/admin/code changes and stop initiating new bids. No market admin pause is added: admitted settlements must continue without fresh registry checks. Store public facts only; RPC secrets, keys, keystore passwords and credentials stay out of reports.

## 13. Tests and independent differential conformance

### 13.1 Fixed authoritative data

Every test reports a stable scenario ID. `acceptance-layer-3.json` maps L3 requirements to full pytest parameter IDs and Foundry test names. Missing/duplicate/skipped cases fail the offline gate; do not treat one passed parameter as proof that the whole test family ran.

| Existing fixtures | L3 execution surface |
|---|---|
| `market.json` (all 18) | Production math through harness; compare exact winner, scores, critical/reserve and rejection; test all relevant insertion permutations |
| `reputation.json` (all 15) | Pure probability/checkpoint harness plus real transaction snapshot/transfer/capacity tests |
| `lifecycle.json` (all 34) | Actual commands where ABI-encodable; guard harness for reference projections; explicit nonpayable mismatch from §1.1 |
| `delegation.json` (all 17) | Envelope helper projections plus real funded linked tasks; no fake public settleChild API |
| `layer-1.json` | Alias-credit, ceiling, nonce, late-child, tree-success and tree-parent-timeout as Solidity behavior; analytics cases remain lower-layer regression tests |
| `signatures.json` | Original hashes/signatures/recovered signers through Solidity independently; every negative domain/task/agent/result/type/nonce/expiry/owner case |
| `replay.json` | On-chain identity transfer, wallet, operator, role conflict and ERC-1271 cases; duplicate logs/reorg via test replay consumer; A2A/export cases stay L2/L4/L7 |
| `objects.json` | Exact wire/nullable/ABI/event round trips; examples remain synthetic |
| `validation.json`, `a2a.json`, L2 fixtures | Lower-layer checks and boundary assertions only; no claim to implement evaluator/A2A/export in Solidity |

Follow [L1 fixture projection rules](layer-1.md#9-fixture-consumption-and-known-projection-differences). Synthetic p=0/Q and historical ceiling counters are pure helper tests, not permission to submit p or seed production state. `child-permissions-cannot-expand` is a helper DEPTH_LIMIT case; the public schema's out-of-range case remains INVALID_RANGE. Duplicate receipt reduction is a projector no-op; duplicate on-chain settlement is ALREADY_SETTLED. Preserve each original expected value.

### 13.2 Additional reviewed regressions

Add explicit initial/action/expected/event-or-error data before production implementation for:

- ABI absent wrappers with nonzero hidden fields, dirty enum/address data, zero digest/agent ID, wrong namespace, bad versions, signed int bounds, URI boundaries (§4.1), invalid/noncoprime alpha and exact deadline equality.
- All three insertion positions for top two, identical scores, nonpositive runner-up, huge negative score, one bidder, budget cap, integer floor, zero-price SUCCESS, namespace exhaustion and maximum counter arithmetic.
- Invalid signature combined with used nonce/expired verdict (L1 precedence); mismatched permit/offer; stale owner; owner transfer back; current owner with cleared/restored wallet; transfer after admission/award/acceptance; no registry reads during allocation/settlement.
- EOA high-s, bad-v, compact/wrong length/zero recovery; ERC-1271 magic/revert/short/extra/dirty return, gas exhaustion, huge return bomb, attempted callback writes; pinned validator never uses 1271, including a delegation-code validator address.
- Every terminal state with exact escrow/credit/counter/checkpoint effects; same payout/refund account; zero paid amount; duplicate settlement and failed verdict nonce rollback; insufficient/zero withdrawal and hostile receiver rollback/reentry.
- Full-funded child, exact/over envelope, own-work reserve, successful child retaining paid capacity, failure freeing budget, child retries consuming lifetime count, max depth/count, deadline slack equality/one-over, successful retry forbidden, same-parent/root/requester binding and fresh-value failure rollback.
- Parent failure after child SUCCESS, parent expiry with active child, late child expiration updating terminal parent once, late PASS/FAIL rejected by the child's deadline, depth-two settlement affecting only immediate parent, and independent root retry.
- Same-block settlement before/after creation, two outcomes in one block, next-block snapshot, atomic rollback on failed external call and forced native balance excess.

Do not invent reachable paths for reserved catalog codes. For example `INVALID_PARENT` is catalogued but the current L1 parent lookup/state/actor paths use UNKNOWN_TASK/WRONG_STATE/ALREADY_SETTLED/UNAUTHORIZED. Keep the numeric constant; do not replace those tested errors to exercise it.

### 13.3 Differential runner

Use the real Python `applyCommand`/math/checkpoint functions as one implementation and deployed Solidity as the other. Tests must not use Python outputs to populate Solidity storage or use Solidity outputs to construct the next expected state. A shared scenario supplies **inputs/context only**; both implementations independently advance their own state.

For every command compare: success/error class and exact custom code where applicable; full TaskSpec/Bid/Allocation/result/receipt; all affected credits and aggregate conservation; parent's four envelope fields; latest and snapshot counters; nonce consumption; complete ordered decoded event payloads. Compare untouched known accounts/tasks as well. On rejection compare pre/post state, credits, nonces and logs for both implementations. A receipt status alone is insufficient to prove a specific revert code: use a deterministic eth_call/trace against matching prestate or Foundry expectRevert.

Use actual mined block number/timestamp for the Python CommandContext. Control Anvil automining and block timestamps for deadline/same-block tests; preserve transaction order. Supply identity/signature facts to L1 from independently prepared test registry/key state, not from the Solidity market result. Maintain pre-admission frozen actor fields in both implementations.

For original fixed signing vectors, use a pure Solidity harness with the vector's explicit domain values to compare typeHash/domainSeparator/structHash/digest/recovery unchanged. For live-market command sequences, Python signs fresh messages using `types.json` with the actual Anvil chain/market. Do not rewrite static signatures to fit a deployment. For symbolic fixture actors/task IDs use a documented one-to-one input mapping and inverse-normalize output references only; never normalize amounts, p, scores, status, effects, errors or hashes. Original golden expected files remain byte-for-byte unchanged.

Run deterministic fixed cases plus seeded generated sequences containing valid and invalid commands, transfer/wallet changes, block advances and withdrawals. Defaults: 256 math/signature fuzz runs per property; 64 invariant runs of depth 128; 32 differential seeds of 64 actions. Report seeds and persist minimized failures as reviewed regression cases. The stateful handler must count successful transitions and reached terminal reasons; a campaign where every call reverts is a failure. Include all deterministic terminal scenarios regardless of random reachability.

Invariants: exact best-two equivalence to a test-only full sort; winner/payment bounds and score positivity; immutable admitted records; namespace/authority isolation; atomic nonce consumption; money conservation; receipt/counter once-only relation; snapshot exclusion; nonnegative child capacity; active≤lifetime≤limit; child sums match immediate-parent totals; no terminal-parent reversal; and external recipients/signers cannot gain another task's funds or authority.

### 13.4 Gate commands

During implementation add:

```sh
make check-l3       # L0/L1/L2 gates, compiled ABI, Forge, differential/integration, coverage/report
make demo-l3        # starts an isolated Monad Anvil; local funded scenarios; cleans up its process
```

`check_layer3.py` must run existing `check_layer2.py` (which includes L0/L1), `forge fmt --check`, a locked optimized build and bytecode-size check, Foundry unit/fuzz/invariant suite, compiled ABI checks, pytest conformance/integration and a coverage report. Run applicable Python Ruff checks for new tooling. Collect reports in ignored `.scratch/layer3/`. No live RPC/credential requirement for the offline gate; fail clearly on missing pinned Foundry rather than skip Solidity tests.

Require every production command/terminal/authority/deadline/replay branch in the acceptance map. Record Solidity line/branch/function coverage and enumerate any unreachable defensive branches; do not claim numerical 100% from a subset of files or count generated types/test mocks as protocol coverage. Source coverage supplements, rather than replaces, independent golden assertions. No deployment readiness label is generated by the offline gate.

## 14. Executable local and testnet scenarios

### 14.1 Local process flow

The demo runner starts a fresh `anvil --network monad --chain-id 31337` with the pinned hardfork on loopback, deploys the exact market build and a clearly labelled ERC-8004 test double, registers distinct controlled identities/wallets, then submits real funded transactions using the frozen ABI. Temporary state/keys/logs go in a unique ignored directory. Teardown stops only the node it started. No L1 fixture HTTP market acts as authority.

The Python driver is the second process. It uses existing HTTPX/eth-account/ABI tooling, verified test keys and static result/evidence bytes. No A2A process, worker, LLM or validator service is needed to prove the chain layer. It prints transaction IDs, canonical event/receipt objects, balances, counters and scenario assertions, never private keys. Caller, owner, executionSigner, payout, requester, relayer and validator must be distinguishable test actors.

Fixture amounts below are **atoms**, intentionally small. Gas balances are separate. Use a fresh identity per case unless history is the subject of the case. Relative deadlines are measured from a freshly read base timestamp T, and all transactions must actually fit them.

### 14.2 Solo SUCCESS

Terms: B=100, alpha=1/100, no delegation, deadlines T+30, T+60, T+180, T+300, T+420. Identity prior=(0,0); bid=20; valid owner permit relayed by another account; payout is registry-verified wallet.

1. Fund createTask with 100. EscrowTotal=100, creditTotal=0; store snapshot=creationBlock−1.
2. Submit signed bid: p=500000, score=30000000. Transfer identity after admission in a variant and confirm the bid persists.
3. At biddingClose an unrelated caller allocates: winner fixed, secondScore=0, critical=50, reserve=50. Escrow still100.
4. Frozen signer accepts with ownWorkReserve=50; commits the exact fixture result digest before resultBy.
5. Relayer submits pinned validator PASS with exact bindings before validationBy and expiry. Receipt SUCCESS paid50/refund50, escrow0, credits50 to payout and50 to refundAddress, success counter+1, one checkpoint/receipt.
6. Duplicate verdict rejects ALREADY_SETTLED. Both credit owners withdraw to chosen receivers; credits0, withdrawn100; all aggregate invariants hold. NFT's new owner receives no old payout authority.

### 14.3 Parent/child SUCCESS and independent funding

Parent: B=200, alpha=1/200, prior p=500000, bid20 → score80000000, reserve100. Deadlines T+30, T+60, T+180, T+1200, T+1320. Accept ownWorkReserve20, yielding child capacity80.

Before parent.resultBy, its frozen signer publishes a child with **fresh value60**, refundAddress set to that signer, alpha=1/80, distinct fresh child identity bid10. Use child deadlines T+210, T+240, T+360, T+600, T+720; child.maxDepth=1/maxChildren=0, parent.maxDepth=1/maxChildren≥1. Slack test:720+120≤1200. Parent may have been accepted earlier; do not start this scenario after the child's bidding window.

Expected sequence:

- After child creation deposits=260, escrow=260; parent fields lifetime1, active1, reservedChildren60, committedPaid0, available20. Parent escrow remains200.
- Child allocation: score30000000, second0, critical40, reserve40. Child accepts/commits; PASS pays child wallet40 and credits manager refund20.
- Parent fields lifetime1, active0, reservedChildren0, committedPaid40, available40. Parent result now allowed.
- Parent PASS credits manager payout100 and root refund100. Across receipts paid140/refunds120; all260 atoms credited, then independently withdrawable. Manager's own fresh child funding is not reimbursed by an extra transfer.

Variant: after child SUCCESS, let parent reach resultBy without submission and expire it. Child paid40 remains; parent refund200, child refund20; parent failure counter+1, child success+1. Another variant leaves child active, expires parent, then expires the child according to its own state; terminal parent's envelope updates once. A late verdict is rejected because the child's validation deadline precedes the parent's result deadline. Never wait for a child to auto-expire.

### 14.4 Timeout, refund and malicious actors

Run separate roots for: no bids UNALLOCATED before allocationBy; missed allocation; no-show; execution timeout; valid FAIL; validator timeout; requester cancellation. Compare the exact table in §8 and withdraw each refund. At each cutoff test an ordinary late action rejected and permissionless expiration accepted. Use L0 deadline fixtures for equality, not only approximate waits.

Demonstrate a wrong-chain/wrong-market permit, old-owner permit, unauthorized signer, wrong result/policy, high-s validator and invalid-1271 owner cannot change task state/credits/counters/nonces. Include one valid contract-owner permit and one valid direct-owner bid. Allocate/expire using an unrelated caller. Set a payout or refund recipient to a reverting contract: settlement succeeds into its credit; a reverting withdrawal restores that credit; withdrawal to an alternate permitted receiver succeeds. Reentrant receiver attempts every market write; no second credit or settlement occurs.

### 14.5 Monad testnet flow

After qualification and explicit deployment authorization, use `scripts/demo_layer3.py --network monad-testnet --report <verified-report> --scenario solo|parent-child|timeouts|malicious`. The implementation must support these named scenarios and emit a verification artifact per run. Load keys from local keystore/secret configuration; no private-key CLI argument or tracked `.env`.

Use actual qualified registry identities and verified wallets, not testnet-deployed substitutes labelled ERC-8004. Use the same market bytecode/settings as the local gate. Relative windows may be lengthened before creation to allow real confirmations; keep every ordering/minimum/slack invariant. Wait for real block timestamps/finality; never use evm_warp/test RPCs on testnet. Testnet timeouts may take minutes. Artifact/policy/evidence bytes remain clearly labelled fixture material; signing them does not claim a production validator exists.

Record successful funded solo and parent/child receipts, refund/timeout, wrong signer and ERC-1271 evidence, withdrawals, counters at snapshot/current heights and exact code/config hashes. Run exhaustive deadline/receiver adversarial permutations locally; the live report must distinguish which representative cases actually ran on Monad. A broadcast hash without finalized receipt/state checks is not acceptance. Lack of funding, a verified registry, key access or RPC finality support is a concrete pending live gate, never a simulated pass.

## 15. Objective exit gate and handoff

| Requirement | Required evidence |
|---|---|
| L3-01 | All ten real entrypoints; all eight terminal reasons; acceptance/result/timeout/cancel/retry deadlines and exact receipt/event checks |
| L3-02 | Qualified registry namespace; direct-owner, EOA permit and ERC-1271 paths; owner/wallet/profile bindings and malicious signer rejection |
| L3-03 | Immutable one-bid admission; exact snapshot/counters/p/score; duplicate/late/over-budget rejection; no owner-transfer deletion |
| L3-04 | All market goldens + insertion-order fuzz; constant-size top-two close; exact ties/floor/zero/cap/max arithmetic |
| L3-05 | Historical checkpoints, same-block exclusion/overwrite, next-block visibility, counter ceiling and transfer preservation |
| L3-06 | Atomic receipt/counter/credit/nonce updates; no double settlement; conservation and hostile receiver/reentrancy tests |
| L3-07 | Real fresh child funding; all 17 delegation cases; full tree/retry/lifetime/depth/slack tests; late child updates terminal parent without recursion |
| L3-08 | PASS/FAIL/timeout via exact pinned verdict; artifact/policy/evidence binding; frozen TaskSettled sufficient for later export; no synchronous export |
| L3-09 | Immutable deployable build/config, exact ABI, verified chain/registry/code/finality facts, funded solo+child testnet evidence and pending manifest dependencies honestly recorded |

The implementation handoff must contain:

1. Executed offline gate report with full fixture/test IDs, tool versions, random seeds, gas/size/coverage results and no skipped required scenarios.
2. Independent Python↔Solidity equality for all ABI-valid economic transitions; explicit report of the frozen nonpayable rejection-byte mismatch and the verdict guard-order discrepancy in §1.1. Strict error-byte parity cannot be called complete while the former remains unaccepted; do not bury it in a green aggregate count.
3. Local Anvil solo/tree/timeout/malicious reports using real transactions and exact events, with conservation after withdrawals.
4. Actual Monad testnet deployment and funded scenario evidence, or a precise “offline implementation complete; live gate pending” status with missing deployment facts. Source/test completion is not the full L3 exit gate without live evidence.
5. A verified L3 report and either the complete canonical manifest or its explicit L7-dependent pending fields. Do not claim complete live AgentLance compatibility from a report that L2 cannot load as DeploymentManifest.
6. Review confirming no economic rule, L0/L1 expected fixture, L2 port contract, signature type, event tuple, authority or lifetime limit was changed to obtain a pass; no L4–L8 implementation added.

This specification is complete when the above implementation work is unambiguous, including the explicitly identified baseline compatibility decisions. **L3 itself is complete only after the executable and live gates pass and any claimed exact-parity exceptions are explicitly resolved.** Executed implementation evidence is recorded below. No live deployment or testnet address is claimed.

## 16. Executed implementation acceptance (2026-10-07)

The implementation reuses the unchanged Python domain/core as its independent oracle. It adds the immutable Solidity market, eight read views, generated wire declarations, pinned build/deployment tooling, real local transactions and a qualified testnet scenario driver. Probability is computed from checkpoints; external callers cannot supply it. Complete task/bid/result/receipt records and the seven frozen event tuples remain available. No L4 adapter, production evaluator, feedback publisher or UI was added.

The implementation builds on source commit `e1dbe636ba6fd08a15c70bd9c8d05f9e25f65c4f`. The local `gate.json` records the exact production source-tree digest, compiler metadata and storage-layout digest. These are build evidence, not a live deployment manifest. The original protocol ABI, catalog, signing types, lower-layer implementation and golden expected data remain unchanged.

### Executed suites

The final optimized suite passed **101 Solidity tests**, with no failures or skips. The chain/conformance suite passed **321 pytest cases**. The lower-layer gate passed **30 checker tests, 270 L0/L1 cases and 180 L2 cases**, including the real two-process HTTPS demo; L0/L1 retain zero missing domain/core statements or branches. Ruff, generated-file drift and frozen ABI checks passed. Exact full test IDs and fixture links are checked against the executed XML/Forge reports; omitted, duplicate, failed, skipped and unmapped cases fail acceptance.

Coverage includes all 18 market, 15 reputation, 34 lifecycle and 17 delegation goldens under their documented projection boundaries; original signature/hash vectors, canonical wire objects, same-block transaction ordering, independent event reconstruction with restart overlap, and orphan-log rollback. The 32 fixed differential seeds each execute 64 commands and then drain credits, independently advancing L1 and Solidity and comparing full records, ordered events, nonces, counters, native balances and parent envelopes after each command. The URI campaign compares 1,479 actual EVM results against the pinned independent Python parser, including 199 reviewed UTF-8/URI boundaries. Math/signature/URI fuzz properties run 256 cases; the invariant campaign runs 64 sequences of depth 128 and checks successful transitions so an all-revert run cannot pass.

### Completed offline gate and source coverage

`make check-l3` completed successfully, including all preceding suites, exact fixture mapping, source coverage, final Ruff/format/diff checks and restoration of the locked optimized build. Its retained report is `.scratch/layer3/gate.json`. Instrumented coverage executes the same maximum-record functional scenario; only the separately named gas-limit assertion test is excluded from instrumentation because its compiler profile changes gas costs. That assertion passed in the optimized suite.

| Executable production source | Lines | Branches | Functions |
|---|---:|---:|---:|
| `AgentLanceMarket.sol` | 388/400 | 49/49 | 46/48 |
| `ContentUri.sol` | 160/162 | 57/57 | 7/7 |
| `ProtocolMath.sol` | 16/18 | 1/3 | 5/5 |
| `ProtocolSignatures.sol` | 19/23 | 2/2 | 6/6 |
| **Total** | **583/603 (96.68%)** | **109/111 (98.20%)** | **64/66 (96.97%)** |

The two uncovered math branches are the generic tie-order fallbacks for different chain IDs and registry addresses (`ProtocolMath.sol:38–40`). They are unreachable through production admissions, which require the configured chain and immutable registry; agent-ID tie ordering is exercised. The two uncovered functions, `readPolicy` and its internal helper, are reachable and repeatedly checked by the independent RPC suite, which Forge coverage does not instrument. Other missed statement anchors include executed condition headers, returns and ERC-1271 assembly; Foundry emits source-anchor warnings for this minimally optimized profile. These counts are not a claim of 100% source coverage or a proof over every compiler-generated branch.

### Build and measured bounds

The optimized market runtime is **41,601 bytes**, with **42,004 bytes** of initcode including its two constructor arguments. It fits the pinned native Monad limits (128 KiB runtime/256 KiB initcode); this is not an Ethereum 24-KiB deployment build. The exact compiled command/event/error ABI and separately generated eight-view ABI pass comparison without changing the frozen ABI.

The optimized bounds suite measures production entrypoints after test setup. Allocation reads 54 slots for every measured count from two through 1,024 bidders (52 for one); settlement reads 115. The tests compare both read/write counts and gas growth. Maximum valid URI length is 2,048 bytes; the valid contract-owner permit is 4,096 bytes. Measured call gas is 21,081,743 for creation, 448,096 for admission, 7,046,271 for result submission and 7,714,891 for verdict settlement, each below the asserted 30-million bound. Maximum TaskView/receipt ABI payloads are 15,520/3,552 bytes. Grandchild settlement changes only its immediate parent's envelope; terminal receipts remain unchanged.

Those Forge measurements are call deltas within a test, including storage warmed by setup; they are not live fee estimates. Separate Anvil transactions in the funded solo report record receipt gas of 668,517 (creation), 383,356 (permit admission), 290,456 (allocation), 233,909 (result), and 868,905 (verdict). Different fixture sizes and receipt gas accounting mean these values are not a controlled cold/warm price comparison. Each real transaction/report retains its actual gas used. Live limits/revision and conservative fee estimates must still be qualified before broadcast.

### Funded local evidence

`make demo-l3` writes an ignored `.scratch/layer3/demo-*/demo.json` report: **12 reports and 134 protocol transactions**, with zero remaining escrow/credits after each scenario. It contains solo SUCCESS; successful child/parent; parent timeout after child success; terminal parent followed by child expiry; all seven other terminal reasons; and the malicious scenario. Deposited amounts equal withdrawn amounts: 100 atoms per ordinary root, 260 per two-task tree and 400 across the malicious scenario's four tasks.

The malicious report includes invalid domain/old-owner permits, restored owner, nonce replay, unauthorized execution, wrong result/policy, high-s/invalid/expired verdicts, exact ERC-1271 rejection and acceptance, direct-owner admission, a reverting withdrawal followed by a different receiver, and a receiver attempting all ten commands during withdrawal. Expected rejections use actual transaction hashes and revert bytes; no rejection fabricates a successful event. Contract-signature and transfer outcome facts supplied to L1 come from independently configured test doubles. These are labelled synthetic identities/artifacts executing real local chain transactions.

### Reproduction and remaining limits

Run `make setup`, `make setup-l3`, `make check-l3` and `make demo-l3`. Detailed reports stay in ignored `.scratch/layer3/`; the durable exact test/fixture mapping is [acceptance-layer-3.json](acceptance-layer-3.json). Python is pinned to 3.12.12/Unicode 15.0.0 for URI oracle compatibility; the checksummed Solidity/Foundry/library pins and MonadTen profile are in `contracts/toolchain.lock.json` and `foundry.toml`.

**Live gate remains pending.** No qualified official registry/proxy/admin report, controlled testnet identities/keystores/funding, verified RPC finality/deployment evidence, explicit broadcast authorization or funded finalized Monad testnet receipts were supplied. The implemented driver therefore was tested through signed local transactions only. Its operational instructions are [layer-3-testnet.md](../docs/layer-3-testnet.md). The six L7-dependent manifest fields remain pending; no addresses or manifest were fabricated.

**Strict error-byte parity remains pending.** The frozen nonpayable ABI rejects nonzero value with empty EVM bytes, while L1 reports `WRONG_VALUE`. The suite checks both outcomes and unchanged economics without normalization. The executable signature-first verdict guard order remains preserved and tested against the documented prose discrepancy. The separate read ABI uses output name `view_` because Solidity reserves `view`; no frozen command, event or record changed. These explicit limits prevent a claim that the full L3 live/exact-parity exit gate has passed.
