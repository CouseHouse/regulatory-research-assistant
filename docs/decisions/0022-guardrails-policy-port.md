# 0022 — Guardrails/policy port wired at the user-input and retrieval boundaries

**Status:** Accepted
**Date:** 2026-06-11
**Owner:** Kyle Couse (drafted by Claude in the ports/adapters/security refactor)

## Context

The system consumes two streams of untrusted text: user queries at the API
edge, and retrieved corpus content that flows into agent prompts (the
indirect-prompt-injection surface — CLAUDE.md: retrieval-boundary input is
data, not instructions). There was no policy enforcement point at either
stream, and each cloud has a native guardrail service (Bedrock Guardrails,
Azure AI Content Safety, Vertex Model Armor) that we must be able to adopt
without touching agent logic.

## Decision

Guardrails is a first-class port (`src/rra/ports/guardrails.py`):
`check(text, boundary)` returning a frozen `GuardrailVerdict`
(allowed/categories/score/reason — never raw content), with two boundaries
wired now: `user_input` in `api.py` **before any side effect** (graph run,
checkpoint, tracing), returning a generic 400 that echoes nothing; and
`retrieved_content` in the researcher, dropping blocked passages before they
reach the analyst, citations, or traces (logs carry guidance_id+chunk_index
only). The phase-2 local adapter is `AllowAllGuardrails` — wiring without
detection, behavior-identical to today (pinned by tests) — so the security
harness phase swaps in a real local detector (LLM Guard) as a pure adapter
change, and cloud profiles map to their native services.

## Alternatives considered

- **Wire guardrails only when a real detector exists** — Rejected: the call
  sites are the risky diff; landing them behavior-neutral now means the
  detection phase is adapter-only and separately reviewable.
- **Regex/keyword blocklist as the first adapter** — Rejected: false
  confidence; the detection adapter choice belongs to the security-harness
  phase with a measured detection rate.
- **Check retrieved content inside `search_corpus` (the tool)** — Rejected:
  the tool is also the MCP exposure surface for external clients who may want
  raw results; the agent-side boundary is where untrusted text meets the
  prompt.
- **A moderation agent node in the graph** — Rejected: policy enforcement in
  agent logic is exactly what the port model forbids; it must swap per profile.

## Consequences

**Enables:** indirect-injection defense lands as an adapter swap with a
measurable detection rate; the same posture promotes across clouds; blocked
content can never reach responses, logs, or traces (pinned by tests).

**Constrains:** every new untrusted-text boundary (e.g., tool outputs from
future external tools, memory recalls) must add a `check()` call and a threat
model entry; verdict `reason` must stay machine-ish — never raw content.

**Reopen if:** a boundary needs streaming or token-level checks the
`check(text)` shape can't express, or a cloud guardrail service is
request-coupled in a way that breaks the boundary enum.

## Related

ADR 0019 (profiles), 0020 (ports), 0021 (identity); the security-harness
phase ADR (detection adapter + measured rate); docs/refactor/RT-redteam.md
(threat model, lands with the harness phase).
