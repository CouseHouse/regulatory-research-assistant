"""FastAPI gateway: POST /query endpoint.

Day 3: single-shot retrieval + one Anthropic call. No agents yet.
Day 4 replaces the Anthropic call block with the LangGraph orchestrator;
the endpoint signature and response schema are stable.
"""
from __future__ import annotations

import contextlib
import re
import uuid
from typing import Annotated

import structlog
from anthropic import Anthropic
from anthropic.types import TextBlock
from fastapi import FastAPI, Header, HTTPException, status

from rra.config import settings
from rra.retrieval import search_corpus
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

        passages = search_corpus(request.query, k=settings.rerank_top_k, lf=lf)

        if not passages:
            log.info("query.no_passages", session_id=session_id)
            if trace_span is not None:
                trace_span.update(output={"answer": "", "citation_count": 0})
            if lf is not None:
                lf.flush()
            return QueryResponse(
                answer="", citations=[], passages=[], trace_id=trace_id
            )

        system_prompt = (
            "You are a regulatory research assistant specializing in FDA guidance documents. "
            "Answer questions based solely on the provided passages. "
            "When citing a passage, place ONE citation bracket immediately after the claim: "
            "[guidance_id:chunk_index]. Copy guidance_id and chunk_index exactly as shown in "
            "each <passage> tag. Never put multiple citations in the same bracket. "
            "Example of correct form: 'The device must meet X [abc123:4] and Y [abc123:7].' "
            "Be precise and concise. Do not invent citations or facts not found in the passages."
        )

        user_prompt = _format_user_prompt(request, passages)

        # Day 3: single Anthropic call, no agents. Day 4 replaces this block.
        gen_cm = (
            lf.start_as_current_observation(
                name="anthropic-call",
                as_type="generation",
                model=settings.analyst_model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            if lf is not None
            else contextlib.nullcontext(None)
        )

        with gen_cm as gen_span:
            anthropic_client = Anthropic(
                api_key=settings.anthropic_api_key.get_secret_value()
            )
            message = anthropic_client.messages.create(
                model=settings.analyst_model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            answer = next(
                (block.text for block in message.content if isinstance(block, TextBlock)),
                "",
            )

            log.info(
                "query.complete",
                session_id=session_id,
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            )

            if gen_span is not None:
                gen_span.update(
                    output=answer,
                    usage_details={
                        "input": message.usage.input_tokens,
                        "output": message.usage.output_tokens,
                    },
                )

        citations = _resolve_citations(answer, passages)

        if trace_span is not None:
            trace_span.update(
                output={"answer": answer, "citation_count": len(citations)}
            )
        if lf is not None:
            lf.flush()

        return QueryResponse(
            answer=answer,
            citations=citations,
            passages=passages,
            trace_id=trace_id,
        )


def _format_user_prompt(request: QueryRequest, passages: list[RetrievedPassage]) -> str:
    parts = ["<passages>"]
    for p in passages:
        parts.append(
            f'<passage guidance_id="{p.guidance_id}" chunk_index="{p.chunk_index}">\n'
            f"<title>{p.guidance_title}</title>\n"
            f"<text>{p.text}</text>\n"
            f"</passage>"
        )
    parts.append("</passages>")
    parts.append("")
    parts.append(f"Question: {request.query}")
    if request.product_context:
        parts.append(f"Product context: {request.product_context}")
    parts.append(
        "\nAnswer based on the passages above. "
        "Cite sources as [guidance_id:chunk_index]."
    )
    return "\n".join(parts)


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
