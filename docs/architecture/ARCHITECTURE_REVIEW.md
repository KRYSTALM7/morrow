# Architecture consistency review

## Required-document status

All requested paths exist. They are complete as an architecture proposal, not as implementation specifications or production evidence. The following qualifications should remain visible:

| Document | Status | Gap / interpretation |
|---|---|---|
| README | Complete | Navigation and status are clear. |
| SYSTEM_DESIGN | Complete | Audit, review, phased roadmap, decisions and residual risks; product funds-flow remains unresolved. |
| HIGH_LEVEL_DESIGN | Complete | Modular boundaries, trust zones and AI/payment sequence. |
| LOW_LEVEL_DESIGN | Complete | Module seams and transaction patterns; not an implementation/API coding spec. |
| PAYMENT_ORCHESTRATION | Complete | State/workflow and PayPal Sandbox approach; exact PayPal account capability and supported recipient model unresolved. |
| LEDGER_DESIGN | Complete | Core balanced posting invariants; chart of accounts and business-specific postings await funds-flow/accounting approval. |
| AI_AGENT_DESIGN | Complete | Safety, confirmation and provider controls; selected Gemini model, privacy terms and prompt versions remain choices. |
| SECURITY_ARCHITECTURE | Complete | Controls and threat boundaries proposed; no assessment or compliance certification performed. |
| SCALABILITY | Complete | Explicit traffic assumptions and load plan; targets are unmeasured and DB sizing is not proven. |
| GCP_DEPLOYMENT | Complete | Proposed environments/topology; no IaC, project or provisioning. |
| OBSERVABILITY | Complete | SLI/SLO targets and signal design; no telemetry currently instrumented. |
| FAILURE_RECOVERY | Complete | Nine requested failure modes and DR/replay semantics covered; recovery objectives await owner decision and drills. |
| COST_ESTIMATION | Preliminary | Transparent broad ranges and cost drivers, not line-item regional pricing. Refresh with chosen region/configuration before deployment. |
| DATA_MODEL | Complete at proposal level | Entities, constraints and partition criteria; no executable DDL or migration yet. |
| API_DESIGN | Complete at proposal level | `/v1` examples and legacy route summary; detailed OpenAPI schema is a later implementation artifact. |
| ADR 001–006 | Complete | Decisions and tradeoffs recorded; revisit as product/account/provider and measured scaling evidence evolves. |

No required architecture document is missing. “Complete” means the requested design topics are covered; it does not mean software exists or is production-ready.

## Cross-document consistency checks

1. **One modular monolith:** HLD, LLD, ADR 001 and deployment plan use shared domain code with separately scalable API/webhook/worker processes. This avoids contradicting the no-premature-microservices principle.
2. **One financial authority:** PostgreSQL + immutable double-entry journal is the only authoritative financial store. Redis, Pub/Sub, the AI provider, frontend and analytics are not authorities.
3. **External side effects:** Payment design and ADR 003 agree that DB/provider are not one ACID boundary; durable outbox/inbox, provider idempotency, lookup and reconciliation recover ambiguity. Messaging remains at least once; no exactly-once delivery claim.
4. **Consent and execution:** AI documents and ADR 005 agree that the model proposes only; a user confirms exact material and deterministic policy rechecks before posting.
5. **Money representation:** API, schema, ledger and tests use integer minor units and explicit currency. The legacy name `amount` is identified as minor units; `/v1` uses `amount_minor`.
6. **Capacity claims:** 1M registered, 10k concurrent and 2k RPS are explicitly modelled assumptions/targets. No support claim is made before load test.
7. **GCP cost and reliability:** Demo and production configurations distinguish single-zone cost choices from Cloud SQL HA; cost ranges are caveated and no resource was provisioned.
8. **Historical behavior:** Stage exports, effective/recorded time, revisions, holds, splits, settlement/refund semantics and UI are treated as migration/reference concerns rather than discarded.

## Inconsistencies and risks resolved during review

- **Unauthenticated destructive test endpoints:** dispatch-order inspection of all four stages confirmed reset/import/export are handled before bearer-token enforcement. System design calls this a P0 release blocker for all stages, not a routine hardening item.
- **PayPal vs peer-to-peer wallet:** “PayPal integration” cannot be equated with user-to-user payout. Documents now require identification of order beneficiary and wallet legal meaning before claiming live settlement.
- **Optional API gateway:** the product diagram shows an API gateway as a logical edge policy layer; deployment makes a separate API Gateway optional to avoid redundant components.
- **AI vs deterministic operations:** assistant confirmation endpoints still pass through exact proposal digest, user confirmation and domain application service. No general execution tool is available to the model.
- **Provider idempotency horizon:** PayPal’s documented request-ID retention is finite (up to 45 days); Morrow’s own durable operation record and reconciliation remain necessary after that horizon.

## Approval gates before implementation

1. Decide beneficiary/funds flow, wallet meaning and PayPal account product (including target region); label demo behavior accurately.
2. Agree MVP scope and any mock/simulated portions explicitly. PayPal operations must use Sandbox credentials and actual Orders API responses; no fabricated provider status.
3. Approve ledger account chart/posting rules and conversion treatment for existing fixture balances.
4. Decide auth/browser session and role model, plus removal/isolation of `/_test/*` routes.
5. Choose GCP region, spend cap, HA level and RPO/RTO; refresh price estimate in calculator.
6. Define AI provider/model/data-retention settings and user confirmation UX.

