"""Critic agent: citation verification and verdict.

Model: claude-sonnet-4-6 (settings.critic_model).

Pre-validates every [guid:idx] citation via check_citation (in-process,
ADR 0010/0011) before the LLM call, injecting the results as <citation_checks>
XML.

Day 5 ran address-only (key-existence, quoted_text=None). Day 8 (critic-flip,
ADR 0013) parses each citation's optional <q>…</q> supporting quote with the
SHARED parser (rra.citations.parse_answer) and passes it to check_citation as
quoted_text, activating ADR-0010 faithfulness matching. A bare citation (no <q>)
stays address-only, so a valid bare address is never a faithfulness failure. The
<citation_checks> XML now distinguishes an address failure (cited chunk missing)
from a quote-faithfulness failure (chunk present, quote not faithful).

ToolError(retryable=True) → inconclusive; critic instructed not to penalize those.
ToolError(retryable=False) and verified=False → evidence for revise note (ADR 0010
retryability invariant).

Verdict semantics (ADR 0009 — unchanged):
  approve   — all citations valid; exit graph.
  revise    — specific citations are fixable; route back to analyst.
  escalate  — question cannot be grounded in available corpus; exit immediately.

The critic node increments revision_count when verdict is "revise", and sets
cap_hit = True when the new revision_count reaches settings.max_critic_revisions.
This lets the routing function do a simple equality check on cap_hit.

Prompt caching applied to system prompt (exceeds 1024-token threshold).
"""
from __future__ import annotations

import contextlib
import json
from typing import Any

import structlog
from anthropic.types import (
    CacheControlEphemeralParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
)

from rra.agents.types import CriticNote, CriticOutput
from rra.citations import parse_answer
from rra.config import settings
from rra.ports.llm import get_llm
from rra.mcp_server.tools import CitationCheckResult, ToolError, check_citation
from rra.schemas import RetrievedPassage
from rra.tracing import get_langfuse

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a citation verification agent for FDA regulatory analysis. Your task is \
to audit every inline citation in a draft regulatory analysis for accuracy and \
relevance, then return a structured verdict.

════════════════════════════════════════════
CITATION FORMAT
════════════════════════════════════════════
Citations appear as [guidance_id:chunk_index] inline in the draft, e.g. [abc123:4]. \
A citation may be followed by a verbatim supporting quote in <q>…</q> — e.g. \
[abc123:4]<q>substantial equivalence</q>. A bare citation with no <q> is also allowed. \
You are provided with the full set of source passages that were available to the \
analyst. A citation is valid only if:
  (a) the cited (guidance_id, chunk_index) pair matches an actual provided passage, AND
  (b) that passage plausibly supports the specific claim it follows.
Any <q> quote's faithfulness to the cited passage is checked for you deterministically \
— see CITATION CHECKS below. A bare citation (no quote) is checked for address only.

════════════════════════════════════════════
VERDICT CRITERIA
════════════════════════════════════════════
approve
  Use when ALL citations pass both checks (a) and (b), and the overall answer is \
well-grounded in the provided passages. Minor wording issues do not require revision.

revise
  Use when one or more specific citations fail check (a) or (b), or carry an unfaithful \
supporting quote (reason="quote_unfaithful"), but the failure is fixable: a different \
passage in the provided set could support the claim, the claim could be narrowed to \
match what the cited passage actually says, or the quote could be replaced with a \
verbatim span. For each fixable issue, include a CriticNote. Use severity "hard" for \
citations that directly contradict, do not appear in the provided passages, or carry an \
unfaithful quote; use "soft" for overclaimed or imprecise citations that could be improved.

escalate
  Use when the question fundamentally cannot be grounded in the available corpus — \
e.g., the topic is not covered by any provided passage, or the analyst produced a \
refusal because no relevant evidence was available. Escalate on ANY pass (including \
the first); do not use escalate for individual bad citations. When escalating, notes \
may be empty.

