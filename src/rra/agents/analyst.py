"""Analyst agent: synthesis and revision.

Model: claude-sonnet-4-6 (settings.analyst_model).

First pass: synthesizes a structured answer from retrieved passages, following
the planner's outline. Every factual claim must carry exactly one [guid:idx]
citation immediately after it.

Revision pass (revision_count > 0): receives the prior draft + critic notes and
edits in place, addressing only the flagged issues without regressing correct
sections. This is cheaper than full re-synthesis and more measurable in evals.

Prompt caching applied to the system prompt (exceeds the 1024-token threshold).
"""
from __future__ import annotations

import contextlib
from typing import Any

import structlog
from anthropic.types import CacheControlEphemeralParam, TextBlockParam

from rra.agents.types import CriticNote
from rra.config import settings
from rra.ports.llm import get_llm
from rra.schemas import QueryRequest, RetrievedPassage
from rra.tracing import get_langfuse

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a regulatory research synthesis agent specialising in FDA medical device \
guidance. You produce precise, well-structured regulatory analyses for regulatory \
affairs professionals, quality engineers, and product managers preparing FDA \
submissions.

════════════════════════════════════════════
CITATION RULES (strictly enforced)
════════════════════════════════════════════
1. Cite EVERY factual claim with exactly ONE bracket immediately after the claim: \
[guidance_id:chunk_index]. Copy guidance_id and chunk_index verbatim from the \
<passage> tag attributes — do not paraphrase or abbreviate them.
2. Never cite a passage that does not directly support the specific claim it follows.
3. Never put multiple citations in a single bracket. Correct: "… [abc:4] and … [abc:7]." \
Wrong: "[abc:4, abc:7]."
4. Never invent citations. If a claim cannot be grounded in the provided passages, \
omit the claim or acknowledge the gap.
5. The guidance_id may contain hyphens, underscores, or alphanumeric characters. \
Preserve them exactly.
6. After each citation bracket, with NO space, append the shortest verbatim span \
from that passage's <text> that supports the claim, wrapped in <q></q> — e.g. \
[72674:3]<q>a risk-based approach to software validation aligned with the intended \
use</q>. Copy the characters EXACTLY from the <text>: do not paraphrase, summarise, \
re-order, or fix typos. Keep it to a SINGLE sentence of at most 25 words — quote the \
specific phrase that supports the claim, never the whole passage. If you cannot copy \
a verbatim supporting span, OMIT <q> entirely (a bare [guidance_id:chunk_index] is \
acceptable); never invent, paraphrase, or guess a quote. The <q> wrapper never goes \
inside the bracket and never replaces it.

════════════════════════════════════════════
STRUCTURE RULES
════════════════════════════════════════════
- Follow the section headings in the outline provided. Do not add or remove sections.
- Use plain prose within sections; bullet lists are acceptable for enumerated \
requirements or steps.
- Keep the tone professional and precise. Do not hedge unless the guidance hedges.
- Maximum answer length: ~600 words. Be concise; do not pad.

════════════════════════════════════════════
GROUNDED REFUSAL (when passages are empty or insufficient)
════════════════════════════════════════════
If <passages> is empty or no passage covers the question, respond with exactly:
"The corpus does not contain sufficient evidence to answer this question. \
No citations can be provided."
Do not fabricate content in this case.

