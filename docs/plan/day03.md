# Day 3 — Basic RAG, no agents

## Goal

A working `/query` endpoint that retrieves and answers single-shot — no multi-agent yet. This proves retrieval works before the orchestrator complicates everything. Day 4 will replace the answering logic; the retrieval and API contract stay.

## Deliverables

- `src/rra/api.py`: FastAPI app, single `POST /query` endpoint, API key middleware, structured response schema
- `src/rra/retrieval.py`: `search_corpus(query, k, filters)` function (will be reused by MCP server on day 5)
- `src/rra/schemas.py`: Pydantic response models — `QueryRequest`, `QueryResponse`, `Citation`, `RetrievedPassage` (lock the API contract now so day 4 doesn't break it)
- `tests/test_retrieval.py`: against a known small corpus (use a fixture-loaded subset)
- `tests/test_api.py`: endpoint tests with TestClient

## Design constraints

- Response shape MUST be stable across days 3, 4, 5 — don't change it later
- Reranker (Voyage rerank-2) sits between retrieval and answering — apply it here, not later
- Retrieval logic stays a pure function that the MCP server will wrap on day 5
- All config via `rra.config.settings`

## Decisions to make

1. Citation format: `[guidance_id:span]` inline in answer text, with structured `citations[]` array? Recommended yes.
2. Top-k for retrieval (25 per spec) and rerank-to (5 per spec) — confirm in `config.py` if not already
3. How to format retrieved passages in the Claude prompt — XML tags vs. JSON vs. plain text
4. Streaming or one-shot for v1? One-shot is fine; streaming is future-work §4

## Stop conditions

- `curl -X POST localhost:8000/query -H "X-API-Key: dev-key-change-me" -d '{"query":"...","product_context":"..."}'` returns valid JSON
- Response includes coherent answer with at least one citation
- 10 manual smoke-test queries on real questions don't crash and produce passable answers (eyeball quality — not scored yet)
- Tests pass, mypy clean

## Don't do yet

- Multi-agent orchestration (day 4)
- MCP tools (day 5)
- Eval scoring (day 6)
- Cloud deploy (day 8-9)

## Definition of done

Dev-log entry shows: a curl example, the response JSON, one example trace URL in Langfuse, eyeball quality assessment (be honest — "weak on synthesis questions, good on lookups").
