# Ledger design

## Accounting model

Use immutable double-entry journal transactions in integer minor units. Each journal transaction is denominated in exactly one currency and satisfies `SUM(debits) = SUM(credits)`; each posting amount is positive and each entry has exactly one debit/credit side. Do not use binary floating point. The ledger is authoritative; wallet balances are projections for efficient reads and must be rebuildable.

An example internal transfer of 1,200 USD cents from customer A to customer B:

| Account | Debit | Credit |
|---|---:|---:|
| A customer payable | 1,200 | 0 |
| B customer payable | 0 | 1,200 |

A PayPal-funded capture may debit `paypal_clearing` and credit customer payable (subject to the actual legal/product fund flow). Provider fee is separately debit expense/fee and credit clearing or payable according to statement facts. Account normal balance and account class are explicit; “debit means money left” is not assumed globally.

## Core schema

See [DATA_MODEL.md](DATA_MODEL.md). `journal_transactions` has source type/ID, effective/recorded timestamps, idempotency operation and reversal reference. `journal_entries` is append-only with account, currency, debit_minor, credit_minor. `accounts` have owner and type. Posting is through one domain service/stored procedure boundary; DB roles deny application update/delete on posted rows. Deferred constraint trigger or equivalent transaction-final validation enforces balance. Unique `(source_type, source_id, posting_kind)` prevents duplicate economic postings.

## ACID posting algorithm

1. Begin transaction and claim idempotency row.
2. Lock account rows in sorted ID order; validate active state, currency, limits and available balance from current posted balance/holds.
3. Insert journal transaction and at least two entries.
4. Validate exact per-currency sum zero and references; append audit event/outbox.
5. Update materialized balance projection/version in the same transaction.
6. Commit all or rollback all.

Enforce arithmetic/range bounds with PostgreSQL `BIGINT` or `NUMERIC(precision,0)` and explicit limits. Avoid SQL `SUM` overflow by capacity bounds and appropriate `NUMERIC` aggregate. Currency exponent comes from a controlled currency table/snapshot; provider currency must match the intent.

## Invariants and tests

- Every committed transaction balances per currency; zero/negative entry values rejected.
- Debits cannot overdraw eligible customer liability; holds reserve but do not post captured value.
- A transfer conserves total customer liabilities and has exactly paired source/destination entries.
- Refund total never exceeds settled captured amount; refund has reverse postings linked to original capture.
- Reversal is equal-and-opposite against original account/currency structure and references original transaction; no deletion.
- Idempotent same key/same canonical request returns the original result; same key/different request conflicts.
- Concurrent debits on one account serialize so the account cannot overspend; inter-account locks have stable ordering.
- Rebuilt projections from journal equal stored projections at a defined sequence/cutoff.
- Effective time is business event time; recorded time is immutable database knowledge time. Historical-as-known queries filter both dimensions.
- Property-based/randomized posting sequences, concurrent integration tests on real PostgreSQL, rollback injection, migration/replay tests and invariant audits run in CI.

## Corrections and holds

Corrections append a new journal transaction and correction record. A correction changing historical effective amount must preserve both original and restatement/adjustment history, with operator role/reason and approval as policy requires. Existing Stage 3 semantics (effective vs recorded time) are retained conceptually. Authorization holds live in a separate reservation table with remaining amount and expiry; capture converts reserved amount to journal postings atomically, void/expiry release reservation without postings. If provider auth is involved, provider authorization state must also reconcile.

## Financial boundary

Never trust client balance or AI-generated ledger instructions. AI cannot reach SQL. Every financial command arrives as a typed domain command and is independently authorized, validated, idempotent and posted transactionally. A ledger is not a substitute for licensing, safeguarding, finance controls or reconciliation.

