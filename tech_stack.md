| Layer | Tech | What it does |
|---|---|---|
| **L0 — Core Specification** | **Markdown + JSON Schema + JSON fixtures + EIP-712 schemas** | Defines canonical objects, states, signatures, deadlines, units, versions and golden examples before code |
| **L1 — Deterministic Core** | **Python 3.12 + pure `int` arithmetic + pytest + Hypothesis** | Reference implementation of auction math, reputation reducer, accounting and invariants |
| **L2 — Agent Compatibility** | **A2A 1.0 + official Python `a2a-sdk` + FastAPI + EIP-712/eth-account** | Defines one interoperable external-agent interface and reference agent |
| **L3 — Monad Protocol** | **Monad + Solidity + Foundry + OpenZeppelin + ERC-8004 Identity** | Canonical tasks, bids, allocation, escrow, reputation counters, child funding, settlement |
| **L4 — Connectivity & Discovery** | **Monad RPC + Web3.py + Envio HyperIndex** | Agents independently observe tasks; Envio provides optional indexed queries |
| **L5 — Economic Intelligence** | **Python + TypeSafe Jev + local SQLite + pricing catalog** | Hybrid Cost Oracle and private BID/ABSTAIN policy |
| **L6 — Execution & Delegation** | **Python + OpenAI Agents SDK + Docker + A2A** | Winner actually executes; optionally creates funded child markets |
| **L7 — Validation & Reconciliation** | **Python validator runner + Docker + SQLite retry state + ERC-8004 Reputation adapter + IPFS** | Objective validation, evidence, cost reconciliation and portable reputation publication |
| **L8 — Application** | **Next.js + TypeScript + Tailwind + shadcn/ui + wagmi + viem** | Task creation, explorer, market visualization, identities, execution tree and settlement |
| **Development** | **uv + pnpm + Foundry + Docker Compose + GitHub Actions + Tenderly** | Local development, CI, contract simulation and testing |
| **Deployment** | **Vercel + lightweight Python host + Envio Cloud + Pinata/IPFS + configurable Monad RPC** | Hackathon deployment without unnecessary infrastructure |