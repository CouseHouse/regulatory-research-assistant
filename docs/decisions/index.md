# ADR Index

A flat list of all ADRs with status and one-line summary. Keep current — update whenever an ADR is added or superseded.

| #     | Title                                                    | Status | Date       | Notes |
|-------|----------------------------------------------------------|--------|------------|-------|
| 0001  | [LangGraph for orchestration](0001-langgraph-for-orchestration.md) | Active | 2025-05-28 |       |
| 0002  | [pgvector in Postgres](0002-pgvector-in-postgres.md)     | Active | 2025-05-28 |       |
| 0003  | [Anthropic SDK direct, not framework-portable](0003-anthropic-sdk-direct.md) | Active | 2025-05-30 |       |
| 0004  | [Data-access layer: psycopg3 + pool, no ORM](0004-data-access-layer-psycopg-pool.md) | Active | 2026-05-31 | Day 3 |
| 0005  | [Query-time embeddings: singleton client + input_type=query](0005-query-time-embeddings.md) | Active | 2026-05-31 | Day 3 |
| 0006  | [Citation span addressing: guidance_id:chunk_index](0006-citation-span-addressing.md) | Active | 2026-05-31 | Day 3; quoted_text clause amended by 0013 |
| 0007  | [Corpus scope and methodology](0007-corpus-scope-and-methodology.md) | Active | 2026-05-31 | Day 3 |
| 0008  | [LangGraph state shape and agent contracts](0008-langgraph-state-shape.md) | Active | 2026-06-01 | Day 4; session_id contract amended by 0015 |
| 0009  | [Critic-loop policy](0009-critic-loop-policy.md) | Active | 2026-06-01 | Day 4 |
| 0010  | [`check_citation` matching contract](0010-check-citation-matching-contract.md) | Active | 2026-06-01 | Day 5 |
| 0011  | [MCP tools as in-process functions; server as exposure layer](0011-mcp-tools-in-process.md) | Active | 2026-06-01 | Day 5 |
| 0012  | [Day 6 eval-harness scoring and CI policy](0012-eval-harness-scoring-and-ci-policy.md) | Active | 2026-06-02 | Day 6 |
| 0013  | [Quote-faithfulness: analyst-emitted supporting quotes](0013-quote-faithfulness-activation.md) | Active | 2026-06-02 | Day 7; amends 0006; critic-flip activated in-loop faithfulness (2026-06-05) — see 0013 banner |
| 0014  | [Corpus cleaning + structural chunking](0014-corpus-cleaning-structural-chunking.md) | Active | 2026-06-02 | Day 7; resolves ADR 0006 orphan-row Q; **D4a:** cutover ran via `--truncate` not swap — see 0014 validation banner |
| 0015  | [Session tracking: persist and surface session_id](0015-session-tracking.md) | Active | 2026-06-02 | Day 10 (new); amends 0008; opens ADR 0016 |
| 0017  | [Private-RDS bootstrap via a one-off in-VPC ingest task](0017-private-rds-bootstrap.md) | Superseded by 0018 | 2026-06-08 | Closes pre-deploy finding B1; → 0018 (corpus-sourcing) |
| 0018  | [Bootstrap image bakes the cached corpus (FDA blocks the Fargate IP)](0018-bootstrap-bakes-cached-corpus.md) | Active | 2026-06-08 | Supersedes 0017; FDA/Akamai 4xx-blocks the datacenter IP |
| 0019  | [RRA_PROFILE config/profile system](0019-rra-profile-config-system.md) | Active | 2026-06-11 | Phase 1 of ports/adapters refactor; per-profile defaults + SecretStr leak tests |


## Conventions for this file

- Sort by number ascending. Don't reorder by status.
- When an ADR is superseded, edit its row to show the new status; do NOT remove the row.
- The "Notes" column is for the supersede pointer (e.g., "→ 0014") or other quick context.

Example after a supersession:

```
| 0007  | [No Redis caching](0007-no-redis.md)        | Superseded by 0014 | 2025-06-15 | → 0014 |
| 0014  | [Redis for retrieval cache](0014-redis-...) | Active             | 2025-07-22 | Supersedes 0007 |
```
