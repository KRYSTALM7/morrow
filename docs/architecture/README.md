# Morrow future architecture

**Product tagline:** Your money, tomorrow.

This directory records a proposed future architecture and migration plan for Morrow. It is a design, not an implementation or a claim of production readiness. No infrastructure has been provisioned and the stated capacity/SLO figures are design targets only. The current implementation is described in the repository [README](../../README.md), [Stage 4 run guide](../../stage-4/RUN.md), [finance API guide](../../stage-4/FINANCE_API.md), and [security notes](../../SECURITY.md).

## Read in this order

1. [System design](SYSTEM_DESIGN.md) — audit, target boundaries, review, roadmap.
2. [High-level design](HIGH_LEVEL_DESIGN.md) and [low-level design](LOW_LEVEL_DESIGN.md).
3. Domain designs: [payments](PAYMENT_ORCHESTRATION.md), [ledger](LEDGER_DESIGN.md), [AI agent](AI_AGENT_DESIGN.md), [data model](DATA_MODEL.md), [API](API_DESIGN.md).
4. Operations: [security](SECURITY_ARCHITECTURE.md), [scalability](SCALABILITY.md), [GCP deployment](GCP_DEPLOYMENT.md), [observability](OBSERVABILITY.md), [failure recovery](FAILURE_RECOVERY.md), [cost model](COST_ESTIMATION.md).
5. [Architecture consistency review](ARCHITECTURE_REVIEW.md) summarizes completeness, cross-document decisions and approval gates.
6. [ADRs](ADR/) record the major choices.

## Current repository in brief

The four `stage-N/` directories are independently buildable, standard-library Python HTTP services. Stages 1–3 are historical copies. Stage 4 is the current Morrow app: it serves a bundled HTML/CSS/JavaScript interface, persists wallet/session state and finance/PayPal records in separate SQLite stores, and includes forecasting, Safe-to-Spend, an optional Gemini companion, and PayPal Sandbox checkout. Some UI proposal/rate-limit/snapshot state remains process-local. Stage 4 tests cover API contracts, invariants, concurrency, historical queries, browser behavior, migration/import, finance records, AI provider behavior, and restart recovery. Its latest verified suite count is documented in the repository README; test results do not prove production readiness or capacity.

## Documents

| Document | Purpose |
|---|---|
| [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) | Repository audit, design review, roadmap and acceptance gates |
| [HIGH_LEVEL_DESIGN.md](HIGH_LEVEL_DESIGN.md) | Components, trust zones and system flows |
| [LOW_LEVEL_DESIGN.md](LOW_LEVEL_DESIGN.md) | Runtime modules, transactions, concurrency and deployment units |
| [PAYMENT_ORCHESTRATION.md](PAYMENT_ORCHESTRATION.md) | Provider adapter, state machine, PayPal and durable workflows |
| [LEDGER_DESIGN.md](LEDGER_DESIGN.md) | Double-entry accounting and financial invariants |
| [AI_AGENT_DESIGN.md](AI_AGENT_DESIGN.md) | Safe intent interpretation and approved tool execution |
| [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) | Identity, application, provider, AI and supply-chain security |
| [SCALABILITY.md](SCALABILITY.md) | Capacity model and performance strategy |
| [GCP_DEPLOYMENT.md](GCP_DEPLOYMENT.md) | Environments and GCP topology |
| [OBSERVABILITY.md](OBSERVABILITY.md) | SLOs, telemetry and operations |
| [FAILURE_RECOVERY.md](FAILURE_RECOVERY.md) | Failure modes and recovery behavior |
| [COST_ESTIMATION.md](COST_ESTIMATION.md) | Cost model assumptions and drivers |
| [DATA_MODEL.md](DATA_MODEL.md) | Relational schema and retention/partition guidance |
| [API_DESIGN.md](API_DESIGN.md) | Versioned API and event examples |
| [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) | Completeness and consistency review, unresolved risks |

