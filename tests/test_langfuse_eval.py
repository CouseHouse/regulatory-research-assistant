"""Tests for rra.evals.langfuse_eval — the Langfuse dataset/score integration.

BUILD-ONLY verification: every test MOCKS the Langfuse client. No real Langfuse
API call is made and no eval is run, so nothing is populated (the integration is
gated until the critic-flip; see langfuse_eval module banner). These tests assert
the right dataset/score calls happen with the right args, that scores link to the
trace, that the SHARED tracing client is reused (no second client), and that the
population gate holds.
"""

from __future__ import annotations

import contextlib
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
from rra.evals.run import CaseRun, run_agent  # noqa: E402
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


def _response(raw_trace_id: str | None = None) -> AgentResponse:
    return AgentResponse(
        answer_text="an answer " * 50,  # >200 chars so the preview is exercised
        citations=[{"guidance_id": "GUID-1", "chunk_index": 0, "quoted_text": "q"}],
        retrieved_passages=[],
        raw_trace_id=raw_trace_id,
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


def test_emit_scores_uses_idempotent_score_id() -> None:
    """Each score carries score_id={trace_id}-{scorer} so a re-run UPDATES rather
    than duplicates (Langfuse 'Preventing Duplicate Scores')."""
    client = MagicMock()
    emit_scores(client, "t", [_numeric_score()])
    assert client.create_score.call_args.kwargs["score_id"] == "t-citation_validity"


# ─── sync_eval_to_langfuse (orchestration) ───────────────────────────────────


def test_sync_attaches_scores_to_runtime_trace() -> None:
    """Scores attach to each run's raw_trace_id (the run-time eval-case trace from
    run_agent); NO post-hoc span is opened (that would orphan the scores)."""
    client = MagicMock()
    runs = [
        CaseRun(_case(1), _response(raw_trace_id="trace-1"), [_numeric_score(), _na_score()]),
        CaseRun(_case(2), _response(raw_trace_id="trace-2"), [_numeric_score()]),
    ]

    summary = sync_eval_to_langfuse(client, runs)

    # Dataset pushed for both cases.
    client.create_dataset.assert_called_once()
    assert client.create_dataset_item.call_count == 2
    # NO post-hoc eval-case span — scores ride the run-time trace.
    client.start_as_current_observation.assert_not_called()
    # 3 scores total, each attached to its OWN run's trace.
    assert client.create_score.call_count == 3
    trace_ids = sorted(c.kwargs["trace_id"] for c in client.create_score.call_args_list)
    assert trace_ids == ["trace-1", "trace-1", "trace-2"]
    client.flush.assert_called_once()

    assert summary == {
        "dataset": GOLDEN_DATASET_NAME,
        "items": 2,
        "cases": 2,
        "scores": 3,
        "skipped_no_trace": 0,
    }


def test_sync_skips_case_without_runtime_trace() -> None:
    """A case with no run-time trace (errored → no response, or a response with
    raw_trace_id=None) is skipped: no scores, counted in skipped_no_trace."""
    client = MagicMock()
    runs = [
        CaseRun(_case(9), None, [], error="boom"),            # errored → no response
        CaseRun(_case(8), _response(raw_trace_id=None), []),  # response but no trace
    ]

    summary = sync_eval_to_langfuse(client, runs)

    client.create_score.assert_not_called()
    client.start_as_current_observation.assert_not_called()
    assert summary["scores"] == 0
    assert summary["skipped_no_trace"] == 2
    assert summary["cases"] == 2


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


# ─── --allow-population flag (the per-run gate opener) ───────────────────────
# These exercise the flag against the COMMITTED POPULATION_GATED default (no
# explicit gated= override), so they verify the flag ALONE opens the gate — and
# would fail if the wiring broke or someone flipped the constant.


def test_allow_population_off_is_noop_under_default_gate() -> None:
    """Flag off → gated no-op, even with keys present (the safe default)."""
    allowed, reason = should_populate(MagicMock(), allow_population=False)
    assert allowed is False
    assert "GATED" in reason


def test_allow_population_on_without_keys_is_noop() -> None:
    """Flag on but no keys (client is None) → still a no-op (disabled)."""
    allowed, reason = should_populate(None, allow_population=True)
    assert allowed is False
    assert "disabled" in reason


def test_allow_population_on_with_keys_opens_the_default_gate() -> None:
    """Flag on + keys present → population allowed, opening the committed gate."""
    allowed, _ = should_populate(MagicMock(), allow_population=True)
    assert allowed is True


def test_maybe_sync_flag_opens_gate_through_the_glue() -> None:
    """End-to-end: --allow-population forwarded through maybe_sync_langfuse opens
    the default gate and reuses the shared client (no gated= override needed)."""
    shared = MagicMock(name="shared-langfuse-client")
    with (
        patch("rra.tracing.get_langfuse", return_value=shared),
        patch(
            "rra.evals.langfuse_eval.sync_eval_to_langfuse",
            return_value={"items": 1, "cases": 1, "scores": 1},
        ) as sync,
    ):
        status = maybe_sync_langfuse([], enabled=True, allow_population=True)

    assert status["synced"] is True
    sync.assert_called_once()


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


# ─── run_agent eval-case parenting (Phase 3: orphan-span fix) ────────────────
# run_agent opens ONE per-case "eval-case" span and runs the graph INSIDE it, so
# the agents' own spans nest under a single trace instead of orphaning. These
# mock run_graph / parse_answer / get_langfuse so no graph, API, or Langfuse runs.


def test_run_agent_no_langfuse_when_flag_off() -> None:
    """trace_to_langfuse=False → get_langfuse is never called; raw_trace_id None."""
    with (
        patch("rra.tracing.get_langfuse") as get_lf,
        patch("rra.graph.run_graph", return_value={"draft": "d", "passages": []}) as rg,
        patch("rra.citations.parse_answer", return_value=("prose", [("GUID-1", 0, "q")])),
    ):
        resp = run_agent(_case(1), trace_to_langfuse=False)

    get_lf.assert_not_called()
    assert resp.raw_trace_id is None
    assert rg.call_args.args[0]["session_id"]  # a session_id was generated + passed


class _RecordingCM:
    """Real context manager: records enter order, yields a span mock. Avoids the
    MagicMock dunder-lookup gotcha (ExitStack resolves __enter__ on the type)."""

    def __init__(self, order: list[str], span: MagicMock) -> None:
        self._order, self._span = order, span

    def __enter__(self) -> MagicMock:
        self._order.append("span_enter")
        return self._span

    def __exit__(self, *exc: object) -> bool:
        return False


def test_run_agent_parents_graph_under_eval_case_span() -> None:
    """trace_to_langfuse=True + client: the eval-case span is ENTERED BEFORE the
    graph runs (so agent spans nest under it), the case identity is on the span,
    and the trace_id is captured onto raw_trace_id for Phase 4/5."""
    order: list[str] = []
    span = MagicMock()
    client = MagicMock()
    client.get_current_trace_id.return_value = "trace-xyz"
    client.start_as_current_observation.return_value = _RecordingCM(order, span)

    def fake_graph(state: dict) -> dict:
        order.append("run_graph")
        assert state["session_id"]  # graph runs with the propagated session
        return {"draft": "d", "passages": []}

    with (
        patch("rra.tracing.get_langfuse", return_value=client),
        patch("langfuse.propagate_attributes", return_value=contextlib.nullcontext()) as prop,
        patch("rra.graph.run_graph", side_effect=fake_graph),
        patch("rra.citations.parse_answer", return_value=("prose", [("GUID-1", 0, "q")])),
    ):
        resp = run_agent(_case(2), trace_to_langfuse=True)

    # Orphan-fix proof: the span opened BEFORE the graph executed.
    assert order == ["span_enter", "run_graph"]
    # Session propagated; case identity on the parent span.
    assert prop.call_args.kwargs["session_id"]
    obs = client.start_as_current_observation.call_args.kwargs
    assert obs["name"] == "eval-case"
    assert obs["metadata"]["case_id"] == "easy-002"
    # Captured trace_id rides out for the Phase-4 score writes / Phase-5 linkage.
    assert resp.raw_trace_id == "trace-xyz"
    span.update.assert_called_once()
