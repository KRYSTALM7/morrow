# ADR 002: PostgreSQL is the financial source of truth

- **Status:** Accepted for target design
- **Context:** In-memory state is volatile and instance-local. Money commands require durable atomicity, unique idempotency, locking and audit history.
- **Decision:** Use Cloud SQL PostgreSQL with SQLAlchemy 2.x/Alembic; immutable balanced journal entries are authoritative. Keep balance projections rebuildable. Apply short ACID transactions, database uniqueness and row locking.
- **Consequences:** Strong relational constraints and mature operations; connection capacity, lock contention, migrations and HA cost require careful sizing. Redis is not authoritative.
- **Alternatives:** Document database (weaker relational accounting constraints); Redis or event log alone (not selected as ledger source); sharding early (unjustified complexity).

