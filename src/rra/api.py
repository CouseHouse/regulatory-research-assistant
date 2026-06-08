"""FastAPI gateway: POST /query endpoint.

Day 3: single-shot retrieval + one Anthropic call.
Day 4: replaces the Anthropic call block with the LangGraph orchestrator.
       The endpoint signature and response schema are stable (ADR 0006, 0008).
"""
from __future__ import annotations

import contextlib
import uuid
from typing import Annotated, Any

import structlog
from fastapi import FastAPI, Header, HTTPException, status

from rra.citations import parse_answer
from rra.config import settings
from rra.db import get_pool
from rra.graph import run_graph
from rra.schemas import Citation, QueryRequest, QueryResponse, RetrievedPassage
from rra.tracing import get_langfuse

log = structlog.get_logger(__name__)

app = FastAPI(title="Regulatory Research Assistant", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for the ALB target group (infra/terraform/ecs.tf).

    Unauthenticated and dependency-free by design: /query requires X-API-Key and
    is a POST, so it can't serve as a health check. This must stay a cheap 200
    that does NOT touch Postgres — a DB blip should not pull tasks out of service.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness probe: confirms Postgres is reachable (SELECT 1).

    Deliberately SEPARATE from /health (W1, ADR 0017 dev-log). The DB connection
    is lazy — it first opens on /query (the graph checkpointer + the retrieval
    pool), so a HEALTHY task can still 500 every real query if RDS is unreachable
    (wrong SG rule, subnet, or creds). This endpoint surfaces that on demand —
    curl it in the deploy smoke test BEFORE the first /query. It must NOT back the
    ALB liveness check: a DB blip should not pull tasks out of service.

    Borrows from the request-path pool but skips register_vector — this is a pure
    connectivity check, not a pgvector check, so it stays green pre-ingest too.
    """
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        log.warning("readyz.db_unreachable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unreachable",
        ) from exc
    return {"status": "ready"}


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
    # Propagate session_id onto the parent span AND the child spans the graph
    # opens, so Langfuse's Sessions view groups this request's whole trace —
    # metadata={"session_id": ...} alone does NOT populate Sessions (v4). The
    # "query" span must START INSIDE this context to inherit it, so session_cm is
    # the OUTER with-context. Imported lazily to keep langfuse off the cold-path
    # when tracing is disabled (mirrors tracing.get_langfuse).
    session_cm: Any
    if lf is not None:
        from langfuse import propagate_attributes

        session_cm = propagate_attributes(session_id=session_id)
    else:
        session_cm = contextlib.nullcontext()

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

    with session_cm, trace_cm as trace_span:
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
        # parse_answer (shared with the eval) splits the analyst's draft into the
        # user-facing prose (with <q>…</q> envelopes stripped) and the citation
        # triples carrying each analyst-emitted supporting quote (ADR 0013).
        clean_prose, triples = parse_answer(draft)
        citations = _resolve_citations(triples, passages, session_id=session_id)
        no_quote_count = sum(1 for c in citations if not c.quoted_text)
        warning = _build_warning(final_state)

        log.info(
            "query.complete",
            session_id=session_id,
            verdict=final_state.get("verdict"),
            cap_hit=final_state.get("cap_hit"),
            revision_count=final_state.get("revision_count", 0),
            citation_count=len(citations),
            no_quote_citations=no_quote_count,
            total_tokens=sum(final_state.get("token_usage", {}).values()),
        )

        if trace_span is not None:
            trace_span.update(
                output={
                    "answer": clean_prose[:200],
                    "citation_count": len(citations),
                    "no_quote_citations": no_quote_count,
                }
            )
        if lf is not None:
            lf.flush()

        return QueryResponse(
            answer=clean_prose,
            citations=citations,
            passages=passages,
            trace_id=final_state.get("trace_id") or trace_id,
            warning=warning,
        )


def _resolve_citations(
    triples: list[tuple[str, int, str | None]],
    passages: list[RetrievedPassage],
    session_id: str | None = None,
) -> list[Citation]:
    """Resolve parsed (guidance_id, chunk_index, quoted_text) triples to Citations.

    char_start/char_end are resolved server-side from the passage shown to the
    model (ADR 0006); the model never computes them. quoted_text is the analyst's
    OWN verbatim supporting span (ADR 0013) — it is NOT a slice of the chunk and
    NOT guaranteed to be a substring of it. A missing/empty analyst quote resolves
    to quoted_text="" and is logged as citation.no_quote; it is NEVER back-filled
    with a chunk slice — doing so would reinstate the tautology this change exists
    to kill (Day-7 plan §7-#1). Quote faithfulness is measured out-of-band by
    check_citation (the eval scorer), not asserted here.

    Dedup is by (guidance_id, chunk_index), first occurrence wins for the response
    shape (so its quote is the one surfaced when a chunk is cited more than once).
    """
    passage_map = {(p.guidance_id, p.chunk_index): p for p in passages}
    seen: set[tuple[str, int]] = set()
    citations: list[Citation] = []

    for guidance_id, chunk_index, quote in triples:
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
                session_id=session_id,
            )
            continue

        if not quote:
            # Honest "no quote" — log and surface as quoted_text="". NEVER fall
            # back to a chunk slice (Day-7 plan §7-#1 / drop-point D-E).
            log.info(
                "citation.no_quote",
                guidance_id=guidance_id,
                chunk_index=chunk_index,
                session_id=session_id,
            )

        citations.append(
            Citation(
                guidance_id=guidance_id,
                chunk_index=chunk_index,
                char_start=passage.char_start,
                char_end=passage.char_end,
                quoted_text=quote or "",  # NO slice fallback — ever (ADR 0013).
            )
        )

    return citations
