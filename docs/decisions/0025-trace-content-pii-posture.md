# 0025 — Trace-content / PII posture: metadata, never confidential content

**Status:** Accepted
**Date:** 2026-06-15
**Owner:** Kyle Couse (drafted by Claude during the Langfuse trace review)

## Context

A review of live Langfuse traces (a happy-path `/query` trace = 47 observations, a
failed query, a `security.guardrail_block` one-shot) checked what actually lands in the
trace store. Findings:

- **Good already:** the `search_corpus` retriever span records **metadata only**
  (`guidance_id`/`chunk_index`/`title`/`score`) — no passage text. LLM generations record
  **token usage only** (no prompts/completions), so retrieved passage text never reaches a
  trace. No secrets (`sk-ant`/Voyage/RRA keys/Postgres DSN) appear anywhere.
- **The one real exposure:** `api.py` put the full **`product_context`** into the `query`
  span input. For a pre-submission device that is **confidential commercial information**
  (the device strategy + timing) — exactly the gap RT-redteam.md RT-4 flagged
  ("decide and document the PII posture for query/product_context in traces").
- **Inconsistency:** `anthropic:researcher` generations record I/O (the reformulated search
  query); `anthropic:{planner,analyst,critic}` record token usage only. No policy was
  written down for what generations may carry.

This ADR sets one rule for trace-payload content. (The duplicate `search_corpus` span and
the analyst-revision mis-nesting are separate items tracked in `docs/refactor/trace-cleanup.md`.)

## Decision

**Trace payloads carry metadata and derived/non-confidential data, never raw confidential
or untrusted content.** Concretely:

1. **`product_context` is never stored as text in a trace** — record only its size
   (`product_context_chars`). Implemented at the `api.py` `query` span.
2. **The user `query` IS kept** — it's a general regulatory question (not device CCI) and is
   needed for the trace to be useful. Accepted, documented exposure (self-hosted Langfuse at
   `langfuse_host`).
3. **Retrieved passage text is never stored** — the single canonical `search_corpus` span
   (at the retrieval boundary) stays `guidance_id`/`chunk_index`/`title`/`score` only.
4. **Generations record token usage + PII-safe I/O only** — never raw prompts (which embed
   passages + `product_context`) or completions that could carry CCI. The researcher's
   reformulated-query I/O is a derived search string and is permitted; planner/analyst/critic
   stay usage-only. The invariant is the rule, not the exact fields: **no passages,
   no `product_context`, no raw prompts on any observation.**
5. **Blocked content never reaches traces** (ADR 0024) — only the metadata
   `security.guardrail_block` score. Unchanged.

## Alternatives considered

- **Hash `product_context` instead of a char count** — Rejected: a hash is still derived from
  CCI and invites "is value X present" probing; the char count is enough to see that context
  was supplied and how large, with zero recoverable content.
- **Redact the `query` too** — Rejected for the self-hosted local profile: the question is
  what makes a trace navigable and is not device-specific CCI. Revisit if a multi-tenant /
  managed Langfuse replaces self-hosting (reopen trigger).
- **Record full prompts/completions on every generation for debuggability (the
  "consistency" fix)** — Rejected: that would push passage text + `product_context` into the
  trace store, directly violating items 1–4. Consistency is achieved by recording *less*
  (usage-only), not more.

## Consequences

**Enables / preserves:**
- Traces stay debuggable — query, full span tree, per-model token cost, citation-check
  results, the critic loop — with **no confidential content**.
- Closes the RT-4 `product_context` gap; the threat-model entry can cite this ADR as the control.

**Constrains:**
- You cannot read the exact `product_context` or the raw LLM prompts in Langfuse. Acceptable:
  prompt debugging is rare and can be done locally; CCI confidentiality outranks it.
- Any new instrumentation must honor the invariant (no passages / `product_context` / raw
  prompts) — enforce in review.

**Reopen if:**
- Observability moves to a managed/multi-tenant backend (tighten further — redact the query,
  consider field-level encryption).
- A real debugging need requires prompt capture → add a gated, access-controlled debug mode
  rather than relaxing the default.
