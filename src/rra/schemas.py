"""Pydantic v2 models for the query API contract.

This contract is frozen from Day 3 forward. Later days add features via
optional fields only — do NOT rename or remove fields without a version bump.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    product_context: str = Field(
        default="",
        description="Optional regulatory/product context to narrow the answer.",
    )


class Citation(BaseModel):
    """Resolved citation per ADR 0006.

    The model emits [guidance_id:chunk_index] inline in the answer text.
    char_start, char_end, and quoted_text are resolved server-side from the
    corpus.chunks row — the model never computes or emits them directly.
    """

    guidance_id: str
    chunk_index: int
    char_start: int
    char_end: int
    quoted_text: str = Field(
        ...,
        description="Verified to be a substring of the stored chunk text.",
    )


class RetrievedPassage(BaseModel):
    """One passage returned by the retrieval layer, in post-rerank order."""

    guidance_id: str
    guidance_title: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    score: float = Field(..., description="Voyage rerank-2 relevance score.")


class QueryResponse(BaseModel):
    """Response envelope — stable from Day 3 forward."""

    answer: str
    citations: list[Citation]
    passages: list[RetrievedPassage]
    trace_id: str | None = Field(
        default=None,
        description="Langfuse trace ID — populated once Langfuse is wired in Day 4.",
    )
