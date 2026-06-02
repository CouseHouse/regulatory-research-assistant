# Day 1 — Foundation

**Status:** ✅ Complete (retained for reference)

## Goal

Local stack running, config typed, eval harness shape committed, docs in place, repo on GitHub.

## Deliverables built

- WSL2 + Docker Desktop + git configured
- `docker-compose.yml`: Postgres+pgvector, Langfuse v3 (web + worker + ClickHouse + MinIO + Redis), MinIO bucket auto-init
- `init-db/01-init.sql`: schemas (app, corpus, langgraph) + chunks table with vector(1024) + HNSW index
- `pyproject.toml`: full dep list, strict mypy, ruff config, pytest config
- `src/rra/config.py`: Pydantic Settings singleton
- `src/rra/evals/{dataset.py,scorers.py,run.py}`: eval harness skeleton with NotImplementedError stubs
- `evals/golden.jsonl`: schema with 3 placeholder questions
- `docs/spec.md`, `docs/future-work.md`, `docs/dev-log.md`
- `.env.example` with all required keys including the three Langfuse compose secrets
- `.claude/settings.json`: safety hooks
- `CLAUDE.md`: project context for Claude Code

## Notes captured in dev-log

- Docker credential helper missing in WSL → fixed with empty `~/.docker/config.json`
- Langfuse v3 needs real 64-char hex encryption key + nextauth secret + salt (not zeros)
- MinIO bucket has to exist before Langfuse starts → init container added
- pgvector 0.8.2 is fine (newer than originally targeted 0.7)
