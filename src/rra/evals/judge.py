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


def judge_call(model: str, prompt: str) -> str:
    """Make one judge call and return the raw text response.

    Callers (scorers) handle JSON parsing and retry logic; this function
    only handles the transport. Max 512 tokens is sufficient for the
    structured JSON the scorers request.
    """
    msg = _get_client().messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
