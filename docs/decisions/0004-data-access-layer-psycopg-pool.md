# 0004 — Data-access layer: psycopg3 + connection pool, no ORM

**Status:** Active
**Date:** 2026-05-31
**Owner:** Kyle Couse

## Context

Day 3 opens the query (read) path; ingest (Day 2) used psycopg3 directly with a
fresh `psycopg.connect()` per document — fine for a weekly batch, wrong under
per-request API concurrency. Whatever pattern `retrieval.py` adopts is inherited
by `graph.py` and the MCP server, so the data-access style must be settled now
(Day 2 dev-log left "psycopg vs SQLAlchemy" explicitly undecided). ADR 0002
already commits us to a single Postgres+pgvector instance, which removes
cross-dialect portability as a motivation.

## Decision

We use **psycopg3 directly with a process-lifetime shared `ConnectionPool`**,
exposed through one `get_pool()` / connection helper in `src/rra`, and write SQL
(including pgvector `<=>` queries) by hand — **no ORM, no query builder**.

## Alternatives considered

- **SQLAlchemy Core (query builder, no ORM)** — Rejected. Adds a dependency and
  an abstraction over SQL we already hand-write; pgvector operators need custom
  type/dialect plumbing; its main payoff (dialect portability) is moot under
  ADR 0002's single-Postgres commitment.
- **SQLAlchemy ORM (models + session)** — Rejected. Session lifecycle and
  lazy-loading are a poor fit for a read-mostly vector-search path that returns
  ad-hoc projections, not mapped entities. LangGraph's checkpointer manages its
  own tables independently, so the ORM would map only a thin slice.
- **`psycopg.connect()` per call (ingest status quo)** — Rejected for the query
  path. Connection setup latency on every request plus exhaustion under
  concurrency. Acceptable only for the batch job it already serves.
- **asyncpg** — Rejected for now. Faster async driver, but request latency here
  is dominated by Anthropic/Voyage calls, and mixing asyncpg with ingest's
  psycopg3 doubles the driver surface. psycopg3 offers a native async path if we
  later need it, so we can get there without adopting asyncpg.
- **External pooler (PgBouncer)** — Rejected. An operational component for a
  scale problem we don't have (single instance, 20 docs / 288 chunks); an
  app-level pool suffices.

## Consequences

**Enables:**
- One shared pool across `api.py`, `retrieval.py`, `graph.py`, and the MCP server.
- Raw pgvector `<=>` similarity queries with no ORM translation layer.
- Continuity with ingest's existing psycopg3 usage (one driver in the codebase).
- A native psycopg3 async path later without a driver swap.

**Constrains:**
- SQL is hand-written — no compile-time query checking; correctness rides on
  tests.
- We own pool sizing / connection lifecycle (config flows through
  `src/rra/config.py` per the project config rule).
- A future multi-DB requirement would mean per-dialect hand-written SQL.

**Reopen if:**
- App-owned relational state grows past ~6 interrelated tables (excluding
  LangGraph-managed checkpoint tables) such that hand-written SQL becomes
  error-prone — revisit SQLAlchemy Core at that point.
- A customer mandates a non-Postgres store where dialect abstraction would pay
  off (cross-reference ADR 0002's Azure AI Search reopen trigger).

## Related

- spec.md §4.3 (pgvector), §5 (data flow)
- ADR 0002 (pgvector in Postgres) — single-store commitment is the load-bearing premise
- ADR 0005 (query-time embeddings) — the other half of the query path introduced this day
- Day 2 dev-log: open question "psycopg directly vs. SQLAlchemy ORM"; deferred connection pool
