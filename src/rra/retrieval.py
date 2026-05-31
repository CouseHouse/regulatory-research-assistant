"""Corpus retrieval: pgvector similarity search + Voyage rerank.

search_corpus() is a pure function (no FastAPI imports). It is called
directly by api.py in Day 3 and will be wrapped as an MCP tool in Day 5.
The function signature is part of the frozen API contract (docs/plan/day03.md).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog
from psycopg.rows import dict_row

from rra.config import settings
from rra.db import get_conn
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def _voyage_client() -> Any:
    """Process-lifetime Voyage client (ADR 0005: singleton, not per-call)."""
    import voyageai  # local import keeps voyageai out of the module-level namespace

    return voyageai.Client(api_key=settings.voyage_api_key.get_secret_value())  # type: ignore[attr-defined]


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
    """
    if k is None:
        k = settings.rerank_top_k

    client = _voyage_client()

    # ADR 0005: query must use input_type="query"; corpus was indexed with "document".
    # These are NOT interchangeable — using the wrong type silently degrades recall.
    resp = client.embed([query], model=settings.embedding_model, input_type="query")
    raw_emb: list[float] = [float(v) for v in resp.embeddings[0]]

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

    with get_conn() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows: list[dict[str, Any]] = cur.fetchall()

    if not rows:
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
    rerank_resp = client.rerank(query, texts, model=settings.rerank_model, top_k=k)

    return [
        RetrievedPassage(
            **{**candidates[r.index].model_dump(), "score": float(r.relevance_score)}
        )
        for r in rerank_resp.results
    ]
