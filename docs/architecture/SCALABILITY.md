# Scalability and performance

## Terms and explicit model

Registered users are lifetime accounts; monthly active users are those active in a month; concurrent users are simultaneous sessions/requests; requests per second (RPS) is an offered/served rate. One million registrations does not imply one million concurrent users.

Planning assumptions (illustrative, replace with analytics): 1,000,000 registered; 10% MAU = 100,000; busiest 1% of MAU active at once = 1,000 interactive users; initial stress target is 10,000 concurrent sessions; at 0.2 API requests/s per active user this is 2,000 RPS. For a 5x burst use 10,000 RPS capacity headroom. If 10k users each issue 1 request/s, offered load is 10k RPS; this is a separate stress scenario. Read/write mix assumption 90/10 gives 200 financial writes/s at the 2k target, with actual payment completion far lower and provider-limited. Size only after workload measurement.

These are test hypotheses, not measured support claims. Target objectives to evaluate: 99.9% monthly availability and p95 internal API latency <300ms excluding provider latency. Provider workflow endpoints should return durable pending status quickly and never hold request open for external completion.

## Scaling controls

- Stateless API/worker instances; all authoritative state in PostgreSQL, no sticky sessions or process-local idempotency.
- Cloud Run autoscaling with max instance guardrails. Couple per-instance async concurrency to DB pool. Pooler/connector only if supported and transaction/session semantics fit. Budget `instances × pool max` below DB connection ceiling.
- Redis: cache static/reference data and short-lived read aggregates only with versioned keys; rate limits and ephemeral coordination where loss is safe. Cache miss/failure falls back to Postgres or degraded responses; never use Redis as ledger or idempotency authority.
- Pub/Sub: independently scale consumers on backlog age/size, cap provider concurrency and DB writer concurrency, acknowledge after durable state transition. Retry with jitter/DLQ. Backpressure at API via 429/503 before DB saturation.
- DB: tune indexes from `EXPLAIN (ANALYZE, BUFFERS)`, keep transactions short, batch analytics reads, avoid N+1, use primary for money decisions. Read replicas for lag-tolerant history only; state lag in product behavior.
- Partition only when size/retention and query evidence justify it. Candidate append-only ledger/event/audit tables by recorded month; plan uniqueness and FK constraints carefully.
- Hot account contention: account row lock is the truth. Limit attempts per account, queue serial commands if needed, split independent sub-ledgers only if accounting semantics allow. Do not shard one account's spendable balance without consensus/locking semantics.
- Isolate slow jobs (AI, exports, reconciliation, notifications) from payment API pools. Apply deadlines, quotas, circuit breakers and bounded queues.
- Graceful degradation: AI disabled → deterministic forms; Redis down → rate-limit fallback/local emergency controls and DB reads; Pub/Sub delayed → durable commands remain pending; provider down → pending/retry, no false success; analytics down has no command-path impact.

## Load testing plan

Use Locust with realistic account distribution and weighted scenarios: auth/session refresh, balance/history reads, request/split preview, idempotent writes, concurrent debit on same and varied accounts, webhook duplicate bursts, worker backlog, provider latency/error simulation, report reads. Ramp to 10k concurrent users and 2k RPS, then 10k RPS burst; test at 1×, 2× and 5× duration phases. Run against staging-sized Cloud Run/Cloud SQL, not in-memory server. Verify ledger invariants and idempotency throughout. Record code/image/DB sizes, workload mix, region, p50/p95/p99, error/timeout rates, DB locks/CPU/connection utilization, queue age and cost. No target is achieved until results are reviewed and repeatable.