════════════════════════════════════════════
REVISION MODE
════════════════════════════════════════════
When a <prior_draft> and <critic_notes> are provided, edit the draft in place:
- Address every "hard" severity note precisely.
- Address "soft" severity notes if they improve accuracy without introducing new issues.
- Do NOT rewrite sections that have no associated notes.
- Do NOT regress correct citations in untouched sections.
- Keep the <q>…</q> supporting quote on every citation you retain or rewrite \
(rule 6); do not drop quotes when editing.
- Return the complete revised draft, not a diff.
"""


def _format_passages_xml(passages: list[RetrievedPassage]) -> str:
    """Format retrieved passages as XML for the analyst prompt.

    This is the UNCHANGED passage XML format from Day 3 api.py so that
    _resolve_citations() continues to work without modification.
    """
    parts = ["<passages>"]
    for p in passages:
        parts.append(
            f'<passage guidance_id="{p.guidance_id}" chunk_index="{p.chunk_index}">\n'
            f"<title>{p.guidance_title}</title>\n"
            f"<text>{p.text}</text>\n"
            f"</passage>"
        )
    parts.append("</passages>")
    return "\n".join(parts)


def format_user_prompt(request: QueryRequest, passages: list[RetrievedPassage]) -> str:
    """Build the user-turn message for the first-pass analyst call.

    Moved from api.py (Day 3). Passage XML format is UNCHANGED so that
    _resolve_citations() and the citation contract (ADR 0006) are unaffected.
    """
    parts = [_format_passages_xml(passages)]
    parts.append("")
    parts.append(f"Question: {request.query}")
    if request.product_context:
        parts.append(f"Product context: {request.product_context}")
    parts.append(
        "\nAnswer based on the passages above. "
        "Cite sources as [guidance_id:chunk_index]."
    )
    return "\n".join(parts)


def _format_revision_prompt(
    query: str,
    product_context: str,
    passages: list[RetrievedPassage],
    prior_draft: str,
    critic_notes: list[CriticNote],
    outline: str,
) -> str:
    parts = [_format_passages_xml(passages)]
    parts.append("")
    parts.append(f"Question: {query}")
    if product_context:
        parts.append(f"Product context: {product_context}")
    parts.append("")
    parts.append("<prior_draft>")
    parts.append(prior_draft)
    parts.append("</prior_draft>")
    parts.append("")
    parts.append("<critic_notes>")
    for note in critic_notes:
        key = note.citation_key or "claim-level"
        parts.append(f'<note key="{key}" severity="{note.severity}">{note.issue}</note>')
    parts.append("</critic_notes>")
    parts.append("")
    parts.append(
        "Revise the draft in place, addressing the critic notes above. "
        "Do not rewrite sections that have no associated notes. "
        "Return the complete revised draft."
    )
    if outline:
        parts.append(f"\nOutline to follow:\n{outline}")
    return "\n".join(parts)


def run_analyst(state: dict[str, Any]) -> dict[str, Any]:
    """Node function: synthesize or revise a draft answer.

    Returns partial state dict with draft and token_usage.
    """
    query: str = state["query"]
    product_context: str = state.get("product_context", "")
    outline: str = state.get("outline", "")
    passages: list[RetrievedPassage] = state.get("passages", [])
    critic_notes: list[CriticNote] = state.get("critic_notes", [])
    revision_count: int = state.get("revision_count", 0)

    llm = get_llm()

    if revision_count == 0:
        # First pass: full synthesis.
        request = QueryRequest(query=query, product_context=product_context)
        user_message = format_user_prompt(request, passages)
        if outline:
            user_message += f"\n\nAnalysis outline to follow:\n{outline}"
    else:
        # Revision pass: edit in place.
        prior_draft: str = state.get("draft", "")
        user_message = _format_revision_prompt(
            query, product_context, passages, prior_draft, critic_notes, outline
        )

    lf = get_langfuse()
    span_name = "analyst" if revision_count == 0 else f"analyst:rev{revision_count}"
    span_cm = (
        lf.start_as_current_observation(
            name=span_name,
            as_type="span",
            input={"query": query, "revision_count": revision_count, "passage_count": len(passages)},
        )
        if lf is not None
        else contextlib.nullcontext(None)
    )
    with span_cm as span:
        gen_cm = (
            span.start_as_current_observation(
                name="anthropic:analyst",
                as_type="generation",
                model=settings.analyst_model,
            )
            if span is not None
            else contextlib.nullcontext(None)
        )
        with gen_cm as gen:
            message = llm.complete(
                model=settings.analyst_model,
                max_tokens=1500,
                system=[
                    TextBlockParam(
                        type="text",
                        text=_SYSTEM_PROMPT,
                        cache_control=CacheControlEphemeralParam(type="ephemeral"),
                    )
                ],
                messages=[{"role": "user", "content": user_message}],
            )
            if gen is not None:
                gen.update(
                    usage_details={
                        "input": message.usage.input_tokens,
                        "output": message.usage.output_tokens,
                    },
                )

        draft = ""
        for block in message.content:
            if hasattr(block, "text"):
                draft = block.text
                break

        log.info(
            "analyst.complete",
            session_id=state.get("session_id"),
            revision_count=revision_count,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

        if span is not None:
            span.update(output={"draft_preview": draft[:200], "revision_count": revision_count})

        suffix = "" if revision_count == 0 else f"_rev{revision_count}"
        return {
            "draft": draft,
            "token_usage": {
                f"analyst_input{suffix}": message.usage.input_tokens,
                f"analyst_output{suffix}": message.usage.output_tokens,
            },
        }
