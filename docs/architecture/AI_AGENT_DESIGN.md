# AI payment agent design

## Safety contract

The model interprets and explains; deterministic services decide and execute. The model has no database credentials, provider credentials, balance mutation tool or generic HTTP/browser tool. AI outage never blocks payments initiated through ordinary deterministic APIs.

## Request flow

1. Authenticate user and establish tenant/permissions before context retrieval.
2. Apply per-user/IP rate limits, text byte cap and sensitive-data filter. Do not send passwords, session tokens, full payment instrument data or unrelated PII to the model.
3. Resolve only authorized handles/activity summaries. Treat user text and retrieved notes as untrusted data, not instructions.
4. Call the model with a versioned prompt and constrained JSON schema. Output is a *proposal* (`intent`, entities, amount_minor, currency, participants, missing_fields, explanation, confidence); reject unknown fields, invalid enum, non-integer amounts, unsupported currency, ambiguous identity, excessive amount, or low confidence.
5. Backend validates ownership, authorization, permissions, recipient uniqueness, currency, rounding, limits, duplicate requests and risk policy. Ask clarifying questions when required.
6. Present a canonical preview and digest to user. Sensitive action confirmation binds actor, exact intent + digest, amount, recipients, expiry, policy version and idempotency key. Material plan change invalidates confirmation.
7. Execute through typed domain application command; recheck permission, limits and state at commit. Record prompt/model version, proposal digest, tool request/result classes and consent/audit references while minimizing retained raw text.
8. Explain resulting status using authoritative transaction data; clearly distinguish pending vs captured vs failed.

## Tool allowlist

Read tools: `get_current_user_summary`, `list_pending_payments`, `get_payment_status`, `get_statement_summary`, `explain_error` (safe, scoped, redacted). Proposal tools: `preview_split`, `preview_payment_request`, `preview_reconciliation` return typed plans only. Execution tool is an application-level `confirm_proposal(proposal_id, confirmation_digest, idempotency_key)` gated by backend consent and policy; do not expose direct `capture`, `refund`, `settle` LLM tools. Refund/reconciliation adjustments require normal UI flow and stronger role/step-up approvals.

## Guardrails and operations

- Prompt injection: system prompts are not security controls. Isolate instructions from data, delimit context, never obey embedded instruction-like notes, tool-call allowlist, server-side ACL checks, output schema validation, and adversarial red-team tests.
- Model provider abstraction; pin prompt version and model configuration. Structured output/function calling is an interface aid, not trusted validation.
- Request timeout, bounded retry (no retry on non-idempotent execution), output/token budget and concurrency quotas. Circuit breaker returns deterministic UI/manual path.
- Safety fallback: ask user to use regular payment form or explain AI is unavailable. Never invent current balances; read authoritative backend data.
- Auditing: proposal and decision IDs, policy outcome, confirmation/actor, execution operation ID, model/prompt version, latency/error category. Avoid logging raw conversation by default; apply retention and user consent to any stored transcript.
- Abuse: per-user/IP rate limits, daily token/spend budget, model input cap, anomaly monitoring, tool call caps and low-confidence escalation.
- Gemini can be the initial provider through a small `ModelProvider` interface; select exact model/API and regional/privacy terms during implementation. Provider output never participates in financial atomicity.

