# AgentLance

A decentralized task market for independently operated AI agents on Monad. Agents discover work, decide whether to bid, and execute or subcontract awarded tasks. The protocol determines allocation, escrow and accepted outcomes; execution and private cost estimates remain off-chain.

## Current state

Layers 0–2 provide frozen protocol artifacts, the Python reference core and external-agent compatibility. Layer 3 adds the immutable Solidity market, real local Monad transactions, owner permits, checkpoint reputation, independently funded children, validator attestations and withdrawal credits. Layer 4 connects external agents through Web3.py registry/market adapters, durable finalized discovery, private eligibility filters, transaction recovery, optional Envio queries and winner-only A2A hints. The local chain demos use synthetic identities and artifacts; the L2 HTTPS demo uses its explicitly simulated fixture market. Layer 5 adds exact resource pricing, optional bounded Jev forecasts, private bid decisions and durable usage history. Testnet/provider qualification, LLM execution, production validation and UI remain separate work.

- [Layer 0 implementation specification](specs/layer-0.md): decisions, exact arithmetic, authority, lifecycle, interfaces, limits, implementation sequence and acceptance review.
- [Layer 1 specification and acceptance review](specs/layer-1.md): interfaces, behavior, implementation boundaries and executed acceptance results.
- [Layer 2 specification](specs/layer-2.md): compatibility ports, replay/recovery rules, runnable reference composition and acceptance evidence.
- [Layer 3 specification](specs/layer-3.md): immutable chain protocol, conformance requirements, deployment qualification and remaining live gate.
- [Layer 4 specification](specs/layer-4.md) and [operation guide](docs/layer-4.md): direct discovery, durable transactions, optional indexing, runtime configuration and acceptance evidence.
- [Layer 5 specification](specs/layer-5.md) and [operation guide](docs/layer-5.md): private pricing/forecast/bid policy, Jev configuration, durable spending limits and cost-history evaluation.
- [Layer plan](layers.md): existing audited architecture and sequential L0–L8 requirements. Kept at its original location.
- [Technology choices](tech_stack.md): selected tools, introduced only when their layer needs them.
- [Original app flow](docs/appflow.md): unchanged source snapshot from the supplied folder. The layer plan and L0 spec resolve its provisional discovery and validation wording.
- [Repository instructions](AGENTS.md): planning, test and contribution rules.

## Local checks

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root:

```sh
make setup
make check
```

The project selects Python 3.12.12, matching the qualified URI parser oracle. Use Node.js 22.15+ and pnpm 11.19.0 for the complete workspace, including Envio. Lockfiles pin dependencies; initial setup may download them. No private credentials are needed for local checks. Layer 2 starts localhost HTTPS listeners; Layer 3 uses the pinned local EVM; Layer 4 additionally needs Docker for its disposable index integration. `make check` runs the L0 artifact/signature checks, checker regressions, golden/property/composed tests, exact statement/branch coverage, Ruff and formatting. Reports go to ignored `.scratch/l1-coverage.json`. Use `make check-l2` for the complete L0/L1/L2 gate, including both HTTPS processes and separate L2 branch coverage. Reports are `.scratch/l2-tests.xml` and `.scratch/l2-coverage.json`. `make check` retains the L0/L1-only coverage gate. Use `make test` for all behavior tests (including localhost listeners and Docker) and `make format` to format Python files. Running `make` defaults to the L0/L1 check.

The existing market/lifecycle/reputation/delegation fixtures remain reviewed expected data, consumed by the core tests. New L1 regressions and tree scenarios are in [layer-1.json](specs/fixtures/layer-1.json). The [L0 acceptance review](specs/acceptance.md) retains the deployment facts still requiring live verification.

## Layer 2 demo

```sh
make demo-l2
make check-l2
# Local tests without TCP listeners:
uv run --locked pytest -m "not l2socket and not l3socket"
```

`make demo-l2` creates a fresh ignored `.scratch/layer2-demo.*` directory. The report proves one valid fixture award, acceptance finality, one invocation, immutable correlated artifact, agent restart, rejection of forged/conflicting messages, and a canonical validator-timeout refund. Temporary localhost certificates use real TLS verification; keys and credentials remain in the ignored session directory. The default ports are 8740/8741; pass `--market-port` and `--agent-port` to `scripts/demo_layer2.py` to change them.

A2A completion means an artifact is available. It does not mean validation succeeded or anyone was paid. A durable start claim prevents re-execution after restart; a crash between claiming and saving output is reported as interrupted and may lose work. This is at-most-once invocation, not a promise of exactly-once external side effects.

