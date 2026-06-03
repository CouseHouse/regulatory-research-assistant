# Day 10 — Session tracking (persistence)

> Inserted after the cloud demo (Option B, ADR 0015). This is a **new** day; the
> original Day 10–14 (design docs → buffer) shift to Day 11–15. See `plan.md`.

## Goal

Promote the `session_id` that already flows through the system (api.py →
LangGraph `thread_id` → Langfuse → logs) into a first-class, **persisted,
client-visible** session entity, and activate the dead `app.query_audit`
scaffolding. Full day — it ships with its own tests + an eval pass (the
"run evals before done" rule), because it changes the `/query` contract and adds
a hot-path DB write.

Decision of record: **ADR 0015**. User identity/auth is **explicitly out of
scope** → ADR 0016 (proposed), Day 11 `identity-design.md`.

## Deliverables

1. **`app.sessions` parent table** — the missing entity. `app.query_audit` is
   per-*query* (keyed by `session_id`) with no parent row; add `app.sessions`
   (`session_id` PK, `created_at`, `last_seen_at`, and room for a future
   `user_id` FK — see "Out of scope"). DDL in `init-db/01-init.sql`, mirrored in
   any runtime ensure-schema per the ADR-0004 / Day-2 schema-drift lesson.
2. **Wire `api.py` to write both tables** — on each `/query`, upsert
   `app.sessions` and insert `app.query_audit` (query, product_context, response
   JSONB, the **`langfuse_trace_id`** link, token_count, started/completed_at).
   This is the first app-schema write from the query path.
3. **Fail-open audit write** — wrap the session/audit writes so a failure can
   **never** fail the `/query` response. Log loudly as a structured error
   (`audit.write_failed` with `session_id`) so a dropped record is visible, never
   silent. The answer path takes priority over the audit path (ADR 0015
   Constraints).
4. **Return `session_id` in `QueryResponse`** + **accept an optional inbound
   `session_id`** on the request — additive frozen-contract change (the ADR-0008
   `warning`-field pattern). Validate a caller-supplied id (well-formed UUID;
   an unknown id starts a new session row rather than erroring).
5. **History-retrieval path** — a read endpoint (e.g. `GET /sessions/{id}`)
   returning the session's prior queries + responses from `app.query_audit`,
   ordered by `started_at`. API-key auth as today (no new auth — ADR 0015).
6. **Link eval-runs to a session** — the eval runner (`evals/run.py`) already
   mints a `session_id` per case; persist it so an eval row is traceable to its
   session/audit record (ADR 0012 link).
7. **Tests + eval pass** — unit tests for the write path (including a forced
   write-failure proving fail-open), the contract change, and history retrieval;
   then a full eval-harness run to confirm `citation_validity` and the other
   scorers are unmoved (persistence must not perturb the answer path).

## Out of scope (→ ADR 0016, proposed)

The **user-tracking framework** is NOT part of this day:
- a `users` table, per-user session association, identity source / `user_id`
  issuance;
- auth mechanism (interim header vs OAuth — the Day-11 `identity-design.md`);
- PII handling / retention / right-to-deletion on stored query text +
  `product_context`;
- the Langfuse `userId` feature (does our identity model populate it?);
- multi-tenant isolation (spec §8 / future-work §3).

`app.sessions` should leave **room** for a later `user_id` FK, but this day adds
**no** user identity. If a scope-creep urge appears, stop and write ADR 0016.

## Design constraints

- All config via `src/rra/config.py` (Pydantic Settings) — no `os.getenv`
  elsewhere.
- New DDL goes in **both** `init-db/01-init.sql` and any runtime ensure-schema,
  updated together (Day-2 schema-drift postmortem).
- The `/query` contract change is **additive only** — existing clients that send
  no `session_id` and ignore the returned one keep working (ADR 0008 frozen
  contract).
- No new auth, no new external dependency.

## Stop conditions

- A `/query` with no `session_id` returns one; a second `/query` passing it back
  is recorded against the same `app.sessions` row.
- `app.query_audit` rows are actually written (dead since Day 1), including the
  `langfuse_trace_id` link.
- Forcing the audit write to fail still returns a normal `/query` answer, with a
  loud `audit.write_failed` structured log (fail-open proven by a test).
- `GET /sessions/{id}` returns prior queries for that session.
- Full eval run: `citation_validity` ≥ 0.95 (hard gate); other scorers unmoved
  vs the pre-change baseline — persistence didn't perturb the answer path.
- `uv run python -m rra.evals.run` passes the gate; unit tests + mypy clean.

## Don't do yet

- Anything in "Out of scope" above (→ ADR 0016).
- Multi-turn *context* in the graph (feeding prior session turns into the
  planner/analyst) — the persistence substrate lands here; using it for
  conversation is a later lever.
- A UI for session history (future-work §6).

## Definition of done

Dev-log entry shows: the `app.sessions` schema, the fail-open behavior with the
write-failure log line, the additive contract change (request + response), a
working `GET /sessions/{id}`, and an eval run proving the answer path is
unperturbed. ADR 0015 referenced; user-tracking explicitly deferred to ADR 0016.
