# ADR 003: Use an outbox with Pub/Sub for durable async work

- **Status:** Accepted for provider workflows
- **Context:** PayPal, notification and model calls cannot join a PostgreSQL transaction. Direct calls can succeed while the application times out.
- **Decision:** Commit outbox intent with domain changes, publish to Pub/Sub, process at least once, deduplicate consumers and track provider outcomes in an inbox/workflow. Apply bounded retries and DLQs.
- **Consequences:** Recoverable asynchronous behavior; eventual completion and operational replay tooling are required. No exactly-once transport claim.
- **Alternatives:** Call providers in DB transaction (long locks and still no atomicity); fire-and-forget task (loss risk); Kafka (unneeded at this scale/phase).

