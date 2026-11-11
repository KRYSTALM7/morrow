# API and event design

Use `/v1`, JSON, UTC ISO-8601 timestamps and integer `amount_minor` plus ISO 4217 `currency`. Authentication and caller identity come from verified session/token. All mutating endpoints require `Idempotency-Key`; errors carry stable code, safe message, request ID. Pagination uses opaque cursor and stable snapshot semantics where needed.

## Representative API

```http
POST /v1/payment-intents
Authorization: Bearer <short-lived-access-token>
Idempotency-Key: 5c06...
Content-Type: application/json
```

```json
{"kind":"paypal_checkout","amount_minor":12000,"currency":"USD","description":"Dinner split","payee_reference":"user_..."}
```

```json
{"intent_id":"pi_...","state":"CREATED","amount_minor":12000,"currency":"USD","version":1}
```

Other endpoints: `GET /v1/me`, `GET /v1/payments?state=&cursor=`, `GET /v1/payment-intents/{id}`, `POST /v1/payment-intents/{id}/confirm`, `POST /v1/payment-intents/{id}/cancel`, `POST /v1/payment-intents/{id}/refunds` (step-up/role policy), `POST /v1/assistant/proposals`, `POST /v1/assistant/proposals/{id}/confirm`, `GET /v1/ledger/entries`, `POST /v1/reconciliation-runs` (operator), `GET /v1/reconciliation-runs/{id}`. PayPal webhook is a provider-specific ingress route; never let caller choose a user ID in place of ownership scope.

## Error example

```json
{"error":{"code":"idempotency_key_reuse","message":"This key was used for a different request","request_id":"req_..."}}
```

Use 401 for missing/invalid identity, 403 for denied role/ownership where disclosure is safe, 404 for intentionally hidden object, 409 for state/version/key conflict, 422 for invalid payload, 429 with `Retry-After` for quota and 5xx for transient server/provider uncertainty. Include authoritative current state in safe conflict responses where useful.

## Event envelope

```json
{
  "event_id":"evt_...","event_type":"payment.capture_succeeded","schema_version":1,
  "occurred_at":"2026-11-02T10:00:00Z","recorded_at":"2026-11-02T10:00:03Z",
  "aggregate":{"type":"payment_intent","id":"pi_...","version":5},
  "correlation_id":"req_...","tenant_id":"tn_...",
  "data":{"currency":"USD","amount_minor":12000,"provider":"paypal","provider_capture_id":"..."}
}
```

Events are versioned, minimal and contain no secrets/payment credentials. Pub/Sub is at least once; consumers must deduplicate `event_id` and guard aggregate version. Do not rely on global event order. Publish an event only from outbox committed with the domain transaction. Schema evolution is backward-compatible or new event version.

## Legacy compatibility

Stage routes use major-unit-independent integer minor values, but response shapes and opaque bearer tokens are prototype-specific. Maintain contract fixtures and an explicit adapter during migration; do not expose `/_test/reset`, `/_test/import` or `/_test/export` publicly. Import migration is an offline operator job with record counts, source digest, balance reconciliation and sign-off.

Stage 4 contract summary, confirmed from `stage-4/app.py` dispatch and the stage tests:

| Route | Contract / authority | Idempotency |
|---|---|---|
| `POST /auth/signup` | `email`, `password`, `display_name`; derives handle from email local part; returns user ID/display name/token | Not keyed |
| `POST /auth/login` | `email`, `password`; returns new opaque bearer token | Not keyed |
| `GET /me` | Authenticated caller only; reports integer `balance`, `total`, `available`, `held`, currency/minor units; `as_of` and `known_at` support historical views | Read |
| `POST /payments` | `amount` (integer minor units), `to_handle`, optional note/visibility; authenticated caller is sender | `Idempotency-Key` required |
| `POST /requests` | `amount`, `payer_handle`, optional note; caller is requester | Required |
| `POST /splits` | `amount`, unique `participant_handles`, optional note; server deterministically distributes remainder and creates payment requests | Required |
| `POST /requests/{id}/pay` | Payer only; posts transfer and marks request paid atomically in prototype state | Required |
| `POST /requests/{id}/decline|cancel` | Payer may decline; requester may cancel; pending state only | Not consistently keyed in legacy contract |
| `POST /authorizations` | `amount`, `to_handle`; reserves caller available balance, does not transfer | Required |
| `POST /authorizations/{id}/capture` | Recipient only; optional partial amount; records captured payment and reduces hold | Required |
| `POST /authorizations/{id}/void` | Owner/recipient/state rules; releases remaining reservation | Not consistently keyed |
| `POST /settlements` | Operator allowlist only; 1–32 `transfers` entries (`from_handle`, `to_handle`, `amount`) are atomic | Required |
| `POST /payments/{id}/corrections` | Original sender; expected revision, amount, effective timestamp, reason; append-only revision | Required |
| `POST /payments/{id}/refunds` | Original receiver; amount bounded by corrected payment less prior refunds; creates reverse payment | Required |
| `POST /correction-batches` | Operator allowlist; 1–32 distinct corrections; atomic in Stage 4 | Required |
| `GET /activity`, `/requests`, `/authorizations`, `/statement` | Authenticated; pagination/filtering; visibility and ownership rules; statement snapshot supports stable pages | Read |

Legacy contract caveats: amount fields are integers in minor units despite being named `amount`; currency is global to the wallet state (in memory in Stages 1–3 and persisted in SQLite in Stage 4); existing transfer semantics move simulated wallet balances, not PayPal funds. Some state-changing request decline/cancel and void actions have no idempotency key, a contract to improve in `/v1`. Stages 1–3 expose `/_test/*` routes without an environment gate. Stage 4 requires its legacy `POCKETFUL_ENABLE_TEST_ROUTES=1` configuration to enable test routes; leave it unset in any public deployment.

