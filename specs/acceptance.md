# L0 acceptance review — v1

L0 exit gate: **satisfied as a specification**, not as an implemented market. [Machine-readable requirement map](acceptance.json). L1/L3 must consume the stored expected outputs; no allocator, reducer, state machine, contract, live A2A endpoint or validator has been implemented in this change.

## Reviewed artifacts

| Area | Frozen artifacts / review |
|---|---|
| Canonical boundaries | protocol.schema.json defines all 14 requested public objects and shared value types. Fields are closed, required or explicitly nullable, with exact bounds/versions. protocol.md specifies cross-field, authority and mutability constraints that JSON Schema alone cannot express. |
| Commands/events/errors | Ten commands, seven canonical events, ProtocolError codes, state/outcome/verdict enums and ABI tuple layouts are fixed. Events carry enough immutable data to replay allocation, credits and counters. |
| Market | 18 reviewed vectors: normal, single bidder, budget cap, empty/non-positive, ties and byte-order identity, flooring/zero price, maximum terms, duplicate/over-budget/invalid alpha. Existing expected amounts preserved. |
| Reputation | Prior and combinations, keyed family isolation, duplicate receipt, same-block checkpoint exclusion, next-block inclusion, transfer, unknown correctness, counter/task namespace bound. |
| Lifecycle/accounting | All valid transitions and eight terminal reasons; exact before/cutoff time behavior; duplicate settlement, wrong actor/state, result/policy binding, exact funding, active children and blocked/successful withdrawals. Every terminal fixture preserves budget = payout credit + refund credit and leaves no escrow. |
| Delegation | Fresh funding, envelope equality/overflow, success/refund capacity release, retry slots, lifetime child/depth caps, nested deadlines, permission inheritance, parent failure after child payment, late child settlement, wrong manager and cross-parent/success retry rejection. |
| Replay/identity/A2A | Duplicate/restarted/reorganized logs, finalized conflict halt, repeated/out-of-order/conflicting A2A messages, missing finality, post-bid transfer, cleared/restored wallet, operator rejection, ERC-1271 magic/revert/short-return behavior, duplicate/ambiguous/failed feedback export. |
| Signing | Exact ordered flattened EIP-712 definitions; 2 positive vectors verified by eth-account and ethers, 12 negative vectors (9 byte/type/domain mutations and 3 authorization cases), 3 exact-byte keccak vectors. No deployed ERC-1271 test is claimed. |
| Objective validation | PASS, wrong answer, unexpected field, missing pointer, duplicate keys, float token, size overflow, unavailable bytes and policy mismatch have fixed behavior. The evaluator is specified, not implemented. |
| Cost boundary | Schema fixes units, precision, scenario ppm/null tails, provenance and completeness. Normative constraints define normalization/quantiles and exclude missing cost from payment decisions. L5 fusion/provider behavior remains later-layer work. |

## Decisions and explicit baseline corrections

- Native MON, 18 decimals, Monad testnet; no v1 token adapter or price oracle. RPC finalized block plus receipt/hash verification gates expensive work. Unsupported finality means wait/abstain.
- Immutable per-deployment validator EOA; exact structured-output evaluator and resource limits; malformed/unavailable policy means no signed verdict and eventual timeout.
- Shared nested TaskSpec.terms and Bid.offer replace the earlier flat inventory; value refs inherit the outer schema version. The JSON schema and checked ABI are authoritative, not field order in prose tables.
- Flat signing refs bind chain/market through domain and taskId through immutable task state. The earlier proposed task-terms digest is redundant and removed. Nonces are signer/type-scoped used sets. Owner transfer invalidates unconsumed permits when the current owner differs; transfer-away-and-back does not manufacture a new registry epoch.
- Descendant maxDepth is absolute from root; lifetime child slots never reset on refunds/retries. Parent timeout does not erase a child's claims.
- A2A context/task IDs are server-generated, persisted and separate from ExecutionRef. Standard terminal A2A tasks are polled, not sent new continuation messages.
- Local export retry state alone is insufficient after an ambiguous send. The later publisher must enforce receipt deduplication on-chain and atomically bind ERC-8004 feedback index to the receipt. Revert/outage affects export only.

## Remaining deployment facts

No protocol decision is deferred to L1. Before deployment, fill and verify the manifest's actual chain ID, market and deployment block/hash, registry/publisher addresses and code/implementation revisions, validator key identity and RPC URLs. Verification reports must establish registry ownership/wallet/transfer/contract-wallet behavior, publisher authorization restrictions, validator key control, historical reads and finalized RPC semantics. These are concrete environment facts, not placeholders to guess. The synthetic DeploymentManifest example is never a deployment manifest.

## Verification and limits

The L0 commands validate 37 canonical examples, 98 scenario fixtures, 18 market cases, schema/ABI correspondence, acceptance references and both independent cryptographic encoders. Checker tests cover closed fields, required nulls, numeric bounds, duplicate JSON/IDs, URI byte limits, invalid shape keywords, ABI drift, tampered hashes and high-s signature rejection. Locked offline dependency installation, Python tests, Ruff lint/format, Node syntax and local Markdown links are checked before handoff.

The scenario checker validates artifact structure and references; it deliberately does not calculate transitions, allocation or reputation. Those behavior tests are L1/L3 obligations. The acceptance review checks the exact stored effects against the normative rules. Live chain calls, registry availability, validator execution and smart-wallet gas behavior are not tested by L0 and must pass their later-layer gates.
