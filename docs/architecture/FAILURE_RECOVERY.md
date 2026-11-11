# Failure and recovery design

| Failure | Safe behavior and recovery |
|---|---|
| PayPal unavailable | Create no false success. If intent/outbox committed, return pending. Circuit-break calls, back off, respect retry guidance. Resume with stable PayPal request ID; reconcile provider resources when possible. User can cancel only if provider state permits. |
| AI provider unavailable | Hide/disable agent proposal and offer deterministic payment UI. No effect on ledger or payment APIs. Bound timeout and token spend; do not replay sensitive commands automatically. |
| PostgreSQL temporarily unavailable | Reject new financial commands with retryable 503; no memory fallback. Health/readiness fails for writes. Existing durable workers retry after DB recovery. API idempotency lets client safely retry. |
| Duplicate webhooks | Verify signature, insert unique `(provider,event_id)` inbox record. Duplicate returns success after dedupe. Domain event/version/source uniqueness prevents duplicate ledger posting. |
| Capture succeeds but application times out | Mark operation uncertain/pending; client must query intent. Worker uses same PayPal request ID or GET known order/capture, then records confirmed result. Webhook/reconciliation closes gap. Unique source posting ensures one financial effect. |
| Pub/Sub delivers event multiple times | At-least-once is expected. Consumer claims inbox/event ID and guards aggregate version/operation key in DB transaction. Ack only after commit. |
| Redis unavailable | Bypass noncritical cache. Use database-backed durable idempotency. Rate-limit fallback has conservative emergency controls; if abuse protection cannot be maintained, reject risky writes rather than permit unlimited traffic. |
| Consumer crashes mid-process | Before DB commit, transaction rolls back and message redelivers. After commit-before-ack, redelivery sees dedupe/terminal state and is acknowledged. External call ambiguity follows provider lookup/idempotency path. |
| Deployment fails | Cloud Run revision health/metrics gate traffic; shift traffic back to previous revision. Expand/contract DB migration preserves old revision compatibility; pause incompatible worker events. Never roll back schema destructively with app rollback. |

## Disaster recovery

Cloud SQL HA handles zone failure but not logical corruption or regional disaster. Configure backups/PITR and decide cross-region copy based on data residency/cost. Restore into isolated environment, validate schema, ledger balancing, projection rebuild and provider reconciliation before cutover. Set RPO/RTO only after business approval and prove them in drills. Preserve outbox/inbox/reconciliation evidence. Runbook for suspected ledger corruption freezes new money writes, keeps reads/audit available where safe, pages finance/security owners, captures database snapshot, compares journal/projections/provider reports, and posts approved compensating entries after root cause.

## Delivery guarantees

Pub/Sub and webhook transport are at least once. “Exactly once” is not promised. The design achieves one logical command/economic posting through idempotency, unique constraints and reconciliation, while retaining duplicate delivery evidence.

