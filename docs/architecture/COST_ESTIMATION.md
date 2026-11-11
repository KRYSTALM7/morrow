# Cost estimation framework

This is a transparent planning model, not a quote. Prices vary by region, date, commitment, network egress, log volume and exact configuration. Check the GCP pricing calculator before deployment. No resources have been created.

## Illustrative demo environment

Assumptions: one GCP region; Cloud Run scale-to-zero or minimum 0–1, low max instances and 1 vCPU/512MiB–1GiB class; small single-zone Cloud SQL PostgreSQL; low Pub/Sub volume; no Redis initially; modest logs/traces; PayPal Sandbox has no payment processing fee but is not live money movement. For an always-on month, rough order-of-magnitude budget is **US$100–400/month** depending chiefly on database size/availability, minimum instances, egress and telemetry. This broad range is for budgeting only and must be priced in the chosen region.

## Illustrative production-shaped baseline

Assumptions: one region, Cloud Run API/workers at variable moderate load, Cloud SQL HA with backups/PITR, small Memorystore, load balancer/Armor/CDN, Pub/Sub, 100–500 GB database/storage combined, 50–200 GB egress/month, moderate observability. A rough planning range is **US$1,000–5,000+/month** before PayPal commercial charges, AI inference, support, compliance and multi-region disaster recovery. 10k concurrent / 2k RPS can push compute, database and telemetry well above this; no fixed quote is reliable without load tests.

## Main cost drivers

1. Cloud SQL size, HA standby, storage growth, backups/PITR and cross-region replication.
2. Cloud Run minimum instances and CPU/memory/instance duration, plus max scaling under burst.
3. AI model input/output tokens and retries (set per-user budget and model routing).
4. Logging/tracing ingestion and retention, especially payload/log cardinality.
5. Network egress, CDN/Armor/load balancing and provider/model outbound traffic.
6. Memorystore uptime/size, Pub/Sub retention and high worker concurrency.
7. Production operational, compliance, audit, penetration testing and incident response costs not shown as cloud bill.

Use budgets, billing alerts, max-instance limits, log exclusions/retention, AI token quotas and a demo teardown checklist. Do not turn on production HA/global services automatically for a hackathon demo.

