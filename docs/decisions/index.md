# ADR Index

A flat list of all ADRs with status and one-line summary. Keep current — update whenever an ADR is added or superseded.

| #     | Title                                                    | Status | Date       | Notes |
|-------|----------------------------------------------------------|--------|------------|-------|
| 0001  | [LangGraph for orchestration](0001-langgraph-for-orchestration.md) | Active | 2025-05-28 |       |
| 0002  | [pgvector in Postgres](0002-pgvector-in-postgres.md)     | Active | 2025-05-28 |       |
| 0003  | [Anthropic SDK direct, not framework-portable](0003-anthropic-sdk-direct.md) | Active | 2025-05-30 |       |
| 0004  | [Data-access layer: psycopg3 + pool, no ORM](0004-data-access-layer-psycopg-pool.md) | Active | 2026-05-31 | Day 3 |
| 0005  | [Query-time embeddings: singleton client + input_type=query](0005-query-time-embeddings.md) | Active | 2026-05-31 | Day 3 |
| 0006  | [Citation span addressing: guidance_id:chunk_index](0006-citation-span-addressing.md) | Active | 2026-05-31 | Day 3 |
| 0007  | [Corpus scope and methodology](0007-corpus-scope-and-methodology.md) | Active | 2026-05-31 | Day 3


## Conventions for this file

- Sort by number ascending. Don't reorder by status.
- When an ADR is superseded, edit its row to show the new status; do NOT remove the row.
- The "Notes" column is for the supersede pointer (e.g., "→ 0014") or other quick context.

Example after a supersession:

```
| 0007  | [No Redis caching](0007-no-redis.md)        | Superseded by 0014 | 2025-06-15 | → 0014 |
| 0014  | [Redis for retrieval cache](0014-redis-...) | Active             | 2025-07-22 | Supersedes 0007 |
```
