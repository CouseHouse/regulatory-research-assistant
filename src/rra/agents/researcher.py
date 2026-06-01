"""Researcher agent: query reformulation + retrieval.

Model: claude-haiku-4-5 (settings.researcher_model) for query reformulation.
Retrieval: calls search_corpus() directly (Day 5 replaces with MCP tool).

For each planner sub-question, Haiku rewrites it as an optimised search query
(expanding acronyms, adding regulatory synonyms, rephrasing for the embedding
space), then search_corpus is called with the reformulated query.

Results are deduplicated at chunk level: if the same (guidance_id, chunk_index)
appears from multiple sub-questions, keep the copy with the higher rerank score.
Final list is sorted by score descending.
"""
from __future__ import annotations

import contextlib
from typing import Any

import structlog
from anthropic import Anthropic

from rra.config import settings
from rra.retrieval import search_corpus
from rra.schemas import RetrievedPassage
from rra.tracing import get_langfuse

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a regulatory retrieval query specialist. Your sole task is to rewrite a \
regulatory sub-question into an optimised semantic search query for an FDA guidance \
document corpus.

REWRITING RULES:
- Expand acronyms: PMA → Premarket Approval, 510(k) → premarket notification 510(k), \
IDE → Investigational Device Exemption, De Novo → De Novo classification request, \
OTC → over-the-counter, IVD → in vitro diagnostic, SaMD → Software as a Medical Device.
- Add regulatory synonyms: "clear a device" → "substantial equivalence determination", \
"predicate" → "legally marketed predicate device".
- Rephrase for embedding recall: convert question form to declarative noun phrases when \
it improves specificity.
- Keep the query concise (≤ 25 words). Do not add padding.
- Output ONLY the reformulated query — no explanation, no punctuation wrapper.
"""


def run_researcher(state: dict[str, Any]) -> dict[str, Any]:
    """Node function: reformulate sub-questions and retrieve passages.

    Returns partial state dict with passages and token_usage.
    """
    sub_questions: list[str] = state.get("sub_questions", [])
    if not sub_questions:
        log.warning("researcher.no_sub_questions", session_id=state.get("session_id"))
        return {"passages": [], "token_usage": {}}

    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    total_input_tokens = 0
    total_output_tokens = 0

    # (guidance_id, chunk_index) → best RetrievedPassage
    passage_map: dict[tuple[str, int], RetrievedPassage] = {}

    lf = get_langfuse()
    span_cm = (
        lf.start_as_current_observation(
            name="researcher",
            as_type="span",
            input={"sub_questions": sub_questions},
        )
        if lf is not None
        else contextlib.nullcontext(None)
    )
    with span_cm as span:
        for sq in sub_questions:
            # Step 1: Haiku reformulates the sub-question into an optimised search query.
            gen_cm = (
                span.start_as_current_observation(
                    name="anthropic:researcher",
                    as_type="generation",
                    model=settings.researcher_model,
                    input={"sub_question": sq},
                )
                if span is not None
                else contextlib.nullcontext(None)
            )
            with gen_cm as gen:
                message = client.messages.create(
                    model=settings.researcher_model,
                    max_tokens=128,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": sq}],
                )

                reformulated = ""
                for block in message.content:
                    if hasattr(block, "text"):
                        reformulated = block.text.strip()
                        break

                if gen is not None:
                    gen.update(
                        usage_details={
                            "input": message.usage.input_tokens,
                            "output": message.usage.output_tokens,
                        },
                        output={"reformulated": reformulated},
                    )

            total_input_tokens += message.usage.input_tokens
            total_output_tokens += message.usage.output_tokens

            if not reformulated:
                log.warning("researcher.empty_reformulation", sub_question=sq[:80])
                reformulated = sq  # fall back to original

            log.debug(
                "researcher.reformulated",
                original=sq[:80],
                reformulated=reformulated[:80],
            )

            # Step 2: Retrieve passages for the reformulated query.
            passages = search_corpus(reformulated, k=settings.rerank_top_k)

            if span is not None:
                with span.start_as_current_observation(
                    name="search_corpus",
                    as_type="retriever",
                    input={"query": reformulated},
                    output={"passage_count": len(passages)},
                    metadata={"sub_question": sq[:80]},
                ):
                    pass

            # Step 3: Chunk-level dedup — keep the copy with the higher rerank score.
            for p in passages:
                key = (p.guidance_id, p.chunk_index)
                existing = passage_map.get(key)
                if existing is None or p.score > existing.score:
                    passage_map[key] = p

        # Sort by score descending.
        results = sorted(passage_map.values(), key=lambda p: p.score, reverse=True)

        log.info(
            "researcher.complete",
            session_id=state.get("session_id"),
            sub_question_count=len(sub_questions),
            passage_count=len(results),
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
        )

        if span is not None:
            span.update(
                output={
                    "passage_count": len(results),
                    "sub_question_count": len(sub_questions),
                },
            )

        return {
            "passages": results,
            "token_usage": {
                "researcher_input": total_input_tokens,
                "researcher_output": total_output_tokens,
            },
        }
