"""FastAPI gateway: POST /query endpoint.

Day 3: single-shot retrieval + one Anthropic call.
Day 4: replaces the Anthropic call block with the LangGraph orchestrator.
       The endpoint signature and response schema are stable (ADR 0006, 0008).
"""
from __future__ import annotations

import contextlib
import re
import uuid
from typing import Annotated, Any

import structlog
from fastapi import FastAPI, Header, HTTPException, status

from rra.config import settings
from rra.graph import run_graph
from rra.schemas import Citation, QueryRequest, QueryResponse, RetrievedPassage
from rra.tracing import get_langfuse

log = structlog.get_logger(__name__)

app = FastAPI(title="Regulatory Research Assistant", version="0.1.0")

# Matches any [...] bracket so we can parse the inner content ourselves.
_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
# Matches a single `guidance_id:chunk_index` item (after stripping whitespace).
_SINGLE_CITE_RE = re.compile(r"^([^\]:]+):(\d+)$")


def _verify_api_key(x_api_key: str | None) -> None:
    if x_api_key != settings.rra_api_key.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def _build_warning(final_state: dict[str, Any]) -> str | None:
    """Return a human-readable warning string for non-clean graph exits."""
    if final_state.get("cap_hit"):
        return (
            "Analysis reached the maximum revision limit. "
            "Citations may not be fully verified."
        )
    if final_state.get("verdict") == "escalate":
        return (
            "Query could not be fully grounded in available guidance. "
            "Answer is best-effort."
        )
    return None


@app.post("/query", response_model=QueryResponse)
def query(
    request: QueryRequest,
    x_api_key: Annotated[str | None, Header()] = None,
) -> QueryResponse:
    _verify_api_key(x_api_key)

    session_id = str(uuid.uuid4())
    log.info("query.start", session_id=session_id, query=request.query[:120])

    lf = get_langfuse()
    trace_cm = (
        lf.start_as_current_observation(
            name="query",
            as_type="span",
            input={"query": request.query, "product_context": request.product_context},
            metadata={"session_id": session_id},
        )
        if lf is not None
        else contextlib.nullcontext(None)
    )

    with trace_cm as trace_span:
        trace_id = lf.get_current_trace_id() if lf is not None else None

        final_state = run_graph(
            {
                "query": request.query,
                "product_context": request.product_context,
                "session_id": session_id,
                "trace_id": trace_id,
            }
        )

        draft: str = final_state.get("draft", "")
        passages: list[RetrievedPassage] = final_state.get("passages", [])
        citations = _resolve_citations(draft, passages)
        warning = _build_warning(final_state)

        log.info(
            "query.complete",
            session_id=session_id,
            verdict=final_state.get("verdict"),
            cap_hit=final_state.get("cap_hit"),
            revision_count=final_state.get("revision_count", 0),
            citation_count=len(citations),
            total_tokens=sum(final_state.get("token_usage", {}).values()),
        )

        if trace_span is not None:
            trace_span.update(
                output={"answer": draft[:200], "citation_count": len(citations)}
            )
        if lf is not None:
            lf.flush()

        return QueryResponse(
            answer=draft,
            citations=citations,
            passages=passages,
            trace_id=final_state.get("trace_id") or trace_id,
            warning=warning,
        )


def _parse_citation_pairs(answer: str) -> list[tuple[str, int]]:
    """Extract (guidance_id, chunk_index) pairs from all [...] brackets.

    Handles both single-citation brackets [guid:idx] and grouped citations
    that the model occasionally emits as [guid:idx, guid:idx, ...].
    """
    pairs: list[tuple[str, int]] = []
    for bracket in _BRACKET_RE.finditer(answer):
        for item in re.split(r",\s*", bracket.group(1)):
            m = _SINGLE_CITE_RE.match(item.strip())
            if m:
                pairs.append((m.group(1), int(m.group(2))))
    return pairs


def _resolve_citations(
    answer: str, passages: list[RetrievedPassage]
) -> list[Citation]:
    """Parse inline citations from *answer* and resolve to full Citation objects.

    char_start, char_end, and quoted_text are resolved from the passage
    that was shown to the model — not computed or emitted by the model
    (ADR 0006). quoted_text is the first 150 chars of the chunk, which
    is a verified substring by construction.
    """
    passage_map = {(p.guidance_id, p.chunk_index): p for p in passages}
    seen: set[tuple[str, int]] = set()
    citations: list[Citation] = []

    for guidance_id, chunk_index in _parse_citation_pairs(answer):
        key = (guidance_id, chunk_index)
        if key in seen:
            continue
        seen.add(key)

        passage = passage_map.get(key)
        if passage is None:
            log.warning(
                "citation.unresolved",
                guidance_id=guidance_id,
                chunk_index=chunk_index,
            )
            continue

        citations.append(
            Citation(
                guidance_id=guidance_id,
                chunk_index=chunk_index,
                char_start=passage.char_start,
                char_end=passage.char_end,
                quoted_text=passage.text[:150],
            )
        )

    return citations
