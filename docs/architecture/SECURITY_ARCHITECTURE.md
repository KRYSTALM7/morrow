# Security architecture

## Identity and access

For a production browser, prefer a same-site backend-for-frontend session in `Secure`, `HttpOnly`, appropriately `SameSite` cookies plus CSRF token/origin checks. If a mobile/API bearer token is needed, use short-lived signed access tokens with issuer/audience/algorithm allowlist and key rotation, plus rotating refresh-token families stored hashed server-side, reuse detection and revocation. Do not perpetuate the prototype's never-expiring in-memory bearer token. Passwords use Argon2id (or approved equivalent) with per-record salts and tuned parameters; support breached-password screening, recovery, email verification and MFA/step-up for sensitive actions.

Authorize every object access by authenticated user/tenant ownership. RBAC is minimum (`customer`, `support_readonly`, `finance_operator`, `security_admin`, service identities); use separation of duties and dual approval for high-risk adjustments. Derive tenant and actor from verified principal, not caller-supplied IDs. PostgreSQL row-level security can be defense in depth, not a substitute for application authorization.

## Application and API

- TLS at edge and encrypted internal service paths where supported; managed encryption at rest; field-level encryption/tokenization for sensitive PII where justified.
- Strict Pydantic schemas, allowlisted fields, bounded request sizes, parameterized SQL/SQLAlchemy, transaction isolation, no dynamic SQL from model output.
- CORS allowlist only for known origins; CSRF defenses for cookie auth. CSP, output encoding, secure headers and dependency updates. Rate limits by IP, user, endpoint and idempotency key.
- SSRF: no user/model-controlled arbitrary URL fetch; provider endpoints fixed by environment; egress restricted; validate webhook/proxy headers and redirect behavior.
- Idempotency abuse: authenticated scope, key size/format, request hash, retention, quota and per-user rate limits; unique DB constraints.
- Data isolation: tenant predicates on repositories and tests; audit cross-tenant denial. Minimize PII in prompts, logs, events and analytics; retention/deletion policy must distinguish statutory ledger retention from profile data.

## PayPal webhook and secrets

Verify PayPal signature using official verification process and expected webhook ID/environment; validate event and resource IDs; dedupe provider event IDs; enforce timestamp/replay window where available while allowing legitimate provider retries. A webhook is not authenticated merely because it has a secret-looking field. Persist raw body only when justified, encrypted, access restricted, and with retention; otherwise store digest/normalized minimal fields.

Use Secret Manager for PayPal client secrets, model key and DB credentials. Use separate service accounts per API/worker/deploy. Never commit `.env`, credentials, tokens or sandbox secrets. Rotate secrets; prefer workload identity and managed DB auth. Redact authorization headers, cookies, provider tokens, webhook payload PII, passwords and financial details from logs/traces.

## AI boundary

Treat user text, notes, imported descriptions, provider metadata and model output as untrusted. The model cannot query arbitrary records or invoke arbitrary URLs/SQL. Every tool has narrow schema, current-principal scoping, output minimization and deterministic authorization. Explicitly confirmed proposal digest gates sensitive action; no model output is consent.

## Containers, pipeline, OWASP API risks

Build minimal non-root images; pin base image digest in production; read-only filesystem where compatible; drop Linux capabilities, resource limits, provenance/SBOM/signing. Scan dependencies/container and secret leaks in CI; protect migration and production deploy identity. Separate deployer from runtime identities.

Map OWASP API Security Top 10 controls: object-level authorization (every ID lookup), broken authentication (lifecycle/MFA), property-level authorization (explicit schemas), resource consumption (quotas/body limits), function-level authorization (operator scopes), sensitive business-flow abuse (velocity/risk controls), SSRF (restricted egress), misconfiguration (IaC review), inventory/version (API catalog/deprecation), unsafe third-party APIs (timeouts/signature/idempotency/reconciliation). Conduct threat modeling, SAST/DAST, penetration test and incident-response exercises before real funds.

