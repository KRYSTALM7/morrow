# ADR 006: Use Cloud Run for initial GCP runtime

- **Status:** Accepted as initial deployment choice
- **Context:** Stateless HTTP APIs and workers fit managed container runtime; team wants cost-conscious hackathon deployment without operating Kubernetes.
- **Decision:** Deploy API, webhook ingress and workers as Cloud Run services/jobs with separate service accounts and scaling limits. Cloud SQL PostgreSQL is authoritative; Pub/Sub carries asynchronous events.
- **Consequences:** Fast deployment and autoscaling; max instances, concurrency, DB pool and connection budgets must be coordinated. Cold starts and regional/database dependencies remain. GKE can be reconsidered only if capabilities justify the operational cost.
- **Alternatives:** GKE (more control/operations than needed); Compute Engine (more patching); all services in one always-on VM (poor isolation and scaling).

