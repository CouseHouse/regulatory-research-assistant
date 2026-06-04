"""Tests for rra.evals.langfuse_eval — the Langfuse dataset/score integration.

BUILD-ONLY verification: every test MOCKS the Langfuse client. No real Langfuse
API call is made and no eval is run, so nothing is populated (the integration is
gated until the critic-flip; see langfuse_eval module banner). These tests assert
the right dataset/score calls happen with the right args, that scores link to the
trace, that the SHARED tracing client is reused (no second client), and that the
population gate holds.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from unittest.mock import MagicMock, patch  # noqa: E402

from rra.evals.dataset import GoldenCase  # noqa: E402
from rra.evals.langfuse_eval import (  # noqa: E402
    GOLDEN_DATASET_NAME,
    POPULATION_GATED,
    emit_scores,
    maybe_sync_langfuse,
    push_golden_dataset,
    score_value_and_type,
    should_populate,
    sync_eval_to_langfuse,
)
from rra.evals.run import CaseRun  # noqa: E402
from rra.evals.scorers import AgentResponse, ScoreResult  # noqa: E402


# ─── Builders ────────────────────────────────────────────────────────────────


def _case(i: int) -> GoldenCase:
    return GoldenCase(
        id=f"easy-{i:03d}",
        difficulty="easy",
        query=f"query {i}",
        product_context=f"context {i}",
        expected_facts=(f"fact {i}a", f"fact {i}b"),
        expected_guidance_ids=(f"GUID-{i}",),
        notes=f"note {i}",
    )


def _response() -> AgentResponse:
    return AgentResponse(
        answer_text="an answer " * 50,  # >200 chars so the preview is exercised
        citations=[{"guidance_id": "GUID-1", "chunk_index": 0, "quoted_text": "q"}],
        retrieved_passages=[],
        raw_trace_id=None,
    )


def _numeric_score() -> ScoreResult:
    return ScoreResult("citation_validity", 0.92, True, {})


def _na_score() -> ScoreResult:
    return ScoreResult("citation_validity", None, False, {"reason": "zero citations — N/A"})


# ─── score_value_and_type ────────────────────────────────────────────────────


def test_score_value_and_type_numeric() -> None:
    value, dtype = score_value_and_type(_numeric_score())
    assert value == 0.92
    assert isinstance(value, float)
    assert dtype == "NUMERIC"


def test_score_value_and_type_na_is_categorical_not_dropped() -> None:
    """An N/A score becomes a visible CATEGORICAL 'n/a', never silently dropped."""
    value, dtype = score_value_and_type(_na_score())
    assert value == "n/a"
    assert dtype == "CATEGORICAL"


# ─── push_golden_dataset (deliverable a) ─────────────────────────────────────


def test_push_golden_dataset_creates_dataset_once() -> None:
    client = MagicMock()
    cases = [_case(1), _case(2), _case(3)]

    pushed = push_golden_dataset(client, cases)

    assert pushed == 3
    client.create_dataset.assert_called_once()
    assert client.create_dataset.call_args.kwargs["name"] == GOLDEN_DATASET_NAME


def test_push_golden_dataset_pushes_one_idempotent_item_per_case() -> None:
    client = MagicMock()
    cases = [_case(1), _case(2)]

    push_golden_dataset(client, cases)

    assert client.create_dataset_item.call_count == 2
    first = client.create_dataset_item.call_args_list[0].kwargs
    assert first["dataset_name"] == GOLDEN_DATASET_NAME
    assert first["id"] == "easy-001"  # stable id → idempotent upsert
    assert first["input"] == {"query": "query 1", "product_context": "context 1"}
    assert first["expected_output"] == {
        "expected_facts": ["fact 1a", "fact 1b"],
        "expected_guidance_ids": ["GUID-1"],
    }
    assert first["metadata"]["difficulty"] == "easy"


def test_push_golden_dataset_custom_name() -> None:
    client = MagicMock()
    push_golden_dataset(client, [_case(1)], dataset_name="other")
    assert client.create_dataset.call_args.kwargs["name"] == "other"
    assert client.create_dataset_item.call_args.kwargs["dataset_name"] == "other"


# ─── emit_scores (deliverable b) ─────────────────────────────────────────────


def test_emit_scores_links_every_score_to_the_trace() -> None:
    client = MagicMock()
    scores = [_numeric_score(), _na_score()]

    emitted = emit_scores(client, "trace-abc", scores)

    assert emitted == 2
    assert client.create_score.call_count == 2
    for call in client.create_score.call_args_list:
        assert call.kwargs["trace_id"] == "trace-abc"  # linked to the trace


def test_emit_scores_numeric_call_args() -> None:
    client = MagicMock()
    emit_scores(client, "t", [_numeric_score()])

    kwargs = client.create_score.call_args.kwargs
    assert kwargs["name"] == "citation_validity"
    assert kwargs["value"] == 0.92
    assert kwargs["data_type"] == "NUMERIC"
    assert kwargs["metadata"]["passed"] is True


def test_emit_scores_na_call_args() -> None:
    client = MagicMock()
    emit_scores(client, "t", [_na_score()])

    kwargs = client.create_score.call_args.kwargs
    assert kwargs["value"] == "n/a"
    assert kwargs["data_type"] == "CATEGORICAL"
    assert kwargs["metadata"]["passed"] is False
    assert "zero citations" in kwargs["comment"]


def test_emit_scores_empty_makes_no_calls() -> None:
    client = MagicMock()
    assert emit_scores(client, "t", []) == 0
    client.create_score.assert_not_called()


# ─── sync_eval_to_langfuse (orchestration) ───────────────────────────────────


def test_sync_pushes_dataset_opens_span_and_emits_scores() -> None:
    client = MagicMock()
    client.get_current_trace_id.return_value = "trace-1"
    runs = [
        CaseRun(_case(1), _response(), [_numeric_score(), _na_score()]),
        CaseRun(_case(2), _response(), [_numeric_score()]),
    ]

    summary = sync_eval_to_langfuse(client, runs)

    # Dataset pushed for both cases.
    client.create_dataset.assert_called_once()
    assert client.create_dataset_item.call_count == 2
    # One span per case (mirrors api.py's start_as_current_observation idiom).
    assert client.start_as_current_observation.call_count == 2
    span = client.start_as_current_observation.return_value.__enter__.return_value
    assert span.update.called
    # 3 scores total, all linked to the span's trace.
    assert client.create_score.call_count == 3
    for call in client.create_score.call_args_list:
        assert call.kwargs["trace_id"] == "trace-1"
    client.flush.assert_called_once()

    assert summary == {
        "dataset": GOLDEN_DATASET_NAME,
        "items": 2,
        "cases": 2,
        "scores": 3,
    }


def test_sync_records_error_case_without_scores() -> None:
    client = MagicMock()
    client.get_current_trace_id.return_value = "trace-err"
    runs = [CaseRun(_case(9), None, [], error="boom")]

    summary = sync_eval_to_langfuse(client, runs)

    # Span opened + output records the error; no scores for an errored case.
    span = client.start_as_current_observation.return_value.__enter__.return_value
    assert span.update.call_args.kwargs["output"] == {"error": "boom"}
    client.create_score.assert_not_called()
    assert summary["scores"] == 0
    assert summary["cases"] == 1


# ─── should_populate (gate decision) ─────────────────────────────────────────


def test_should_populate_blocks_when_client_disabled() -> None:
    allowed, reason = should_populate(None, gated=False)
    assert allowed is False
    assert "disabled" in reason


def test_should_populate_blocks_when_gated() -> None:
    allowed, reason = should_populate(MagicMock(), gated=True)
    assert allowed is False
    assert "GATED" in reason


def test_should_populate_allows_when_enabled_and_ungated() -> None:
    allowed, _ = should_populate(MagicMock(), gated=False)
    assert allowed is True


def test_population_is_gated_by_default() -> None:
    """Lock the gate: it must ship True so a stray --langfuse-sync cannot publish."""
    assert POPULATION_GATED is True


# ─── maybe_sync_langfuse (glue: client reuse + gate) ─────────────────────────


def test_maybe_sync_noop_when_not_requested() -> None:
    """--langfuse-sync off: returns immediately, never touches the tracing client."""
    with patch("rra.tracing.get_langfuse") as get_lf:
        status = maybe_sync_langfuse([], enabled=False)
    assert status["synced"] is False
    get_lf.assert_not_called()


def test_maybe_sync_reuses_shared_tracing_client() -> None:
    """When allowed, it syncs with the SHARED get_langfuse() client — no 2nd client."""
    shared_client = MagicMock(name="shared-langfuse-client")
    with (
        patch("rra.tracing.get_langfuse", return_value=shared_client) as get_lf,
        patch(
            "rra.evals.langfuse_eval.sync_eval_to_langfuse",
            return_value={"items": 1, "cases": 1, "scores": 1},
        ) as sync,
    ):
        status = maybe_sync_langfuse([], enabled=True, gated=False)

    get_lf.assert_called_once()
    sync.assert_called_once()
    assert sync.call_args.args[0] is shared_client  # the exact shared client
    assert status["synced"] is True


def test_maybe_sync_blocked_by_gate_even_with_live_client() -> None:
    """Default gate (POPULATION_GATED) blocks the sync even when a client exists."""
    with (
        patch("rra.tracing.get_langfuse", return_value=MagicMock()),
        patch("rra.evals.langfuse_eval.sync_eval_to_langfuse") as sync,
    ):
        status = maybe_sync_langfuse([], enabled=True)  # gated defaults to True

    assert status["synced"] is False
    assert "GATED" in status["reason"]
    sync.assert_not_called()


def test_maybe_sync_blocked_when_langfuse_disabled() -> None:
    with (
        patch("rra.tracing.get_langfuse", return_value=None),
        patch("rra.evals.langfuse_eval.sync_eval_to_langfuse") as sync,
    ):
        status = maybe_sync_langfuse([], enabled=True, gated=False)

    assert status["synced"] is False
    assert "disabled" in status["reason"]
    sync.assert_not_called()


def test_maybe_sync_failure_is_non_fatal() -> None:
    """A Langfuse outage during the populated sync must NOT raise — observability
    is auxiliary and must never turn a green eval red or mask the gate result."""
    with (
        patch("rra.tracing.get_langfuse", return_value=MagicMock()),
        patch(
            "rra.evals.langfuse_eval.sync_eval_to_langfuse",
            side_effect=RuntimeError("langfuse down"),
        ),
    ):
        status = maybe_sync_langfuse([], enabled=True, gated=False)  # does not raise

    assert status["synced"] is False
    assert "non-fatal" in status["reason"]
    assert "langfuse down" in status["reason"]
