# 0020 — Ports and adapters at every external boundary

**Status:** Accepted
**Date:** 2026-06-11
**Owner:** Kyle Couse (drafted by Claude in the ports/adapters/security refactor)

## Context

The agent must promote local → AWS/Azure/GCP and pre-dev → dev → prod with zero
code change (the refactor's north star, CLAUDE.md). Before this ADR, provider
touchpoints were scattered: each agent built its own `Anthropic` client,
retrieval/ingest/tools each carried their own Voyage calls and SQL, graph.py
owned the checkpointer connection, and Langfuse calls were inlined at every
trace site. Supporting a second cloud would have meant editing agent logic —
the definition of wrong under ADR 0019's profile model.

## Decision

Every boundary where the system touches the outside world is a port — a
`Protocol` in `src/rra/ports/` with a profile-resolved, process-singleton
factory — and all provider-specific code lives in `src/rra/adapters/`. The
eight ports are: LLM, embeddings/rerank, vector store, tool transport,
memory/state, observability, identity/NHI, and guardrails/policy (the last two
are specified in ADR 0021 and ADR 0022). Agent logic is written once against
the ports; `RRA_PROFILE` (ADR 0019) selects the adapter set.

## Wire-type decisions (the load-bearing subtlety)

A port abstracts the *boundary*, not necessarily the *data shape*. Three
deliberate choices:

- **LLM port abstracts client construction, not message shapes.** Agents speak
  the Anthropic Messages API directly (ADR 0003), and that API surface is
  itself cloud-portable: the anthropic SDK ships `Anthropic`,
  `AnthropicBedrock`, and `AnthropicVertex` with one `.messages.create`
  surface. `LLMPort.complete(**kwargs) -> Message` keeps `anthropic.types` as
  wire types by design. Abstracting message/tool-use shapes would rewrite all
  four agents for zero portability gain.
- **Vector store port takes raw embeddings + policy, never SQL.** Callers pass
  `(embedding, top_k, guidance_ids)`; SQL, vector-literal formatting, and index
  details belong to the adapter. (The first implementation cut passed
  caller-built SQL through the port — rejected in review as a connection
  factory, not a boundary.)
- **Tool transport is a single stringly chokepoint:**
  `call_tool(tool, arguments)`. This is the MCP-native call shape (a remote
  MCP adapter implements it unchanged), and a single dispatch point is where
  the identity port enforces deny-by-default tool scoping (ADR 0021). Typed
  tool results remain the wire types.

## Alternatives considered

- **LangChain/LiteLLM-style provider abstraction layers** — Rejected; ADR 0003
  already rejected framework wrappers for opacity and churn, and the Anthropic
  SDK's own Bedrock/Vertex clients make a second LLM abstraction redundant.
- **Abstract base classes instead of Protocols** — Rejected; Protocols keep
  adapters dependency-free of the ports package and match the codebase's
  structural-typing style.
- **One mega-`Platform` interface** — Rejected; per-boundary ports let clouds
  mix adapters (e.g., Bedrock LLM + self-hosted pgvector) and keep diffs small.
- **Dependency-injection container** — Rejected as over-engineering; eight
  `lru_cache` factories reading `settings.rra_profile` are sufficient and grep-able.

## Consequences

**Enables:**
- Cloud profiles (AWS/Azure/GCP) as adapter sets + config only (ADR 0019).
- The security spine: identity and guardrails are ports with the same
  swap-by-profile mechanics as everything else (ADR 0021, 0022).
- Swapping observability (Langfuse → LangWatch) without touching trace sites.

**Constrains:**
- New external touchpoints MUST come in through a port; a direct SDK import in
  agent logic is a review-blocking defect. Enforced by grep gates in tests/CI.
- Port surfaces are caller-driven and minimal; adapters may not grow methods no
  caller uses.
- Exemption: the eval harness's Langfuse dataset features
  (`evals/run.py`, `evals/langfuse_eval.py`, `evals/judge.py` tracing) stay
  direct — the harness is local-only tooling and Langfuse-specific by intent.

**Reopen if:**
- A target platform cannot satisfy a port's contract (e.g., a managed runtime
  that forbids self-managed checkpointing) — that's a port-shape problem, not
  an adapter problem.
- Anthropic SDK drops the Bedrock/Vertex client parity the LLM port relies on.

## Related

- ADR 0001 (LangGraph), 0003 (Anthropic SDK direct), 0011 (MCP tools
  in-process), 0019 (RRA_PROFILE), 0021 (identity/NHI port), 0022
  (guardrails port); docs/refactor/00-master-plan.md.
