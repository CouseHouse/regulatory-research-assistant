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


def _emit_judge_observation(model: str, trace_id: str, usage: object) -> None:
    """Attach one judge call's token usage to an already-closed eval-case trace.

    The scorers run AFTER run_agent's eval-case span has exited (run.py), so
    there is no live span to nest under — we link to the trace by id, exactly as
    emit_scores does for scores (langfuse_eval.py). A generation observation
    carrying model + usage_details is what makes self-hosted Langfuse COMPUTE the
    judge's cost (otherwise the 120 judge calls per paid run are invisible and
    traced cost reads as a floor, not the bill).

    Gated/no-op by construction: callers only pass a non-None trace_id on
    --allow-population runs, and get_langfuse() returns None when keys are
    absent. Langfuse is auxiliary observability — any failure here is swallowed
    so a tracing hiccup can never fail a judge (mirrors maybe_sync_langfuse).
    """
    from rra.tracing import get_langfuse  # SHARED singleton — never a 2nd client

    lf = get_langfuse()
    if lf is None:
        return
    try:
        gen = lf.start_observation(
            trace_context={"trace_id": trace_id},
            name="llm-judge",
            as_type="generation",
            model=model,
            usage_details={
                "input": usage.input_tokens,
                "output": usage.output_tokens,
            },
            metadata={"component": "eval-judge"},
        )
        gen.end()
    except Exception:  # noqa: BLE001 — tracing must never fail a judge call
        pass


def judge_call(
    model: str,
    prompt: str,
    prefill: str | None = None,
    trace_id: str | None = None,
) -> str:
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

    If ``trace_id`` is given (the per-case eval-case trace, threaded from the
    scorer via response.raw_trace_id), this call's token usage is attached to
    that trace as a generation so Langfuse prices it. trace_id=None (the
    default, and what non-population runs pass) is a hard no-op: judge_call still
    works with no Langfuse at all.
    """
    messages = [{"role": "user", "content": prompt}]
    if prefill is not None:
        messages.append({"role": "assistant", "content": prefill})

    msg = _get_client().messages.create(
        model=model,
        max_tokens=512,
        messages=messages,
    )
    if trace_id is not None:
        _emit_judge_observation(model, trace_id, msg.usage)
    text = msg.content[0].text
    return prefill + text if prefill is not None else text
