# 0024 — Security-incident observability: surface guardrail blocks as a Langfuse score

**Status:** Accepted
**Date:** 2026-06-15
**Owner:** Kyle Couse (drafted by Claude in the ports/adapters/security refactor, Phase 3.1)

## Context

The guardrails port (ADR 0022) blocks injection at the `user_input` and
`retrieved_content` boundaries, and Phase 3 (ADR 0023) made the detector real.
But a block was only ever a structlog `warning` (`guardrails.blocked` /
`guardrails.passage_blocked`) — it never reached the observability backend.
Two structural facts caused this:

1. The observability port (ADR 0020) exposed only span tracing (`start_span`,
   `current_trace_id`, `flush`, `propagate_session`) — no score/event surface,
   so no call site *could* emit a security signal through the port.
2. At `user_input`, the block raises HTTP 400 *before* the request span is
   opened, so the incident had no trace to attach to even in principle.

The original requirement — "show the security incident in observability" — was
therefore unmet. RT-redteam.md deliberately keeps blocked **content** out of
traces (the self-hosted trace store is a confidential aggregation point), but
that rule bans the offending *text*, not the *fact* of detection. The two are
reconcilable: surface the incident as metadata, never the payload.

## Decision

Extend `ObservabilityPort` with one method:

```
record_security_event(*, boundary, categories=(), detector_score=None,
                       reason=None, location=None) -> None
```

The Langfuse adapter maps it to a **categorical `security.guardrail_block`
score** via the same `create_score` surface the eval harness already uses
(`langfuse_eval.emit_scores`), so caught injections are **filterable** in the
Langfuse Scores view. It is wired at both guardrail block sites (researcher
`retrieved_content`, api `user_input`).

- **Metadata-only, by contract.** Recorded: `boundary`, detector `categories`,
  `detector_score`, and a corpus `location` (`guidance_id#chunk_index`). NEVER
  the offending text — the method does not even accept it. An allow-list test
  pins the metadata keys.
- **Trace attachment.** When a live request trace is active (`retrieved_content`
  during `/query`) the score hangs on it, in context with the agent spans. When
  none is (`user_input` blocked pre-span) the adapter opens a one-shot
  observation so the incident has a home trace rather than an orphaned score.
- **Never raises.** Observability is auxiliary; a Langfuse failure is swallowed
  and logged (`observability.security_event_failed`), never propagated into the
  request path. The `Noop` adapter is a no-op (Langfuse disabled / CI).

This is an additive evolution of the ADR 0020 observability port, not a new
provider: it is implemented against the port, so cloud observability adapters
(and a hypothetical LangWatch adapter) implement the same method.

## Alternatives considered

- **A Langfuse event/span marker instead of a score** — Rejected: scores are
  first-class filterable and aggregatable; the demo and audit win is "filter
  every trace to the guardrail blocks," which a buried span annotation does not
  give as cleanly. (A one-shot span is still opened for the no-trace case, but
  the *signal* is the score.)
- **Record the block in existing span metadata, including category/snippet** —
  Rejected: violates RT-redteam's content-exclusion rule, and `user_input` has
  no span to annotate.
- **Adopt LangWatch for its security-monitoring UI** — Rejected: a new
  dependency and a second heavy observability stack alongside Langfuse cuts
  against CLAUDE.md's "local profile is fully self-hosted and free." The
  observability port already makes LangWatch a future adapter swap, so building
  against the port preserves that option for free. (The stale "LangWatch"
  strings in CLAUDE.md — which seeded the original question — are corrected to
  "Langfuse" in this change; ADR 0020's "Langfuse → LangWatch" remains a
  legitimate *hypothetical* swap example.)
- **Restructure api.py so the request span wraps the `user_input` check** —
  Rejected for now: more invasive to the session-propagation flow; the adapter's
  one-shot-trace fallback keeps call sites trivial and the change isolated.

## Consequences

**Enables:**
- Caught injections are visible and filterable in Langfuse under
  `security.guardrail_block`, with boundary/category/score/location metadata —
  the original "show the security incident" requirement, satisfied.
- A provider-portable security signal: cloud observability adapters implement
  the same port method; the agent/api call sites do not change per profile.
- An audit channel distinct from logs (scores aggregate in the Scores view).

**Constrains:**
- The method is metadata-only by contract; every emitter must pass machine
  labels, never raw text (review + the allow-list test enforce it).
- Adds a stable categorical-score namespace (`security.guardrail_block`).

**Accepted residual / scope:**
- Only the two guardrail boundaries emit today. The NHI tool-denial
  (`ToolAccessDenied`, ADR 0021) and the output-filter image-strip (RT-4) are
  natural next emitters through the same method (the `boundary` Literal widens);
  they are deliberately **not** wired here to keep the change reviewable.
- The `user_input` incident is a standalone one-shot trace, not nested under a
  request trace — acceptable, since a blocked request runs no graph to nest under.
- No eval impact: this touches runtime observability only, not the LLM-visible
  bytes or the detector, so the owed `citation_validity` re-run is unaffected.

**Reopen if:**
- Tool-denial or output-filter incidents need surfacing (widen the Literal + wire).
- A cloud profile lands: implement `record_security_event` for the native
  observability service behind this port.
- LangWatch is genuinely adopted as an observability adapter (its own ADR).
