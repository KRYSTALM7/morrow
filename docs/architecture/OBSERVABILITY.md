# Observability and reliability

## Signals

- Structured JSON logs with UTC time, severity, service/version/environment, trace/span ID, request/correlation ID, tenant pseudonym, actor class, route template, status, duration and safe error code. Redact secrets, passwords, authorization/cookie headers and sensitive payment/PII fields.
- OpenTelemetry trace spans: edge request → API → DB transaction → outbox publish → Pub/Sub delivery → provider request/webhook → ledger post/reconciliation. Carry correlation and payment/intent identifiers as controlled attributes; no full PII.
- Metrics: request rate/latency/error by route; auth/rate limiting; DB pool wait/connections/locks/deadlocks; ledger posting failures/invariant audit; idempotency replay/conflicts; outbox age/attempts; Pub/Sub backlog oldest age/DLQ; provider latency/error/429; webhook verification/duplicates/lag; reconciliation unmatched value/count; AI latency/timeout/schema refusal/token budget; backup age/restore test.

## Initial service objectives (targets)

| SLI | Initial target | Notes |
|---|---:|---|
| Core authenticated API availability | 99.9% monthly | Excludes third-party provider/AI; define maintenance and synthetic check policy. |
| Internal payment-command latency | p95 <300 ms | From API ingress through durable DB commit, excluding PayPal completion. |
| Ledger integrity | 100% balanced committed journal tx | Any violation pages incident response; not a percentile SLO. |
| Durable async processing | 99% accepted outbox events start processing within 60s | Tune after traffic/worker baseline. |
| Backup recoverability | Successful restore exercise on schedule | Define RPO/RTO after product and budget decision. |

Targets are not achieved claims. Error budgets guide rollout pace; payment integrity and security incidents have stricter operational policy than availability budget.

## Alerts and operations

Page on sustained core 5xx/latency, DB unavailable/pool exhaustion, ledger invariant mismatch, capture state ambiguity past threshold, invalid webhook surge, DLQ growth/age, reconciliation unmatched value breach and backup/PITR failure. Ticket on AI outage (payments remain available), cache degradation and noncritical analytics lag. Dashboard per payment lifecycle from intent to provider resource to journal transaction.

Correlate support trace with opaque IDs only. Access to audit/payment data is role-controlled and itself audited. Use sampling for high-volume successful traces but retain error and financial transition traces under approved privacy policy.

