"""Thin Anthropic SDK wrapper used by LLM-as-judge scorers.

A single module-level client is reused across all scorer calls in a run.
The callable returned by make_judge_client() matches the (model, prompt) -> str
interface both KeyFactCoverageScorer and PositionQualityScorer expect.
"""
from __future__ import annotations

import anthropic

from rra.config import settings

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value()
        )
    return _client


def judge_call(model: str, prompt: str, prefill: str | None = None) -> str:
    """Make one judge call and return the raw text response.

    Callers (scorers) handle JSON parsing and retry logic; this function
    only handles the transport. Max 512 tokens is sufficient for the
    structured JSON the scorers request.

    If ``prefill`` is given, it is appended as an assistant turn so the model
    is forced to *continue* from it rather than open with prose (Anthropic
    assistant-prefill). The API does not echo the prefill in its response, so
    we prepend it back onto the returned text — the caller receives the full
    string. KeyFactCoverageScorer passes prefill="{" to force a raw-JSON reply
    (the Day 6 prose-wrapping bug). Default None leaves behaviour unchanged for
    callers that don't opt in (e.g. PositionQualityScorer).
    """
    messages = [{"role": "user", "content": prompt}]
    if prefill is not None:
        messages.append({"role": "assistant", "content": prefill})

    msg = _get_client().messages.create(
        model=model,
        max_tokens=512,
        messages=messages,
    )
    text = msg.content[0].text
    return prefill + text if prefill is not None else text
