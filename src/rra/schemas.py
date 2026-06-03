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
    """Resolved citation per ADR 0006 (addressing) and ADR 0013 (quoted_text).

    The model emits [guidance_id:chunk_index]<q>supporting quote</q> inline in
    the answer. char_start/char_end are resolved server-side from the
    corpus.chunks row (ADR 0006) — the model never computes them. quoted_text is
    the analyst's OWN verbatim supporting span (ADR 0013): it is NOT guaranteed to
    be a substring of the chunk (superseding ADR 0006's substring clause), and is
    "" when the analyst supplied no quote. Quote faithfulness is measured
    separately by check_citation (ADR 0010), not asserted by this field.
    """

    guidance_id: str
    chunk_index: int
    char_start: int
    char_end: int
    quoted_text: str = Field(
        ...,
        description=(
            "The analyst's verbatim supporting span for this citation (ADR 0013). "
            'NOT guaranteed to be a substring of the chunk; "" when no quote was '
            "supplied. Faithfulness is verified out-of-band by check_citation."
        ),
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
        description="Langfuse trace ID. Populated when settings.langfuse_enabled is True.",
    )
    warning: str | None = Field(
        default=None,
        description=(
            "Set when the graph exits with a non-clean condition: cap_hit=True "
            "(max revisions reached) or verdict=escalate (corpus coverage gap). "
            "None on normal approve exits. Additive optional field per ADR 0008."
        ),
    )
