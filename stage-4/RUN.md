# Morrow Stage 4

Stage 4 extends Stages 1–3 with receiver refunds and operator correction batches,
including whole-settlement corrections. Stage 3 added timestamped payment history,
historical wallet balances, paginated statement snapshots, immutable payment revisions and
corrections. It accepts export documents from the Stage 1, Stage 2 and Stage 3 services. The
runtime uses Python's standard library and bundled HTML, CSS and JavaScript assets. PayPal
Sandbox checkout operations, recurring commitments and shared-expense records use SQLite.
These financial planning records are not wallet transfers.

The authenticated finance API supports `POST/GET /commitments`, `GET/PATCH /commitments/{id}`,
`GET /commitments/{id}/occurrences`, `POST /commitments/{id}/occurrences/{occurrence_id}/pay`,
and `POST /commitments/{id}/{pause|resume|cancel}`. Shared expenses use
`POST/GET /shared-expenses`, `GET /shared-expenses/{id}`, and
`POST/GET /shared-expenses/{id}/settlements`. All finance writes require an
`Idempotency-Key`. See [`FINANCE_API.md`](FINANCE_API.md) for payloads and balance semantics.

Forecasts use `GET /forecast` (also returned by `GET /safe-to-spend`) and
per-user settings at `GET/PATCH /forecast/settings`. A PATCH requires an
`Idempotency-Key`. Forecasts need at least three complete prior calendar months
of wallet history; otherwise the service returns an explicit unavailable state.
They cover only the durable Morrow wallet ledger, not external accounts or
cash. The current ledger has no transaction categories, so variable-spend
category breakdowns are not fabricated.

Build and run:

```sh
docker build -t morrow-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 -v morrow-data:/data --env-file .env morrow-stage-4
```

The service listens on `0.0.0.0:$PORT` (default `8080`). PayPal checkout needs outbound
HTTPS access to the PayPal Sandbox API. The named volume stores wallet state, provider checkout
operations, webhook deduplication, finance records, and audit rows. Wallet state is stored in
`MORROW_WALLET_DB_PATH`, separate from the PayPal and finance files; do not point these stores at
the same SQLite file. Commitment payments are user-recorded
against occurrences and do not move wallet funds. Shared-expense reimbursements are separate
records and do not alter wallet balances; PayPal orders are not automatically reconciled to them.
Copy `.env.example` to `.env`, add Sandbox app credentials, and set
`POCKETFUL_PUBLIC_URL` (legacy environment variable retained for compatibility) to a URL PayPal's buyer browser can reach (localhost for local checkout).
The financial companion can optionally use Gemini: set `GEMINI_API_KEY` on the server,
and optionally choose `GEMINI_MODEL` and `GEMINI_TIMEOUT_SECONDS`. Without a valid key or
when the provider is unavailable, the assistant falls back to verified deterministic context.
The runtime uses Python's standard library; no Gemini client dependency is required.
Configure the webhook listener URL as `https://<public-host>/webhooks/paypal` and paste its
PayPal Webhook ID into `PAYPAL_WEBHOOK_ID`. A public HTTPS tunnel is needed for Sandbox webhook
delivery to a local server.

Wallet persistence uses schema migrations 3 and 4. Bearer credentials are never included in
the wallet snapshot; `wallet_sessions` stores only SHA-256 token hashes and session ownership,
expiry, and revocation metadata. Existing version 2 databases are migrated at startup: legacy
token mappings are converted to session hashes and the state snapshot is rewritten without tokens.
SQLite then checkpoints/vacuums the active database and truncates WAL to remove legacy plaintext
from database pages and WAL before the service starts accepting requests; a separate migration
marker causes cleanup to retry after an interrupted scrub. Balances, holds, transactions, and finance
records are left intact. The application does not manage SQLite backups. If backups or filesystem
snapshots were made before this migration, treat them as containing bearer credentials and retire
them according to your backup retention and security procedures.

For an upgrade from a running pre-persistence Stage 4 process, export its wallet state before
stopping it. Place the export JSON in a private file and set `MORROW_WALLET_MIGRATION_FILE` for
the first start with an empty wallet database. The application validates and imports the file
inside a transaction and leaves the source file untouched. Do not configure that variable after
wallet state has been initialized. If the prior process has already stopped without an export,
its in-memory wallet cannot be recovered. Back up all SQLite files and their WAL state with
SQLite-aware online backup tooling, and test restores before upgrades.

To check service health without PayPal credentials:

```sh
docker run --rm --detach --network none --name morrow-stage-4-offline -e PORT=8080 morrow-stage-4
docker exec morrow-stage-4-offline python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"
docker stop morrow-stage-4-offline
```

For local development, run `python app.py` from this directory. Backend tests use:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

For browser tests, install the Chromium browser once with `python -m playwright install chromium`.
Pytest and Playwright are test-only dependencies and are not installed in the runtime image.
