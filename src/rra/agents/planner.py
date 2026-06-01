"""Planner agent: decomposes a user query into sub-questions and an outline.

Model: claude-sonnet-4-6 (settings.planner_model).
Prompt caching applied to system prompt (padded with few-shot examples to
exceed the 1024-token Sonnet caching threshold).
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
from pydantic import BaseModel, Field

from rra.config import settings

log = structlog.get_logger(__name__)

# System prompt for the planner. Few-shot examples push it past the 1024-token
# cache threshold so subsequent calls in the same session hit the cache.
_SYSTEM_PROMPT = """\
You are a regulatory research planning agent specializing in FDA medical device \
submissions. Your role is to decompose a user question into 2–4 targeted \
retrieval sub-questions and produce a concise analysis outline.

DECOMPOSITION RULES:
- Each sub-question must target a DISTINCT aspect of the original query.
- Sub-questions should be self-contained (answerable independently from the corpus).
- Expand regulatory acronyms in sub-questions (e.g., "PMA" → "Premarket Approval").
- 2 sub-questions for narrow questions, 3–4 for broad comparative questions.
- Never repeat the original query verbatim as a sub-question.

OUTLINE RULES:
- 2–4 section headings, in logical order (background → requirements → process → exceptions).
- Each heading is a short phrase (≤ 8 words), not a full sentence.
- The outline guides synthesis structure, not retrieval.

OUTPUT FORMAT:
You must call the `plan_query` tool with your decomposition. Do not output text \
outside of the tool call.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE 1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
User question: "When does a device modification require a new 510(k)?"

Tool call:
{
  "sub_questions": [
    "What changes to a cleared 510(k) device trigger the requirement to submit a new 510(k)?",
    "How does FDA define 'significant change' versus minor modification for 510(k) devices?",
    "What is the intended use and technological characteristics comparison in the 510(k) decision process?"
  ],
  "outline": "1. Regulatory trigger for new submission\\n2. Significant vs. minor change criteria\\n3. Intended use and technological characteristics test"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE 2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
User question: "What are the predicate device requirements for a 510(k) submission?"

Tool call:
{
  "sub_questions": [
    "What makes a device legally marketed enough to serve as a predicate for a 510(k) submission?",
    "How does FDA evaluate substantial equivalence to a predicate device in a 510(k)?",
    "Can a device use multiple predicates or a predicate-of-a-predicate in a 510(k) submission?"
  ],
  "outline": "1. Eligible predicate device criteria\\n2. Substantial equivalence evaluation\\n3. Split and multiple predicates"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLE 3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
User question: "What are the De Novo pathway requirements?"

Tool call:
{
  "sub_questions": [
    "What eligibility criteria must a device meet to use the De Novo classification pathway?",
    "What are the submission content requirements for a De Novo request to FDA?"
  ],
  "outline": "1. De Novo eligibility criteria\\n2. Submission content and format requirements"
}
"""

_PLAN_TOOL: list[ToolParam] = [
    ToolParam(
        name="plan_query",
        description="Output the structured decomposition of the user's regulatory query.",
        input_schema={
            "type": "object",
            "properties": {
                "sub_questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "2–4 targeted retrieval sub-questions.",
                },
                "outline": {
                    "type": "string",
                    "description": "Analysis outline (2–4 section headings, newline-separated).",
                },
            },
            "required": ["sub_questions", "outline"],
        },
    )
]


class PlannerOutput(BaseModel):
    # max_length not enforced here — node function truncates to 4.
    # The tool schema (maxItems=4) prevents over-generation in normal operation.
    sub_questions: list[str] = Field(min_length=1)
    outline: str


def run_planner(state: dict[str, Any]) -> dict[str, Any]:
    """Node function: decompose query into sub-questions + outline.

    Returns partial state dict with sub_questions, outline, and token_usage.
    """
    query: str = state["query"]
    product_context: str = state.get("product_context", "")

    user_content = f"Question: {query}"
    if product_context:
        user_content += f"\nProduct context: {product_context}"

    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    message = client.messages.create(
        model=settings.planner_model,
        max_tokens=512,
        system=[
            TextBlockParam(
                type="text",
                text=_SYSTEM_PROMPT,
                cache_control=CacheControlEphemeralParam(type="ephemeral"),
            )
        ],
        messages=[{"role": "user", "content": user_content}],
        tools=_PLAN_TOOL,
        tool_choice=ToolChoiceToolParam(type="tool", name="plan_query"),
    )

    # Extract tool-use block
    tool_input: dict[str, Any] = {}
    for block in message.content:
        if block.type == "tool_use" and block.name == "plan_query":
            tool_input = dict(block.input)
            break

    if not tool_input:
        log.warning("planner.no_tool_output", query=query[:80])
        tool_input = {"sub_questions": [query], "outline": ""}

    try:
        output = PlannerOutput.model_validate(tool_input)
    except Exception:
        log.warning("planner.parse_error", raw=json.dumps(tool_input)[:200])
        output = PlannerOutput(sub_questions=[query], outline="")

    # Truncate to 4 if model exceeded the limit (shouldn't happen with tool schema)
    if len(output.sub_questions) > 4:
        log.warning("planner.too_many_sub_questions", count=len(output.sub_questions))
        output = PlannerOutput(
            sub_questions=output.sub_questions[:4], outline=output.outline
        )

    log.info(
        "planner.complete",
        session_id=state.get("session_id"),
        sub_question_count=len(output.sub_questions),
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
    )

    return {
        "sub_questions": output.sub_questions,
        "outline": output.outline,
        "token_usage": {
            "planner_input": message.usage.input_tokens,
            "planner_output": message.usage.output_tokens,
        },
    }
