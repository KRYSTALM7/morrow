# ADR 004: Make PayPal a first-class adapter

- **Status:** Accepted for hackathon target; production product validation required
- **Context:** Hackathon requires real PayPal Sandbox flow and future provider flexibility. Provider schemas and failure semantics must not leak into ledger.
- **Decision:** Define provider port for order creation, approval retrieval, capture, refund and webhook verification. Implement official PayPal Orders flow, stable request IDs, hosted approval and reconciliation; store normalized provider identifiers in orchestration domain.
- **Consequences:** Enables Sandbox demo and future adapter; PayPal account/product, geography, fees and permissible funds flow require validation before live use. This is not an assertion that PayPal offers a general custodial wallet.
- **Alternatives:** Fake provider (rejected); couple core ledger directly to PayPal fields (limits expansion); choose multiple providers before need (unnecessary scope).