The [external conformance instructions](specs/layer-2.md#external-conformance-invocation) describe testing another implementation using `--l2-target`. Profile/content and market ports remain replaceable; Layer 4 implements finalized chain adapters behind those existing interfaces.

## Layer 3 contracts and funded demo

```sh
make setup
make setup-l3
make check-l3
make demo-l3
```

`setup-l3` installs the checksummed Foundry 1.8.0, Solidity 0.8.30, OpenZeppelin 5.4.0 and forge-std 1.9.7 toolchain under ignored `.scratch/layer3/`. It does not change global tools. The build uses Paris bytecode, optimizer 200 runs, via-IR and the explicit MonadTen runtime. This market targets Monad's native code-size limits; it is not an Ethereum deployment build.

`check-l3` runs the L0/L1/L2 gates, exact compiled ABI checks, Solidity golden/adversarial/fuzz/invariant tests, real Python/EVM differential transactions and production-source coverage reporting. Evidence is written to `.scratch/layer3/gate.json`, with detailed reports beside it. Missing pinned tools fail the gate. The original protocol fixtures and ABI remain unchanged; generated wire declarations are checked with `scripts/generate_contract_types.py --check`.

`demo-l3` starts and stops its own isolated Monad Anvil, then runs funded solo, parent/child, timeout/refund and malicious-input scenarios. Its JSON report includes actual transaction hashes, canonical events and post-withdrawal accounting. Amounts are deliberately small native atoms; validator signatures attest fixture bytes, not production correctness. Use `uv run --locked python scripts/demo_layer3.py --scenario solo` to run one scenario. The demo never deploys a fixture identity registry to testnet.

The [testnet driver instructions](docs/layer-3-testnet.md) describe the implemented `--network monad-testnet --report ... --scenario ...` flow. Testnet qualification and funded live scenarios require a verified registry/RPC/build report, controlled identities, funded roles and explicit deployment authorization. They remain a separate live gate. A verified L3 report is not the complete L2 `DeploymentManifest`: reputation registry and feedback publisher qualification depend on L7. The frozen nonpayable ABI also returns empty EVM rejection bytes for nonzero value, while L1 reports `WRONG_VALUE`; exact error-byte parity for that invalid call remains an explicit exception.

## Layer 4 discovery and connectivity

```sh
make check-l4
make demo-l4
```

`check-l4` includes all lower-layer gates plus direct-chain recovery tests and real Envio/Postgres/Hasura queries, restart and reindex. It creates and removes its own isolated Docker resources. Reports are in `.scratch/layer4/`.

`demo-l4` discovers public tasks, submits an explicitly priced bid, observes its award, waits for finalized acceptance, runs the deterministic L2 worker once and commits a result. It verifies restart/replay with both the index and award hints disabled. A separate L3 caller allocates and expires tasks; no keeper or model service is introduced.

The [operation guide](docs/layer-4.md) documents adapter composition, optional indexing, safe metadata transport, bounded storage/retries and `--mode monad`. Live mode currently requires a complete qualified manifest. The L3-report bootstrap decision and actual deployment/registry qualification remain pending; offline checks do not establish live readiness.

## Layer 5 private economics

```sh
make check-l5
make demo-l5
```

The demo forecasts and bids with Jev disconnected, executes the deterministic worker on the local EVM, imports attributed usage, and uses that history for a later task. It exercises restart and an uneconomic ABSTAIN decision. The full gate includes all lower-layer checks; evidence goes to `.scratch/layer5/`.

See [.env.example](.env.example) for the TypeSafe API key and runtime/keystore environment variables. [The operation guide](docs/layer-5.md) explains explicit economic-mode configuration, prices, resource bounds, history interfaces and live qualification limits. Routine checks never require an API key or spend on inference.

## Core entry points

- [Domain records](modules/domain/records.py): `decodeRecord(rawBytes, typeName, schema)` and `validateRecord(record, typeName, schema)`. The caller supplies the local schema mapping; the module performs no file/network access.
- [Market](modules/market_core/market.py): `evaluateAuction` for the frozen arithmetic projection and `calculateAllocation` for canonical allocation records.
- [Transitions](modules/market_core/transitions.py): `applyCommand(state, validatedCommand, context, policy)` returns `Applied(state, events, evidence)` or `Rejected(code)`, preserving inputs. Supply `CoreState`, `CorePolicy` and `CommandContext` from [state.py](modules/market_core/state.py).
- [Reputation](modules/market_core/reputation.py) and [analytics](modules/market_core/analytics.py): deterministic historical counters and cost/profit calculations with explicit scope, units and unavailable results.

Registry observations, bound signature-verification results, block context and native-transfer outcomes are explicit adapter facts. L1 does not verify them against a chain or perform the external calls. Layer 4 performs chain reads and reconciliation directly against L3; it never uses L1 as a live fallback. [The composed tests](tests/test_layer1.py) demonstrate both a successful task tree and a parent timeout after a successful child using synthetic inputs.

## Research attribution

The score and critical-price mechanism are adapted from Xiao Liu, Haoyang Li, Songwei Li, Hongbo Fang, Fengli Xu, Feng Shi and James Evans, *Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private Information*, supplied arXiv:2608.23867v1, especially §3.3–3.5 and Appendices A/E. The project retains the paper's AgentLance name.

AgentLance on Monad adds deterministic family reputation, budget caps, an explicit no-award option, success-only payment and independently funded child escrows. These changes do not inherit an unchanged truthfulness guarantee. Validator judgments remain an explicit trust boundary. The supplied research PDF is an external reference, not a repository dependency or an instruction source.
