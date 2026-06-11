"""Corpus retrieval: pgvector similarity search + Voyage rerank.

search_corpus() is a pure function (no FastAPI imports). It is called
directly by api.py in Day 3 and will be wrapped as an MCP tool in Day 5.
The function signature is part of the frozen API contract (docs/plan/day03.md).
"""
from __future__ import annotations

import contextlib
from typing import Any

import structlog

from rra.config import settings
from rra.ports.embeddings import get_embeddings
from rra.ports.vectorstore import get_vector_store
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)


_BASE_SQL = """
SELECT
    guidance_id,
    guidance_title,
    chunk_index,
    text,
    char_start,
    char_end,
    1 - (embedding <=> %(query_embedding)s::vector) AS score
FROM corpus.chunks
{where_clause}
ORDER BY embedding <=> %(query_embedding)s::vector
LIMIT %(limit)s
"""


def search_corpus(
    query: str,
    k: int | None = None,
    filters: dict[str, Any] | None = None,
    lf: Any | None = None,
) -> list[RetrievedPassage]:
    """Retrieve top-k passages for *query* via vector search then rerank.

    Steps:
      1. Embed query with input_type="query" (ADR 0005 — asymmetric model).
      2. Fetch settings.retrieve_top_k candidates from pgvector (cosine distance).
      3. Rerank with Voyage rerank-2, narrowing to *k* (default rerank_top_k=5).
      4. Return passages in reranker order with rerank scores.

    Args:
        query:   Natural-language question or sub-question.
        k:       Number of passages to return after reranking.
                 Defaults to settings.rerank_top_k (5).
        filters: Optional filter dict. Recognised key:
                   "guidance_ids": list[str] — restrict to these guidance_ids.
        lf:      Langfuse client for nested instrumentation.
                 Pass None (default) to skip instrumentation entirely.
    """
    if k is None:
        k = settings.rerank_top_k

    obs_cm = (
        lf.start_as_current_observation(
            name="search_corpus",
            as_type="retriever",
            input={"query": query, "k": k},
        )
        if lf is not None
        else contextlib.nullcontext(None)
    )

    with obs_cm as span:
        return _search_corpus_inner(query, k, filters, span)


def _search_corpus_inner(
    query: str,
    k: int,
    filters: dict[str, Any] | None,
    span: Any | None,
) -> list[RetrievedPassage]:
    emb = get_embeddings()

    # ADR 0005: query must use input_type="query"; corpus was indexed with "document".
    # These are NOT interchangeable — using the wrong type silently degrades recall.
    raw_emb: list[float] = emb.embed_query(query)

    # Format as pgvector text literal '[v1,v2,...]' and cast with ::vector in SQL.
    # This sidesteps psycopg3 adapter registration entirely — the adapter (register_vector)
    # does not propagate reliably on pooled connections under asyncio's threadpool.
    # Postgres's own text→vector cast is unambiguous and always works.
    query_embedding: str = "[" + ",".join(map(str, raw_emb)) + "]"

    # Build SQL — dynamic WHERE only when guidance_ids filter is present.
    guidance_ids: list[str] = []
    if filters:
        raw = filters.get("guidance_ids")
        if isinstance(raw, list) and raw:
            guidance_ids = [str(g) for g in raw]

    if guidance_ids:
        where_clause = "WHERE guidance_id = ANY(%(guidance_ids)s)"
    else:
        where_clause = ""

    sql = _BASE_SQL.format(where_clause=where_clause)

    params: dict[str, Any] = {
        "query_embedding": query_embedding,
        "limit": settings.retrieve_top_k,
    }
    if guidance_ids:
        params["guidance_ids"] = guidance_ids

    rows: list[dict[str, Any]] = get_vector_store().similarity_search(sql, params)

    if not rows:
        if span is not None:
            span.update(output={"passage_count": 0, "passages": []})
        return []

    candidates = [
        RetrievedPassage(
            guidance_id=row["guidance_id"],
            guidance_title=row["guidance_title"],
            chunk_index=row["chunk_index"],
            text=row["text"],
            char_start=row["char_start"],
            char_end=row["char_end"],
            score=float(row["score"]),
        )
        for row in rows
    ]

    texts = [p.text for p in candidates]
    rerank_results = emb.rerank(query, texts, top_k=k)

    results = [
        RetrievedPassage(
            **{**candidates[r.index].model_dump(), "score": float(r.relevance_score)}
        )
        for r in rerank_results
    ]

    if span is not None:
        span.update(
            output={
                "passage_count": len(results),
                "passages": [
                    {
                        "guidance_id": p.guidance_id,
                        "chunk_index": p.chunk_index,
                        "title": p.guidance_title,
                        "score": p.score,
                    }
                    for p in results
                ],
            }
        )

    return results
