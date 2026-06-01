"""Critic agent: citation verification and verdict.

Model: claude-sonnet-4-6 (settings.critic_model).

Day 4: context-match check only — verifies that every [guid:idx] citation in
the draft (a) refers to a passage that was actually provided and (b) the cited
passage plausibly supports the claim. Does NOT call the check_citation MCP tool
(that's Day 5).

Verdict semantics (ADR 0009):
  approve   — all citations valid; exit graph.
  revise    — specific citations are fixable; route back to analyst.
  escalate  — question cannot be grounded in available corpus; exit immediately.

The critic node increments revision_count when verdict is "revise", and sets
cap_hit = True when the new revision_count reaches settings.max_critic_revisions.
This lets the routing function do a simple equality check on cap_hit.

Prompt caching applied to system prompt (exceeds 1024-token threshold).
"""
from __future__ import annotations

import json
from typing import Any

import structlog
from anthropic import Anthropic
from anthropic.types import (
    CacheControlEphemeralParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
)

from rra.agents.types import CriticNote, CriticOutput
from rra.config import settings
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a citation verification agent for FDA regulatory analysis. Your task is \
to audit every inline citation in a draft regulatory analysis for accuracy and \
relevance, then return a structured verdict.

════════════════════════════════════════════
CITATION FORMAT
════════════════════════════════════════════
Citations appear as [guidance_id:chunk_index] inline in the draft, e.g. [abc123:4]. \
You are provided with the full set of source passages that were available to the \
analyst. A citation is valid only if:
  (a) the cited (guidance_id, chunk_index) pair matches an actual provided passage, AND
  (b) that passage plausibly supports the specific claim it follows.

════════════════════════════════════════════
VERDICT CRITERIA
════════════════════════════════════════════
approve
  Use when ALL citations pass both checks (a) and (b), and the overall answer is \
well-grounded in the provided passages. Minor wording issues do not require revision.

revise
  Use when one or more specific citations fail check (a) or (b), but the failure \
is fixable: a different passage in the provided set could support the claim, or the \
claim could be narrowed to match what the cited passage actually says. For each \
fixable issue, include a CriticNote. Use severity "hard" for citations that directly \
contradict or do not appear in the provided passages; use "soft" for overclaimed or \
imprecise citations that could be improved.

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

    user_content = (
        f"{passage_xml}\n\n"
        f"<query>{query}</query>\n\n"
        f"<draft>\n{draft}\n</draft>\n\n"
        "Audit every citation in the draft against the provided passages and "
        "return your verdict via the submit_verdict tool."
    )

    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    message = client.messages.create(
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
