# ADR 005: AI proposes; deterministic services authorize and execute

- **Status:** Accepted
- **Context:** Natural language improves bill splitting and financial explanations, but LLM output is probabilistic and prompt-injectable.
- **Decision:** AI produces validated proposal schemas and read-only explanations. Backend enforces actor permissions, policy, limits, exact user confirmation and idempotent execution. No direct database/provider access.
- **Consequences:** AI outage does not affect core payments; user review adds a step. Prompt/model version, consent and decision audit require privacy-aware retention.
- **Alternatives:** LLM executes transfers directly (unsafe); omit AI (misses product goal); model-generated advice treated as financial truth (rejected).

