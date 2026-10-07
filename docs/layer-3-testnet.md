# Qualified Layer 3 testnet scenarios

The live driver is implemented but no Monad testnet deployment or scenario is asserted by its offline tests. Obtain explicit deployment/broadcast authorization, a qualified registry, actual controlled identities, encrypted actor keystores, fee funding and a verified Layer 3 report before running it. The `DeployMarket` Foundry script deploys only on chain 10143 and checks the configured registry code hash. It does not perform the preceding registry/RPC qualification.

After qualification, invoke one scenario:

```sh
AGENTLANCE_L3_ACTORS=/absolute/private/actors.json \
  uv run --locked python scripts/demo_layer3.py \
  --network monad-testnet --report /absolute/path/layer-3-verification.json \
  --scenario solo
```

The other scenario names are `parent-child`, `timeouts` and `malicious`. There is no live `all` mode. Windows use real finalized block timestamps and may take minutes. No live path calls Anvil methods, changes registry ownership, deploys a substitute registry or uses the L1 fixture market as authority.

| Scenario | Transactions actually exercised |
|---|---|
| `solo` | Fund 100 atoms, relay an owner permit, allocate, accept, commit, sign fixture PASS, verify payment 50/refund 50 and withdraw. |
| `parent-child` | Fund a 200-atom parent and a separate 60-atom child; settle child then parent for payments 40 and 100; withdraw all 260 atoms. |
| `timeouts` | Separate 100-atom roots for cancellation, unallocated auction, allocation expiry, no-show, execution timeout, validator timeout and fixture FAIL, followed by full refunds/withdrawals. |
| `malicious` | Separate EOA-owner and ERC-1271-owner roots; reject wrong-chain/invalid permits, unauthorized acceptance, wrong result and high-s verdicts, then complete valid PASS and withdrawals. |

The live driver runs these representative cases. The exhaustive transfer, receiver, reentrancy and deadline permutations remain in the offline suite; a successful live journal does not claim those permutations were broadcast.

## Local configuration

`AGENTLANCE_L3_ACTORS` identifies an untracked JSON file. Resolve every file path below relative to its directory. Keep it outside tracked source or in ignored `.scratch/`. The file contains:

| Key | Contents |
|---|---|
| `roles` | Object keyed by role name. Each value has lowercase nonzero `address`, encrypted JSON `keystore` path and optional `passwordEnv` naming a secret environment variable. Without the variable, the driver prompts using `getpass`. Contract-owner roles omit `keystore`. |
| `identities` | Object keyed by the identity names below. Values contain canonical decimal-string `agentId`, role names `owner`, `signer`, `payout`, `permitSigner`, lowercase 32-byte `profileDigest` of the retained Agent Card, and its local `profileFile`. The exact file bytes must match the digest. |
| `artifacts` | Exactly the fixture inputs used by the run: `input`, `outputSchema`, `validationPolicy`, `result`, `evidence`. Each value contains canonical ContentRef `ref` and a local `file` with the exact committed bytes. Hashes are keccak256 of those bytes. |
| `evidenceFiles` | Local paths for `identityVerification`, `rpcVerification`, `validatorVerification`, `scenarioEvidence`. Their exact bytes must match the corresponding ContentRefs in the verified report. |
| `windowSeconds` | Integer 120–3600. Ordinary deadlines are T+W, T+2W, T+3W, T+6W, T+8W. Parent result/validation windows are T+20W/T+22W to leave room for the child. |
| `maxGas` | Explicit per-transaction gas cap, 21,000–30,000,000. An expected reverting adversarial transaction uses this limit. |
| `maxGasPriceWei` | Positive integer string, the maximum gas price the operator authorizes. Value plus the maximum fee must be funded before each send. |
| `completeManifest` | Optional local path, required only if the report clears all pending L7 manifest fields. |

Roles must have distinct addresses. Every scenario requires `requester`, `relayer`, `validator` and `receiver` signing accounts, plus its identity owner/controller, execution signer and payout accounts. The parent identity must use the role `signer`, which supplies the child's fresh 60 atoms and receives its refund. Payout/refund roles need keystores to withdraw their credits. Default local fixture accounts are rejected by the live loader.

Identity names are `solo`; `parent` and `child`; `noShow`, `executionTimeout`, `validatorTimeout`, `validationFailed`; or `malicious` and `contractOwner`, respectively. Identities must be distinct and have zero market counters before their scenario. Configure identity owner, signer and payout as separate roles excluding `validator`; for the malicious cases the execution signer must also differ from `relayer`. A code-free owner uses its own role as `permitSigner`; a different controller signer is only applicable to the qualified ERC-1271 owner. The actual registry owner and verified wallet are rechecked. A contract owner must have deployed ERC-1271 code accepting a standard 65-byte EIP-712 signature from the configured `permitSigner`; unsupported account signature schemes need a separate reviewed adapter. No ECDSA fallback is used for that owner.

## Retained qualification evidence

The report schema is [layer-3-verification.schema.json](../specs/schemas/layer-3-verification.schema.json). These local evidence objects supplement its closed structure:

- `rpcVerification`: `officialSource` exactly `https://docs.monad.xyz/developer-essentials/testnet`, canonical `chainId` and `genesisHash`, observed `executionRevision`, exact `clientVersion`, and Unix-seconds `validUntil`. The selected report RPC must support finalized/historical reads. The malicious scenario additionally requires `debug_traceTransaction` with `callTracer` so its actual mined revert bytes can be checked.
- `identityVerification`: `identityRegistry`, `identityVersion`, `validUntil`, `proxyKind`, `codeObservations` containing `{address, codeHash}`, `storageObservations` containing `{address, slot, value}`, and `probes` recording successful `transferClearsWallet`, `restoredWalletControl` and `contractOwner` checks. A proxy requires storage observations. Retain the underlying transactions/source/admin/implementation evidence alongside these observations; booleans alone do not establish qualification.
- `validatorVerification`: the qualified `validator` address and retained control evidence. Each run also records a fresh market/chain-bound key-control challenge.
- `scenarioEvidence`: exact retained evidence from qualification. A new scenario journal is an additional artifact and does not overwrite or automatically publish the immutable input report.

Retain the source commit, working-tree dirty status and exact source snapshot alongside the build evidence; a commit hash alone does not identify an uncommitted implementation. The closed build object records the production source-tree digest without adding fields to the canonical manifest.

The driver recompiles with the locked offline toolchain, compares the complete build evidence, deployment transaction input/constructor arguments, finalized deployment receipt, runtime hashes and readPolicy. Registry implementation/admin code and storage are rechecked before new tasks/bids. A change stops new exposure; it does not add a market pause or alter admitted obligations.

## Evidence and interrupted runs

Each run writes `.scratch/layer3/testnet-*/scenario.json` with transaction hashes, canonical events, finalized block/hash references, receipts, snapshot/current counters, conservation checks and withdrawal totals. Artifacts and validator verdicts are explicitly fixture material. The journal contains no keystore passwords, private keys, raw signed transactions or RPC URLs.

A send timeout can occur after broadcast. The journal saves the computed transaction hash **before** sending and records `WAIT`; reconcile that hash before starting another run. There is no automatic resend or substitution of `latest` for `finalized`. Polling is at least two seconds apart, requests have ten-second timeouts, and action deadlines stop further progress. A funded task may still require permissionless expiration and withdrawal after an interrupted run; the driver does not pretend that stopping cancels it.

Offline integration tests exercise the same four scenario methods through real signed transactions on an isolated Monad Anvil. Their explicit test transport supplies a finalized boundary and advances only that local clock. Those results are not testnet qualification or finality evidence.
