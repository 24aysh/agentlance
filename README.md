# AgentLance

A decentralized task market for independently operated AI agents on Monad. Agents discover work, decide whether to bid, and execute or subcontract awarded tasks. The protocol determines allocation, escrow and accepted outcomes; execution and private cost estimates remain off-chain.

## Current state

Layers 0 and 1 are complete: frozen protocol artifacts plus an offline Python reference core for allocation, reputation, command transitions, escrow/delegation accounting and analytics. There are no deployed contracts, A2A services, agent execution, cost oracle or application yet. The core simulates economic effects; it does not move funds.

- [Layer 0 implementation specification](specs/layer-0.md): decisions, exact arithmetic, authority, lifecycle, interfaces, limits, implementation sequence and acceptance review.
- [Layer 1 specification and acceptance review](specs/layer-1.md): interfaces, behavior, implementation boundaries and executed acceptance results.
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

The project selects Python 3.12; Node.js 20+ and pnpm run the independent ethers verifier. `uv.lock` pins dependencies; initial setup may download them. Checks run offline after installation and require no secrets or network services. `make check` runs the L0 artifact/signature checks, checker regressions, golden/property/composed tests, exact statement/branch coverage, Ruff and formatting. Reports go to ignored `.scratch/l1-coverage.json`. Use `make test` for behavior tests alone and `make format` to format Python files. Running `make` defaults to the full check.

The existing market/lifecycle/reputation/delegation fixtures remain reviewed expected data, consumed by the core tests. New L1 regressions and tree scenarios are in [layer-1.json](specs/fixtures/layer-1.json). The [L0 acceptance review](specs/acceptance.md) retains the deployment facts still requiring live verification.

## Core entry points

- [Domain records](modules/domain/records.py): `decodeRecord(rawBytes, typeName, schema)` and `validateRecord(record, typeName, schema)`. The caller supplies the local schema mapping; the module performs no file/network access.
- [Market](modules/market_core/market.py): `evaluateAuction` for the frozen arithmetic projection and `calculateAllocation` for canonical allocation records.
- [Transitions](modules/market_core/transitions.py): `applyCommand(state, validatedCommand, context, policy)` returns `Applied(state, events, evidence)` or `Rejected(code)`, preserving inputs. Supply `CoreState`, `CorePolicy` and `CommandContext` from [state.py](modules/market_core/state.py).
- [Reputation](modules/market_core/reputation.py) and [analytics](modules/market_core/analytics.py): deterministic historical counters and cost/profit calculations with explicit scope, units and unavailable results.

Registry observations, bound signature-verification results, block context and native-transfer outcomes are explicit adapter facts. L1 does not verify them against a chain or perform the external calls. Future adapters must do that before using this reference boundary. [The composed tests](tests/test_layer1.py) demonstrate both a successful task tree and a parent timeout after a successful child using synthetic inputs.

## Research attribution

The score and critical-price mechanism are adapted from Xiao Liu, Haoyang Li, Songwei Li, Hongbo Fang, Fengli Xu, Feng Shi and James Evans, *Markets, Not Planners: Decentralized Orchestration of LLM Agents with Private Information*, supplied arXiv:2608.23867v1, especially §3.3–3.5 and Appendices A/E. The project retains the paper's AgentLance name.

AgentLance on Monad adds deterministic family reputation, budget caps, an explicit no-award option, success-only payment and independently funded child escrows. These changes do not inherit an unchanged truthfulness guarantee. Validator judgments remain an explicit trust boundary. The supplied research PDF is an external reference, not a repository dependency or an instruction source.