════════════════════════════════════════════
NOTE FORMAT (for revise verdict)
════════════════════════════════════════════
Each note must identify:
  citation_key: "guidance_id:chunk_index" (the bad citation), or null for a \
claim-level issue not tied to a specific citation.
  issue: concise description of what is wrong (≤ 40 words).
  severity: "hard" (must fix) or "soft" (should fix).

════════════════════════════════════════════
GROUNDED REFUSAL HANDLING
════════════════════════════════════════════
If the draft contains "The corpus does not contain sufficient evidence", the analyst \
has already detected a corpus gap. Use verdict "escalate" in this case.

════════════════════════════════════════════
CITATION CHECKS (deterministic pre-validation)
════════════════════════════════════════════
You will receive a <citation_checks> block with one <check> entry per inline citation. \
Each entry carries verified="true|false|unknown", inconclusive="true|false", and — when \
the check actually ran — a check="address|quote" attribute. The reason a citation fails \
is NOT monolithic; keep these cases distinct:

- verified="true" check="address" — the cited chunk exists and the analyst supplied NO \
quote (a bare citation, which is allowed). source_text is the authoritative stored text; \
assess whether it supports the claim, but do NOT flag the citation as unfaithful merely \
for lacking a quote.
- verified="true" check="quote" — the cited chunk exists AND the analyst's quoted_text \
is faithfully present in it. Assess whether the quoted span supports the claim.
- verified="false" reason="address_not_found" — the cited (guidance_id, chunk_index) does \
not resolve to any corpus chunk. This is an ADDRESS error; it is definitive — include a \
hard-severity CriticNote.
- verified="false" reason="quote_unfaithful" — the cited chunk EXISTS, but the analyst's \
quoted_text is not faithfully present in it (the quote misquotes or misrepresents the \
source; similarity is below threshold). This is a QUOTE-FAITHFULNESS error, distinct from \
a missing address — include a hard-severity CriticNote so the analyst replaces the quote \
with a verbatim span or narrows the claim. Do NOT escalate for this; it is fixable.
- inconclusive="true" (tool error) — the check could not run due to an infrastructure \
failure. Treat this as if the check was not run — do NOT use it as evidence for a revise \
verdict, and do NOT penalize the draft for it.

