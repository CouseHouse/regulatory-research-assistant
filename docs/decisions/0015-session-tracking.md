# 0015 — Session tracking: persist and surface `session_id`

**Status:** Active
**Date:** 2026-06-02
**Owner:** Kyle Couse
**Amends:** 0008 — extends the `session_id` field from tracing-only to also
carrying a persistence role. ADR 0008's state-shape decision and every other
field contract remain Active and unchanged.

## Context

`session_id` already flows through the system: api.py mints it (`str(uuid4())`),
the graph uses it as the LangGraph `thread_id` (so the checkpointer already
persists per-node state keyed by it), and it tags every Langfuse trace and log
line. But it is never written to an application table, never returned to the
client, and regenerated on every `POST /query` — so no session is queryable or
continuable. The Day-1 schema anticipated exactly this (the `app` schema is
commented "sessions, audit"; `app.query_audit` already carries `session_id` +
`langfuse_trace_id` + a session index) yet the table is **dead — nothing writes
it**. We want the production-shaped persistence story the unwired scaffolding
implies.

## Decision

We will promote `session_id` from a tracing-only correlation ID into a
first-class, persisted, client-visible session entity — adding an `app.sessions`
parent table, writing the existing `app.query_audit`, returning (and optionally
accepting) `session_id` on `/query`, and linking queries, traces, and eval-runs
to it — while deferring the user-identity framework to ADR 0016.

## Alternatives considered

- **Stay stateless; keep `session_id` tracing-only** — Rejected. Leaving the
  `app.query_audit` / "sessions, audit" scaffolding unwired reads as incomplete,
  not as a deliberate v1 cut; history and per-session attribution are cheap given
  the identifier already flows.
- **Bundle session + user tracking into one change** — Rejected. User identity
  drags in auth, PII/retention, and multi-tenant boundaries — a different weight
  class; bundling stalls the concrete session win behind the expensive identity
  design. Deferred to ADR 0016 (gated on the Day-11 `identity-design.md` pass).
- **Use the LangGraph checkpoint (`langgraph` schema) as the session store** —
  Rejected. Checkpoints are opaque per-node state blobs for durability/HITL; they
  cannot answer "all queries in session X." Session history needs an
  application-level entity, not the checkpointer.
- **Make Langfuse sessions the system of record** — Rejected. Langfuse is
  observability, self-hosted as the regulated-vertical narrative (§4.8); making
  the trace store authoritative for application state inverts the dependency.
  Langfuse `sessionId` may *mirror* our id, never own it.

## Consequences

**Enables:**
- Query-history retrieval and per-session cost / eval attribution.
- A real anchor for the Day-11 identity design (users own sessions → ADR 0016).
- Multi-turn / session-scoped context later, without a new identifier.
- Activates the latent `app.query_audit` + `langfuse_trace_id` link.

**Constrains:**
- `api.py` is no longer write-free on the `app` schema — every `/query` writes
  `app.sessions` + `app.query_audit`, adding a hot-path DB write.
- **Audit writes are fail-open.** A `/query` must **not** fail because the
  `app.sessions` / `app.query_audit` write errored — the answer path takes
  priority over the audit path. A failed audit write is logged **loudly** as a
  structured error (e.g. `audit.write_failed`, with `session_id`) so a dropped
  session record is visible, never silent. Rationale: observability/persistence
  must never take down the product answer — especially for a regulated-vertical
  tool where the grounded answer is the deliverable, and a missing audit row is a
  recoverable gap, not a user-facing failure.
- `QueryResponse` gains `session_id` and `/query` gains an optional inbound
  `session_id` — additive frozen-contract changes (the ADR-0008 `warning`-field
  pattern) plus validation of caller-supplied ids.
- Storing query text + `product_context` against a durable session sharpens the
  PII/retention surface *before* user identity even lands.

**Reopen if:**
- Session writes contend on the hot path (move to async / a write queue), or the
  fail-open policy hides a systemic audit outage (add a write-failure alert).
- ADR 0016 (user identity) forces a sessions-table reshape (e.g. a `user_id` FK
  or tenant key) this schema didn't anticipate.

## What changed (amends ADR 0008)

ADR 0008 documents `session_id` as written by api.py and read by "all nodes
(tracing)" — i.e. tracing-only. That characterization no longer fully holds: this
ADR adds a persistence writer (api.py → `app.sessions` / `app.query_audit`) and a
reader (the history-retrieval path). What forced it: the portfolio now wants a
real persistence/history surface, and the identifier + table scaffolding already
exist, so the cost is small. Everything else in ADR 0008 — the state-shape
decision, single-writer ownership, and every other field contract — remains valid
and Active; only `session_id`'s scope is *extended*, not redefined.

The user-identity framework this opens onto (a `users` table, per-user session
association, PII handling, the Langfuse `userId` question, and the auth mechanism)
is **explicitly out of scope here** and recorded as a consequence requiring its
own decision: **ADR 0016 (proposed)**, to be taken up with the Day-11
`identity-design.md` pass.

## Related

- ADR 0008 (LangGraph state shape) — amended here; state-shape decision intact.
- ADR 0004 (data-access layer / pool) — the write path for the new tables.
- ADR 0012 (eval-harness scoring) — eval-runs gain a `session_id` link.
- **ADR 0016 (proposed)** — user-tracking framework (identity, users table,
  PII/retention, Langfuse `userId`, auth mechanism). Not yet written.
- spec.md §4.9 (identity, deferred), §5 (data flow / "session"), §8 (out of scope).
- `docs/plan/day4-design.md:395` — reverses "v1 is stateless from the API client's
  perspective. Each `POST /query` is a new session."
- `docs/future-work.md` §1 (OAuth), §3 (multi-tenant), §6 (UI).
- `init-db/01-init.sql` — `app` schema, `app.query_audit` (activated by this ADR).
