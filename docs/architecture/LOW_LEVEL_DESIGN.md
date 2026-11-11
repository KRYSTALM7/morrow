# Low-level design

## Suggested code boundaries

```text
src/morrow/
  api/v1/                 routers, request/response schemas
  identity/               sessions, policies, RBAC
  wallet/                 accounts, requests, splits, holds
  payments/               intents, state machine, provider ports
  ledger/                 account rules, posting, balance projection
  settlement/             close, reconciliation, adjustments
  ai/                     prompts, typed proposals, tool allowlist
  providers/paypal/       OAuth token client, Orders, capture, verify webhook
  messaging/              outbox publisher, inbox and consumers
  notifications/          notification preferences and delivery ports
  persistence/            SQLAlchemy models, repositories, units of work
  observability/          trace/log/metric setup
```

Routers are adapters only. Domain services receive a request-scoped unit of work and authenticated principal. SQLAlchemy `AsyncSession` is not shared across concurrent tasks. Do not hold a DB transaction open across an LLM, PayPal or notification network call.

## Command transaction pattern

1. Validate syntax and authentication; derive user/tenant from claims/session, never request body.
2. Begin short PostgreSQL transaction. Claim `(principal, operation, idempotency_key)` unique row with canonical request hash.
3. If completed matching request, return stored result; mismatch returns conflict. If in progress, return stable pending result or retryable conflict, not execute twice.
4. Lock affected wallet accounts in sorted UUID order (`SELECT ... FOR UPDATE`) and validate balances/limits/state version.
5. Insert domain records, balanced journal transaction/entries, projection updates and outbox event in this same transaction.
6. Commit, then return. External effects are performed by workers after commit.

## Concurrency

- `version` columns support compare-and-swap for user-facing stale edits.
- Row locks serialize money debits on each account. For transfers lock both accounts in deterministic order to prevent deadlocks.
- Unique constraints arbitrate duplicate idempotency keys, webhook event IDs, provider operation IDs and journal source references.
- Deadlocks/serialization failures retry the whole short DB transaction with bounded exponential jitter; do not retry a provider call inside the transaction.
- A hot pooled account is a throughput limit. Use per-account serialization queues only after measurement; never bypass correctness with eventually consistent balances.

## Reads and consistency

Read-after-write balance comes from the primary or committed response. User statement/history queries may use a read replica only if the API documents lag and offers a primary-read token/option for fresh activity. Authorization, available balance, capture status and ledger writes always use primary.

## API/UI evolution

Preserve Stage 4 as a behavioral reference and migration source. Introduce `/v1` schemas with ISO 4217 code plus integer minor units. Add compatibility adapter only for a bounded transition, with explicit deprecation. A React + TypeScript UI is justified if AI proposal review and live workflow status exceed the current vanilla UI's maintainability; the current UI already has responsive/a11y work worth retaining as design reference. Do not replace frontend as a prerequisite to financial persistence.

## Runtime security

- Cloud Run instances are immutable, non-root containers; configure startup/readiness probes and graceful shutdown.
- Database credentials are short-lived/connector-managed where supported; each service account has only its needed secret and topic/database role.
- DB pool max is computed from Cloud SQL connection budget: `max_instances × pool_size + migration/admin headroom <= safe connection limit`.
- Use bounded HTTP clients, provider/model timeouts and response-size limits.
- Disable public docs/openapi in production or gate them; never expose test reset/import routes.