You must call the `submit_verdict` tool. Do not output text outside of the tool call.
"""

_VERDICT_TOOL: list[ToolParam] = [
    ToolParam(
        name="submit_verdict",
        description="Submit the citation audit verdict and any issues found.",
        input_schema={
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["approve", "revise", "escalate"],
                    "description": "Overall verdict.",
                },
                "notes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "citation_key": {
                                "type": ["string", "null"],
                                "description": "The bad citation key, or null.",
                            },
                            "issue": {"type": "string"},
                            "severity": {
                                "type": "string",
                                "enum": ["hard", "soft"],
                            },
                        },
                        "required": ["citation_key", "issue", "severity"],
                    },
                    "description": "List of issues (empty for approve/escalate).",
                },
            },
            "required": ["verdict", "notes"],
        },
    )
]


def run_critic(state: dict[str, Any]) -> dict[str, Any]:
    """Node function: verify citations and return verdict.

    Increments revision_count when verdict is "revise".
    Sets cap_hit = True when the new revision_count reaches the configured cap.

    Returns partial state dict: verdict, critic_notes, revision_count, cap_hit,
    and token_usage.
    """
    draft: str = state.get("draft", "")
    passages: list[RetrievedPassage] = state.get("passages", [])
    query: str = state.get("query", "")
    revision_count: int = state.get("revision_count", 0)

    lf = get_langfuse()
    span_cm = (
        lf.start_as_current_observation(
            name="critic",
            as_type="span",
            input={"draft_preview": draft[:200], "revision_count": revision_count},
        )
        if lf is not None
        else contextlib.nullcontext(None)
    )
    with span_cm as span:
        # ── Force-verdict gate (TEST/EVAL ONLY) ───────────────────────────────
        # When set, skip the LLM call and emit the configured verdict directly.
        # Downstream logic (revision_count increment, cap_hit, routing) is unchanged.
        # The critic span is still emitted so forced-run traces show the loop.
        force_verdict = settings.critic_force_verdict
        if force_verdict is not None:
            log.warning(
                "critic.force_verdict",
                verdict=force_verdict,
                session_id=state.get("session_id"),
            )
            forced_notes: list[CriticNote] = (
                [CriticNote(citation_key=None, issue="forced verdict for testing", severity="soft")]
                if force_verdict == "revise"
                else []
            )
            new_revision_count = revision_count
            cap_hit = False
            if force_verdict == "revise":
                new_revision_count = revision_count + 1
                if new_revision_count >= settings.max_critic_revisions:
                    cap_hit = True
            if span is not None:
                span.update(
                    output={"verdict": force_verdict, "forced": True, "cap_hit": cap_hit},
                )
            suffix = "" if revision_count == 0 else f"_rev{revision_count}"
            return {
                "verdict": force_verdict,
                "critic_notes": forced_notes,
                "revision_count": new_revision_count,
                "cap_hit": cap_hit,
                "token_usage": {
                    f"critic_input{suffix}": 0,
                    f"critic_output{suffix}": 0,
                },
            }

        # Build the set of valid (guidance_id, chunk_index) pairs from provided passages.
        valid_keys = {(p.guidance_id, p.chunk_index) for p in passages}
        passage_map = {(p.guidance_id, p.chunk_index): p for p in passages}

        # Build passage summary for the critic prompt.
        passage_summary_parts = ["<passages>"]
        for p in passages:
            passage_summary_parts.append(
                f'<passage guidance_id="{p.guidance_id}" chunk_index="{p.chunk_index}">\n'
                f"<title>{p.guidance_title}</title>\n"
                f"<text>{p.text}</text>\n"
                f"</passage>"
            )
        passage_summary_parts.append("</passages>")
        passage_xml = "\n".join(passage_summary_parts)

        # ── check_citation pre-validation (ADR 0010/0011/0013) ───────────────
        # Parse every inline [guidance_id:chunk_index] citation AND its optional
        # <q>…</q> supporting quote with the SHARED parser (rra.citations.parse_answer)
        # — the same parser the API resolver (api.py) and the eval runner (run.py)
        # use, so the critic, the product surface, and the measurement surface can
        # never drift on what a citation or quote is (ADR 0013).
        #
        # Day-8 critic-flip: the analyst's quote now reaches check_citation as
        # quoted_text, activating ADR-0010 faithfulness matching. A bare citation
        # (no <q>, allowed by the analyst contract) parses to quote=None →
        # check_citation stays in key-existence mode, so a valid bare address is
        # NOT penalised as a faithfulness failure.
        #
        # Dedup on the FULL (guid, idx, quote) triple, not the address alone:
        # collapsing on the address would hide a same-chunk/different-quote pair
        # (one faithful, one not) now that the quote is what gets checked. Exact
        # duplicate triples still collapse to one DB read + one <check>; order is
        # preserved (dict.fromkeys) so the XML is deterministic.
        _, triples = parse_answer(draft)
        inline_citations: list[tuple[str, int, str | None]] = list(
            dict.fromkeys(triples)
        )

        # (citation_key, quoted_text|None, result) per check — a list, not a dict
        # keyed by citation_key, so two checks of the SAME address with different
        # quotes both survive into the XML.
        checks: list[tuple[str, str | None, CitationCheckResult | ToolError]] = []
        for guidance_id, chunk_index, quote in inline_citations:
            ckey = f"{guidance_id}:{chunk_index}"
            cc_cm = (
                span.start_as_current_observation(
                    name="check_citation",
                    as_type="span",
                    input={"citation_key": ckey, "has_quote": quote is not None},
                )
                if span is not None
                else contextlib.nullcontext(None)
            )
            with cc_cm as cc_span:
                try:
                    cc_result = check_citation(
                        claim=query,
                        guidance_id=guidance_id,
                        chunk_index=chunk_index,
                        quoted_text=quote,
                    )
                    checks.append((ckey, quote, cc_result))
                    if cc_span is not None:
                        cc_span.update(
                            output={
                                "verified": cc_result.verified,
                                "similarity_score": cc_result.similarity_score,
                            }
                        )
                except ToolError as exc:
                    checks.append((ckey, quote, exc))
                    if cc_span is not None:
                        cc_span.update(
                            output={"error": exc.code, "retryable": exc.retryable}
                        )
                except Exception as exc:
                    tool_err = ToolError(
                        code="UNKNOWN",
                        message=str(exc),
                        tool="check_citation",
                        retryable=True,
                    )
                    checks.append((ckey, quote, tool_err))
                    if cc_span is not None:
                        cc_span.update(output={"error": "UNKNOWN", "retryable": True})

        # Build <citation_checks> XML. After the flip, verified="false" is no
        # longer monolithic: it splits into an ADDRESS failure (cited chunk
        # absent) and a QUOTE-FAITHFULNESS failure (chunk present, quote not
        # faithful). Each <check> carries check="address|quote" plus a machine
        # reason so the critic can tell a hallucinated address from an unfaithful
        # quote (ADR 0013); a valid bare address stays a clean key-existence pass.
        check_parts = ["<citation_checks>"]
        for ckey, quote, result in checks:
            if isinstance(result, ToolError):
                label = "transient" if result.retryable else "permanent"
                check_parts.append(
                    f'  <check citation_key="{ckey}" verified="unknown" inconclusive="true">\n'
                    f"    <reason>tool error: {result.code} ({label})</reason>\n"
                    f"  </check>"
                )
            elif result.verified and quote is None:
                # Bare citation, address resolves — key-existence mode (no quote).
                check_parts.append(
                    f'  <check citation_key="{ckey}" verified="true" inconclusive="false" check="address">\n'
                    f"    <source_text>{result.source_text}</source_text>\n"
                    f"  </check>"
                )
            elif result.verified:
                # Quote supplied AND faithfully present in the cited chunk.
                check_parts.append(
                    f'  <check citation_key="{ckey}" verified="true" inconclusive="false" check="quote">\n'
                    f"    <quoted_text>{quote}</quoted_text>\n"
                    f"    <source_text>{result.source_text}</source_text>\n"
                    f"  </check>"
                )
            elif not result.source_text:
                # Address failure: the cited chunk does not exist in the corpus.
                check_parts.append(
                    f'  <check citation_key="{ckey}" verified="false" inconclusive="false" check="address" reason="address_not_found">\n'
                    f"    <reason>cited chunk does not exist in the corpus</reason>\n"
                    f"  </check>"
                )
            else:
                # Quote-faithfulness failure: chunk exists, quote not faithful.
                score = (
                    "n/a"
                    if result.similarity_score is None
                    else f"{result.similarity_score:.2f}"
                )
                check_parts.append(
                    f'  <check citation_key="{ckey}" verified="false" inconclusive="false" check="quote" reason="quote_unfaithful">\n'
                    f"    <quoted_text>{quote}</quoted_text>\n"
                    f"    <source_text>{result.source_text}</source_text>\n"
                    f"    <similarity_score>{score}</similarity_score>\n"
                    f"  </check>"
                )
        check_parts.append("</citation_checks>")
        citation_checks_xml = "\n".join(check_parts)

        user_content = (
            f"{passage_xml}\n\n"
            f"{citation_checks_xml}\n\n"
            f"<query>{query}</query>\n\n"
            f"<draft>\n{draft}\n</draft>\n\n"
            "Audit every citation in the draft using the <citation_checks> results. "
            "verified='true' check='address' — the chunk exists and no quote was supplied "
            "(a bare citation is allowed); assess support but do NOT flag it for lacking a "
            "quote. verified='true' check='quote' — the chunk exists and the quoted_text is "
            "faithfully present; assess whether it supports the claim. "
            "verified='false' reason='address_not_found' — the cited chunk does not exist; "
            "definitively wrong; note with hard severity. "
            "verified='false' reason='quote_unfaithful' — the chunk exists but the quoted_text "
            "is not faithfully present (it misrepresents the source); note with hard severity "
            "so the analyst fixes the quote or narrows the claim. "
            "inconclusive='true' (tool error) — treat as not-run; do not penalize. "
            "Return your verdict via the submit_verdict tool."
        )

        llm = get_llm()

        gen_cm = (
            span.start_as_current_observation(
                name="anthropic:critic",
                as_type="generation",
                model=settings.critic_model,
            )
            if span is not None
            else contextlib.nullcontext(None)
        )
        with gen_cm as gen:
            message = llm.complete(
                model=settings.critic_model,
                max_tokens=512,
                system=[
                    TextBlockParam(
                        type="text",
                        text=_SYSTEM_PROMPT,
                        cache_control=CacheControlEphemeralParam(type="ephemeral"),
                    )
                ],
                messages=[{"role": "user", "content": user_content}],
                tools=_VERDICT_TOOL,
                tool_choice=ToolChoiceToolParam(type="tool", name="submit_verdict"),
            )
            if gen is not None:
                gen.update(
                    usage_details={
                        "input": message.usage.input_tokens,
                        "output": message.usage.output_tokens,
                    },
                )

        # Extract tool-use block.
        tool_input: dict[str, Any] = {}
        for block in message.content:
            if block.type == "tool_use" and block.name == "submit_verdict":
                tool_input = dict(block.input)
                break

        if not tool_input:
            # Malformed output: treat as approve to avoid infinite loop (ADR 0009).
            log.error(
                "critic.no_tool_output",
                session_id=state.get("session_id"),
                draft_preview=draft[:120],
            )
            tool_input = {"verdict": "approve", "notes": []}

        try:
            critic_output = CriticOutput.model_validate(tool_input)
        except Exception:
            log.error(
                "critic.parse_error",
                session_id=state.get("session_id"),
                raw=json.dumps(tool_input)[:200],
            )
            critic_output = CriticOutput(verdict="approve", notes=[])

        # Validate notes — drop any that reference non-existent passages.
        validated_notes: list[CriticNote] = []
        for note in critic_output.notes:
            if note.citation_key is not None:
                parts_split = note.citation_key.rsplit(":", 1)
                if len(parts_split) == 2:
                    try:
                        key: tuple[str, int] = (parts_split[0], int(parts_split[1]))
                    except ValueError:
                        key = ("", -1)
                    if key not in valid_keys and key not in passage_map:
                        log.debug(
                            "critic.note_references_unknown_passage",
                            citation_key=note.citation_key,
                        )
            validated_notes.append(note)

        if critic_output.verdict == "revise" and not validated_notes:
            log.warning(
                "critic.revise_with_no_notes",
                session_id=state.get("session_id"),
            )

        # Increment revision_count when issuing a revise verdict.
        new_revision_count = revision_count
        cap_hit = False
        if critic_output.verdict == "revise":
            new_revision_count = revision_count + 1
            if new_revision_count >= settings.max_critic_revisions:
                cap_hit = True
                log.info(
                    "critic.cap_hit",
                    session_id=state.get("session_id"),
                    revision_count=new_revision_count,
                )

        log.info(
            "critic.complete",
            session_id=state.get("session_id"),
            verdict=critic_output.verdict,
            note_count=len(validated_notes),
            revision_count=new_revision_count,
            cap_hit=cap_hit,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

        if span is not None:
            span.update(
                output={
                    "verdict": critic_output.verdict,
                    "note_count": len(validated_notes),
                    "cap_hit": cap_hit,
                },
            )

        suffix = "" if revision_count == 0 else f"_rev{revision_count}"
        return {
            "verdict": critic_output.verdict,
            "critic_notes": validated_notes,
            "revision_count": new_revision_count,
            "cap_hit": cap_hit,
            "token_usage": {
                f"critic_input{suffix}": message.usage.input_tokens,
                f"critic_output{suffix}": message.usage.output_tokens,
            },
        }
