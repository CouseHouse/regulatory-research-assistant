"""Tests for the key-fact-coverage judge parse path (Day 7 prose-wrapping fix).

Day 6 bug: the Haiku key-fact judge wrapped its JSON reply in prose
("Here is the JSON: {...}"), strict json.loads rejected it, both retries
failed → KeyFactCoverageScorer returned score=None (N/A) on all 30 cases.

The fix constrains the judge at the source: KeyFactCoverageScorer now passes
prefill="{" to judge_call, which appends an assistant turn so the model must
continue from "{" (it physically cannot open with prose). judge_call prepends
the "{" back, so the scorer receives a complete JSON string and the strict
parser succeeds. Strictness is intentionally retained (one retry, then None).

PositionQualityScorer is deliberately NOT covered here — it already parsed
cleanly at Day 6 (0.947) and was left untouched (no prefill).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from rra.evals.dataset import GoldenCase  # noqa: E402
from rra.evals.judge import judge_call  # noqa: E402
from rra.evals.scorers import (  # noqa: E402
    AgentResponse,
    KeyFactCoverageScorer,
)


def _case(facts: tuple[str, ...]) -> GoldenCase:
    return GoldenCase(
        id="test-001",
        difficulty="easy",
        query="q",
        product_context="",
        expected_facts=facts,
        expected_guidance_ids=(),
    )


def _response(text: str = "some answer") -> AgentResponse:
    return AgentResponse(answer_text=text, citations=[], retrieved_passages=[])


# ─── judge_call: prefill mechanism (the actual fix) ─────────────────────────

def test_judge_call_prefill_appends_assistant_turn_and_prepends_brace():
    """With prefill, judge_call appends an assistant "{" turn and prepends the
    "{" back onto the (prefill-stripped) model reply → a complete JSON string.

    The model is mocked to return ONLY the continuation after "{" — exactly
    what the API returns for a prefilled turn. A prose-prone model can no
    longer emit "Here is the JSON:" because its first token follows "{".
    """
    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(text='"present": [true, false]}')]
    )
    with patch("rra.evals.judge._get_client", return_value=fake):
        result = judge_call("claude-haiku-4-5", "grade this", prefill="{")

    assert result == '{"present": [true, false]}'

    sent = fake.messages.create.call_args.kwargs["messages"]
    assert sent[-1] == {"role": "assistant", "content": "{"}


def test_judge_call_without_prefill_is_unchanged():
    """No prefill → no assistant turn appended, text returned verbatim.
    Guards that PositionQualityScorer's path (prefill=None) is untouched.
    """
    fake = MagicMock()
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"score": 5}')]
    )
    with patch("rra.evals.judge._get_client", return_value=fake):
        result = judge_call("claude-sonnet-4-6", "grade this")

    assert result == '{"score": 5}'
    sent = fake.messages.create.call_args.kwargs["messages"]
    assert all(m["role"] != "assistant" for m in sent)


# ─── KeyFactCoverageScorer: parse path now yields a real score, not N/A ─────

def test_keyfact_scorer_returns_real_score_with_prefilled_json():
    """The prose-wrapping bug is gone: a judge that (post-prefill) returns raw
    JSON yields a real score, not None. Also asserts the scorer opts into the
    prefill by passing prefill="{" to the judge.
    """
    calls = {}

    def fake_judge(model, prompt, prefill=None):
        calls["prefill"] = prefill
        # Simulates judge_call's contract: a complete JSON string (the "{" has
        # already been prepended back). Previously this arrived prose-wrapped.
        return '{"present": [true, false], "notes": "ok"}'

    scorer = KeyFactCoverageScorer(fake_judge, model="claude-haiku-4-5")
    result = scorer.score(_case(("fact a", "fact b")), _response())

    assert result.score == 0.5  # 1 of 2 present — a REAL number, not None
    assert result.score is not None
    assert calls["prefill"] == "{"


def test_keyfact_scorer_preserves_strictness_on_persistent_garbage():
    """Strictness guarantee retained: if the judge returns non-JSON on BOTH
    attempts, the scorer still returns score=None (N/A) — never a silent 0.0.
    """
    def garbage_judge(model, prompt, prefill=None):
        return "I refuse to emit JSON today."

    scorer = KeyFactCoverageScorer(garbage_judge, model="claude-haiku-4-5")
    result = scorer.score(_case(("fact a",)), _response())

    assert result.score is None
    assert "non-JSON after 2 attempts" in result.detail["reason"]


# ─── Integration: scorer → REAL judge_call → prefill (the Day-6 path) ────────

def test_keyfact_scorer_through_real_judge_call_neutralizes_prose_model():
    """End-to-end over the REAL judge_call (not a fake judge): the Day-6 case
    that returned N/A on all 30 cases now yields a real score.

    At Day 6 the Haiku judge opened with prose ("Here is the JSON: {...}") and
    strict json.loads rejected every reply → score=None everywhere. With the fix,
    the scorer passes prefill="{", so the API is handed an assistant turn that
    already contains "{"; the model can only *continue* the object and physically
    cannot emit a "Here is the JSON:" prefix. We mock the client to return that
    constrained continuation; judge_call prepends "{" back and the scorer parses a
    complete object → a real score, not None.

    This is the one test that exercises scorer + judge_call + prefill together —
    the exact wired path that was N/A at Day 6. The earlier tests cover judge_call
    and the scorer in isolation.
    """
    fake = MagicMock()
    # What a previously-prose-prone model returns once forced to continue from "{":
    fake.messages.create.return_value = MagicMock(
        content=[MagicMock(text='"present": [true, false], "notes": "ok"}')]
    )
    with patch("rra.evals.judge._get_client", return_value=fake):
        scorer = KeyFactCoverageScorer(judge_call, model="claude-haiku-4-5")
        result = scorer.score(_case(("fact a", "fact b")), _response())

    assert result.score == 0.5          # 1 of 2 present — a REAL number, not None
    assert result.score is not None
    # The scorer actually opted into the prefill: an assistant "{" turn was sent.
    sent = fake.messages.create.call_args.kwargs["messages"]
    assert sent[-1] == {"role": "assistant", "content": "{"}
