# ADR 001: Start with a modular monolith

- **Status:** Accepted for target design
- **Context:** Four existing copies are single-file, in-memory services. The product needs clear boundaries and independent scaling, but no measured need for network-separated domain services yet.
- **Decision:** Build one modular application codebase with API, webhook and worker deployment processes. Keep domain modules (identity, wallet, payments, ledger, settlement, AI) isolated by interfaces and transaction ownership. Extract only when independent scaling, deployment/security or failure isolation is demonstrated.
- **Consequences:** Easier atomic financial operations and simpler hackathon delivery; codebase still needs module ownership and dependency rules. Shared database remains a coupling point. Do not claim microservice-level independent data ownership.
- **Alternatives:** Split every listed box into a service now (high operational and distributed-transaction cost); retain one giant app.py (poor change isolation).

