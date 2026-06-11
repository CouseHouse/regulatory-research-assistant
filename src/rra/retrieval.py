"""Corpus retrieval: pgvector similarity search + Voyage rerank.

search_corpus() is a pure function (no FastAPI imports). It is called
directly by api.py in Day 3 and will be wrapped as an MCP tool in Day 5.
The function signature is part of the frozen API contract (docs/plan/day03.md).
"""
from __future__ import annotations

from typing import Any

import structlog

from rra.config import settings
from rra.ports.embeddings import get_embeddings
from rra.ports.observability import get_observability
from rra.ports.vectorstore import get_vector_store
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)


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
        lf:      DEPRECATED — kept for signature compatibility (frozen contract).
                 Previously accepted a raw Langfuse client; now ignored.
                 Instrumentation is handled via get_observability() internally.
                 Pass None (default). Passing a non-None value has no effect.
    """
    if k is None:
        k = settings.rerank_top_k

    obs = get_observability()
    with obs.start_span(
        "search_corpus",
        as_type="retriever",
        input={"query": query, "k": k},
    ) as span:
        return _search_corpus_inner(query, k, filters, span)


def _search_corpus_inner(
    query: str,
    k: int,
    filters: dict[str, Any] | None,
    span: Any,
) -> list[RetrievedPassage]:
    emb = get_embeddings()

    # ADR 0005: query must use input_type="query"; corpus was indexed with "document".
    # These are NOT interchangeable — using the wrong type silently degrades recall.
    raw_emb: list[float] = emb.embed_query(query)

    # Retrieval policy (how many, filtered to what) lives here; everything
    # provider-specific (SQL, vector literals) lives in the adapter.
    guidance_ids: list[str] = []
    if filters:
        raw = filters.get("guidance_ids")
        if isinstance(raw, list) and raw:
            guidance_ids = [str(g) for g in raw]

    rows: list[dict[str, Any]] = get_vector_store().similarity_search(
        embedding=raw_emb,
        top_k=settings.retrieve_top_k,
        guidance_ids=guidance_ids or None,
    )

    if not rows:
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
