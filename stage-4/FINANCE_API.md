# Stage 4 Finance API

These endpoints extend the existing authenticated Stage 4 API. All amounts are
integer minor units in the wallet's configured currency (for example, `120000`
means EUR 1,200.00 when the configured currency is EUR). Currency conversion is
not performed. IDs are opaque server-generated identifiers. Reads are scoped to
the authenticated bearer-token owner; request bodies cannot select an owner.

Every mutation requires an `Idempotency-Key` header. Repeating the same request
with the same key replays its original result; reusing a key for different input
returns `409 idempotency_key_reuse`. Validation errors return `422`, and an
unknown or non-owned record returns `404`.

## Commitments

- `POST /commitments`: create. Body: `title`, `category`, `amount_minor`,
  `frequency` (`weekly`, `monthly`, `annual`, or `custom`), `next_due_date`
  (`YYYY-MM-DD`); `interval_days` is required only for `custom`.
- `GET /commitments?status=active|paused|cancelled&limit=50&offset=0`: list.
- `GET /commitments/{id}` and `PATCH /commitments/{id}`: read or update allowed
  commitment fields. Schedule/amount changes update unpaid future occurrences;
  paid occurrences remain part of history.
- `GET /commitments/{id}/occurrences?status=unpaid|paid|paused|cancelled`: list
  scheduled occurrences.
- `POST /commitments/{id}/occurrences/{occurrence_id}/pay`: mark an occurrence
  paid with optional `payment_reference`.
- `POST /commitments/{id}/pause`, `/resume`, `/cancel`: change lifecycle state.
  Status endpoints accept an empty JSON object.

Create and update responses include occurrence records. Payment tracking is a
user-recorded commitment status; it does not move wallet funds or assert that a
bank transaction was observed. Paid occurrences are excluded from unpaid
commitment state. Occurrences and audit events retain the history of schedule
changes and payment status.

## Shared expenses and reimbursements

- `POST /shared-expenses`: body has `description`, `category`,
  `gross_amount_minor`, `personal_share_minor`, and `participants`, an array of
  `{ "handle": "bob", "share_minor": 800 }` entries. The personal share plus
  participant shares must equal the gross amount. Participants must be other
  existing users, with unique handles.
- `GET /shared-expenses?status=open|settled&limit=50&offset=0`: list.
- `GET /shared-expenses/{id}`: read expense, participant balances, settlement
  history and outstanding receivable.
- `GET /shared-expenses/{id}/settlements`: list settlement history.
- `POST /shared-expenses/{id}/settlements`: body has `participant_handle`,
  `amount_minor`, and optional `payment_reference`.

Settlement records are append-only. Each accepted partial receipt reduces only
that participant's outstanding amount; amounts beyond the outstanding share
are rejected atomically. The aggregate receivable is bookkeeping, not wallet
cash: it is not added to available funds, and a manual settlement record does
not change the wallet balance. PayPal merchant checkout creates no reimbursement
record. A PayPal payment is not associated with a shared expense unless a user
explicitly reconciles it through a future supported flow.

## Forecast and safe-to-spend

- `GET /forecast` and `GET /safe-to-spend` return the same authenticated user's
  current forecast result.
- `GET /forecast/settings` returns that user's savings target and safety buffer.
- `PATCH /forecast/settings` updates either or both of
  `savings_target_minor` and `safety_buffer_minor`. Both are non-negative integer
  minor-unit values; writes require `Idempotency-Key`.

The calculation returns `status`, `calculation_date`, current-month period,
currency, model version, confidence, data coverage, assumptions, category
coverage, unpaid commitment totals, forecast ranges, and the safe-to-spend
amount. If history is missing, incomplete, too short, or an essential input is
invalid, `status` is `unavailable` and `safe_to_spend_minor` is `null`; the
response contains machine-readable `unavailable_reasons`.

At least three complete prior calendar months are required. Up to six months of
wallet history are used; the monthly outflow estimate is their median, and the
remaining variable estimate is the median less this month's observed outflows,
floored at zero. The low/high range uses the lowest/highest comparable monthly
outflow. The service does not project future income. Current available funds
already exclude active holds, so holds are not deducted a second time. Active
unpaid occurrences due on or before month end (including overdue occurrences)
are included; paid occurrences are excluded. A paid occurrence's wallet payment
is excluded from variable history only when its `payment_reference` exactly
matches a wallet `payment_id`.

The formula is `available_funds_minor - unpaid_commitments_minor - forecast_remaining_variable_spend_minor - savings_target_minor - safety_buffer_minor`. A negative result is retained and reported as-is. Shared-expense receivables are not an input and never count
as available cash. Only this Morrow wallet ledger is observed; it does not
include external accounts or cash, and has no transaction category
field. Therefore variable category breakdowns are explicitly unavailable with
the current ledger schema; known commitment categories are shown separately.
Results and privacy-minimized aggregate inputs are stored with the model version
in `forecast_snapshots`.

## AI financial companion

- `POST /ai/companion`: authenticated, read-only financial question endpoint.
  Body: `{ "message": "..." }`, maximum 1,000 characters. Unknown fields are
  rejected. The endpoint is limited to 10 questions per user per minute.
- The response includes an explanatory `answer`, `provider` (`gemini` or
  `deterministic_fallback`), a `financial_context` snapshot, timestamp, and
  disclaimer. Context is built for the authenticated user only: wallet/holds,
  deterministic forecast and assumptions, upcoming commitment amount/date,
  shared receivable and pending request totals, and a short transaction summary.
  Transaction descriptions and counterparty identifiers are omitted.
- Gemini is optional and called only server-side. Configure `GEMINI_API_KEY`,
  `GEMINI_MODEL` (default `gemini-2.5-flash`), and
  `GEMINI_TIMEOUT_SECONDS` (default 12, capped at 20). Missing configuration,
  timeout, provider errors, rate limits, malformed responses, or numeric claims
  in generated prose produce a deterministic fallback. Provider errors and
  credentials are not returned to the caller.
- Gemini has no tools and cannot execute actions. Generated prose is explanatory
  and rejected if it includes numeric/currency claims; the application displays
  authoritative figures separately from deterministic server data. An
  unavailable Safe-to-Spend status and reason remain unavailable; no amount is
  inferred.
- Additional-spend scenarios are calculated server-side from the submitted
  amount and available deterministic Safe-to-Spend result. They are labeled
  hypothetical and never create a transaction.
- This endpoint does not alter `/ai/proposals`, which remains the separate,
  explicit-confirmation flow for supported payment/request actions.

## Persistence

Finance tables are created by the versioned `finance_schema_migrations` schema
in `finance_store.py`. By default the finance store shares `PAYPAL_DB_PATH`; set
`MORROW_FINANCE_DB_PATH` to choose another SQLite file. Back up the chosen file
and its WAL state using SQLite-aware backup tooling while the service is
running. Savings targets, safety buffers, coverage-start metadata, and forecast
snapshots are stored in SQLite. Core wallet balances and wallet transactions
are persisted in a separate versioned wallet snapshot database configured with
`MORROW_WALLET_DB_PATH`. Each wallet dispatch loads the latest snapshot and
serializes mutations inside a SQLite `BEGIN IMMEDIATE` transaction. The service
checks current balances against payment history and historical solvency before
saving. The wallet snapshot retains the compatible Stage 4 export structure;
session records are stored separately as token hashes with expiry and revocation.
