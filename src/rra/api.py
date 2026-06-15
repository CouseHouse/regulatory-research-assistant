"""FastAPI gateway: POST /query endpoint.

Day 3: single-shot retrieval + one Anthropic call.
Day 4: replaces the Anthropic call block with the LangGraph orchestrator.
       The endpoint signature and response schema are stable (ADR 0006, 0008).
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any

import structlog
from fastapi import FastAPI, Header, HTTPException, status

from rra.agents._sanitize import strip_markdown_images
from rra.citations import parse_answer
from rra.config import settings
from rra.db import get_pool
from rra.graph import run_graph
from rra.ports.guardrails import get_guardrails
from rra.ports.identity import get_identity
from rra.ports.observability import get_observability
from rra.schemas import Citation, QueryRequest, QueryResponse, RetrievedPassage

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
    """Readiness probe: confirms Postgres is reachable AND the corpus is bootstrapped.

    Deliberately SEPARATE from /health (W1, ADR 0017 dev-log). The DB connection
    is lazy — it first opens on /query (the graph checkpointer + the retrieval
    pool), so a HEALTHY task can still 500 every real query if RDS is unreachable
    (wrong SG rule, subnet, or creds) OR if the bootstrap ingest task never ran
    (corpus.chunks absent). This surfaces both on demand — curl it in the deploy
    smoke test BEFORE the first /query. It must NOT back the ALB liveness check:
    a DB blip should not pull tasks out of service.

    `to_regclass` is one round-trip that doubles as the connectivity check (a
    failure raises) and the "did the bootstrap run?" check (NULL → table absent).
    register_vector is skipped — this is SQL-only. It deliberately does NOT count
    rows: an empty corpus serves 200s (degraded, not broken), so "ready" means the
    schema is present, not that the corpus is non-empty.
    """
    try:
        with get_pool().connection() as conn:
            row = conn.execute("SELECT to_regclass('corpus.chunks')").fetchone()
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        log.warning("readyz.db_unreachable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unreachable",
        ) from exc

    if row is None or row[0] is None:
        log.warning("readyz.corpus_uninitialized")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="corpus not initialized — run the bootstrap ingest task (ADR 0017)",
        )

    return {"status": "ready"}


def _verify_api_key(x_api_key: str | None) -> None:
    """Verify the caller's API key via the identity port.

    Delegates to get_identity().verify_api_caller() which uses
    secrets.compare_digest for constant-time comparison (fixes the
    timing-attack-prone != compare; ADR 0021).

    Returns normally on success.  Raises HTTP 401 on any mismatch.
    The response body is identical whether the key was absent, wrong, or
    close — no hint about the mismatch is surfaced.
    """
    principal = get_identity().verify_api_caller(x_api_key or "")
    if principal is None:
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

    obs = get_observability()

    # ── Guardrails: user-input boundary ───────────────────────────────────────
    # Check the query and product_context BEFORE the graph runs.
    # With AllowAllGuardrails (phase-2 wiring) this is a no-op; the real
    # detector is swapped in during the security-harness phase.
    # On block: HTTP 400 with a generic message — NO echoing of the query text.
    # Logging: ONLY boundary + categories logged on block; never the text.
    guardrails = get_guardrails()
    query_verdict = guardrails.check(request.query, boundary="user_input")
    if not query_verdict.allowed:
        log.warning(
            "guardrails.blocked",
            boundary=query_verdict.boundary,
            categories=query_verdict.categories,
            # Intentionally omit: request.query, score, reason (might echo content).
        )
        # Surface the incident in the trace UI (ADR 0024). Metadata-only — the
        # offending query text is NEVER recorded (RT-redteam.md). No request span
        # is open yet, so the adapter hosts the score on a one-shot incident trace.
        obs.record_security_event(
            boundary="user_input",
            categories=query_verdict.categories,
            detector_score=query_verdict.score,
            reason=query_verdict.reason,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request blocked by content policy",
        )
    if request.product_context:
        ctx_verdict = guardrails.check(
            request.product_context, boundary="user_input"
        )
        if not ctx_verdict.allowed:
            log.warning(
                "guardrails.blocked",
                boundary=ctx_verdict.boundary,
                categories=ctx_verdict.categories,
            )
            obs.record_security_event(
                boundary="user_input",
                categories=ctx_verdict.categories,
                detector_score=ctx_verdict.score,
                reason=ctx_verdict.reason,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="request blocked by content policy",
            )

    session_id = str(uuid.uuid4())
    log.info("query.start", session_id=session_id, query=request.query[:120])

    # Propagate session_id onto the parent span AND the child spans the graph
    # opens, so Langfuse's Sessions view groups this request's whole trace —
    # metadata={"session_id": ...} alone does NOT populate Sessions (v4). The
    # "query" span must START INSIDE this context to inherit it, so session_cm is
    # the OUTER with-context.
    with obs.propagate_session(session_id), obs.start_span(
        "query",
        as_type="span",
        # ADR 0025: product_context is confidential commercial info — never store its
        # text in a trace; record only its size. The query (regulatory question) is kept
        # for trace utility (general question, not device CCI).
        input={"query": request.query, "product_context_chars": len(request.product_context)},
        metadata={"session_id": session_id},
    ) as trace_span:
        trace_id = obs.current_trace_id()

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
        # Output filter (RT-4 / LLM05): markdown images are a zero-click
        # exfiltration channel when the answer renders in a downstream client.
        # Deny-all — regulatory answers never legitimately contain images.
        clean_prose, images_stripped = strip_markdown_images(clean_prose)
        if images_stripped:
            log.warning(
                "output_filter.images_stripped",
                session_id=session_id,
                count=images_stripped,
            )
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

        trace_span.update(
            output={
                "answer": clean_prose[:200],
                "citation_count": len(citations),
                "no_quote_citations": no_quote_count,
            }
        )
        obs.flush()

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

        # Output filter (RT-4 / LLM05, RT Phase-3 finding RT-P3-3): quoted_text
        # is a verbatim span the analyst copied from untrusted corpus content and
        # ships to the client in the citations[] array.  The prose-level image
        # strip does NOT reach here, so a markdown-image exfil payload laundered
        # through the <q> channel would otherwise bypass the filter.  Strip it
        # here too — same deny-all rationale.
        safe_quote = ""
        if quote:
            safe_quote, q_imgs = strip_markdown_images(quote)
            if q_imgs:
                log.warning(
                    "output_filter.images_stripped",
                    session_id=session_id,
                    location="citation_quote",
                    count=q_imgs,
                )

        citations.append(
            Citation(
                guidance_id=guidance_id,
                chunk_index=chunk_index,
                char_start=passage.char_start,
                char_end=passage.char_end,
                quoted_text=safe_quote,  # NO slice fallback — ever (ADR 0013).
            )
        )

    return citations
