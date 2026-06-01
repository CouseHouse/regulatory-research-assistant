"""Tests for the LangGraph orchestrator (rra.graph).

Four scenarios:
  1. Happy path: critic approves on the first pass.
  2. Revision path: critic revises once, then approves.
  3. Cap-out path: critic always revises, hits cap=2, returns draft + warning.
  4. Escalate path: critic escalates on the first pass, exits immediately.

All LLM calls (planner, researcher, analyst, critic) are mocked so the tests
run without network access. The graph routing logic is what is under test.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver

from rra.schemas import RetrievedPassage


@pytest.fixture(autouse=True)
def reset_graph_cache() -> Any:
    """Reset the module-level _graph singleton so each test builds a fresh graph."""
    import rra.graph as g

    g._graph = None
    yield
    g._graph = None


# ─── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def sample_passage() -> RetrievedPassage:
    return RetrievedPassage(
        guidance_id="gd-001",
        guidance_title="510(k) Program Guidance",
        chunk_index=2,
        text="A new 510(k) is required when the modified device has a new intended use.",
        char_start=500,
        char_end=600,
        score=0.91,
    )


def _planner_output(sub_questions: list[str] | None = None) -> dict[str, Any]:
    return {
        "sub_questions": sub_questions or ["What triggers a new 510(k)?"],
        "outline": "1. Trigger criteria\n2. FDA guidance",
        "token_usage": {"planner_input": 100, "planner_output": 50},
    }


def _researcher_output(passages: list[RetrievedPassage]) -> dict[str, Any]:
    return {
        "passages": passages,
        "token_usage": {"researcher_input": 80, "researcher_output": 30},
    }


def _analyst_output(draft: str, suffix: str = "") -> dict[str, Any]:
    return {
        "draft": draft,
        "token_usage": {
            f"analyst_input{suffix}": 200,
            f"analyst_output{suffix}": 150,
        },
    }


def _critic_approve(revision_count: int = 0) -> dict[str, Any]:
    return {
        "verdict": "approve",
        "critic_notes": [],
        "revision_count": revision_count,
        "cap_hit": False,
        "token_usage": {f"critic_input": 180, f"critic_output": 60},
    }


def _critic_revise(revision_count: int, cap_hit: bool = False) -> dict[str, Any]:
    from rra.agents.types import CriticNote

    return {
        "verdict": "revise",
        "critic_notes": [
            CriticNote(
                citation_key="gd-001:2",
                issue="Citation does not directly support the claim.",
                severity="hard",
            )
        ],
        "revision_count": revision_count,
        "cap_hit": cap_hit,
        "token_usage": {f"critic_input_pass{revision_count}": 180, f"critic_output_pass{revision_count}": 60},
    }


def _critic_escalate() -> dict[str, Any]:
    return {
        "verdict": "escalate",
        "critic_notes": [],
        "revision_count": 0,
        "cap_hit": False,
        "token_usage": {"critic_input": 180, "critic_output": 60},
    }


INITIAL_STATE = {
    "query": "When does a device modification require a new 510(k)?",
    "product_context": "",
    "session_id": "test-session-abc",
    "trace_id": None,
}


# ─── Happy path ────────────────────────────────────────────────────────────────

def test_happy_path_approve_first_pass(sample_passage: RetrievedPassage) -> None:
    """Critic approves on the first pass → graph exits with verdict=approve."""
    draft = "A new 510(k) is required when the modified device has a new intended use. [gd-001:2]"

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", return_value=_analyst_output(draft)),
        patch("rra.graph.run_critic", return_value=_critic_approve()),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["verdict"] == "approve"
    assert result["cap_hit"] is False
    assert result["draft"] == draft
    assert len(result["passages"]) == 1
    assert result["revision_count"] == 0


# ─── Revision path ─────────────────────────────────────────────────────────────

def test_revision_path_revise_once_then_approve(sample_passage: RetrievedPassage) -> None:
    """Critic revises once then approves → analyst called twice, routing loops correctly."""
    first_draft = "A new 510(k) is required. [gd-001:2]"
    revised_draft = "A new 510(k) is required when the modified device has a new intended use. [gd-001:2]"

    # Analyst returns different drafts on first vs. second call.
    analyst_calls: list[int] = []

    def mock_analyst(state: dict[str, Any]) -> dict[str, Any]:
        call_n = len(analyst_calls)
        analyst_calls.append(call_n)
        if call_n == 0:
            return _analyst_output(first_draft)
        return _analyst_output(revised_draft, suffix="_rev1")

    critic_calls: list[int] = []

    def mock_critic(state: dict[str, Any]) -> dict[str, Any]:
        call_n = len(critic_calls)
        critic_calls.append(call_n)
        if call_n == 0:
            return _critic_revise(revision_count=1)
        return _critic_approve(revision_count=1)

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", side_effect=mock_analyst),
        patch("rra.graph.run_critic", side_effect=mock_critic),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["verdict"] == "approve"
    assert result["cap_hit"] is False
    assert result["draft"] == revised_draft
    assert result["revision_count"] == 1
    # Analyst was called twice (initial synthesis + one revision).
    assert len(analyst_calls) == 2
    # Critic was called twice (first pass → revise; second pass → approve).
    assert len(critic_calls) == 2


# ─── Cap-out path ──────────────────────────────────────────────────────────────

def test_cap_out_path_always_revise(sample_passage: RetrievedPassage) -> None:
    """Critic always revises; after cap=2 the graph exits with cap_hit=True."""
    draft = "Incomplete answer. [gd-001:2]"

    analyst_calls: list[int] = []

    def mock_analyst(state: dict[str, Any]) -> dict[str, Any]:
        analyst_calls.append(len(analyst_calls))
        return _analyst_output(draft)

    critic_call_n = [0]

    def mock_critic(state: dict[str, Any]) -> dict[str, Any]:
        n = critic_call_n[0]
        critic_call_n[0] += 1
        new_count = n + 1
        cap = new_count >= 2  # settings.max_critic_revisions default is 2
        return _critic_revise(revision_count=new_count, cap_hit=cap)

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", side_effect=mock_analyst),
        patch("rra.graph.run_critic", side_effect=mock_critic),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["cap_hit"] is True
    assert result["verdict"] == "revise"
    assert result["draft"] == draft  # draft is preserved on cap-out (ADR 0009)
    assert result["revision_count"] == 2


# ─── Escalate path ─────────────────────────────────────────────────────────────

def test_escalate_path_exits_immediately(sample_passage: RetrievedPassage) -> None:
    """Critic escalates on the first pass → immediate exit, analyst not called again."""
    refusal = "The corpus does not contain sufficient evidence to answer this question."

    analyst_calls: list[int] = []

    def mock_analyst(state: dict[str, Any]) -> dict[str, Any]:
        analyst_calls.append(0)
        return _analyst_output(refusal)

    critic_calls: list[int] = []

    def mock_critic(state: dict[str, Any]) -> dict[str, Any]:
        critic_calls.append(0)
        return _critic_escalate()

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", side_effect=mock_analyst),
        patch("rra.graph.run_critic", side_effect=mock_critic),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["verdict"] == "escalate"
    assert result["cap_hit"] is False
    assert result["draft"] == refusal
    # Analyst called exactly once; escalate exits immediately without revision.
    assert len(analyst_calls) == 1
    assert len(critic_calls) == 1


# ─── Force-verdict gate tests ──────────────────────────────────────────────────

def test_force_verdict_default_is_none() -> None:
    """critic_force_verdict must be None by default — gate is off in production."""
    from rra.config import settings

    assert settings.critic_force_verdict is None


def test_force_verdict_revise_hits_cap(
    monkeypatch: Any, sample_passage: RetrievedPassage
) -> None:
    """force_verdict='revise' drives the full revision loop until the cap fires.

    With max_critic_revisions=2 (default):
      - analyst[0]: revision_count=0 → critic forces revise, new_count=1, cap=False
      - analyst[1]: revision_count=1 → critic forces revise, new_count=2, cap=True → END
    Analyst is called exactly max_critic_revisions times (cap fires before a third call).
    """
    from rra.config import settings

    monkeypatch.setattr(settings, "critic_force_verdict", "revise")

    draft = "Draft answer. [gd-001:2]"
    analyst_calls: list[int] = []

    def mock_analyst(state: dict[str, Any]) -> dict[str, Any]:
        analyst_calls.append(state.get("revision_count", 0))
        return _analyst_output(draft)

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", side_effect=mock_analyst),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["cap_hit"] is True
    assert result["verdict"] == "revise"
    assert result["revision_count"] == settings.max_critic_revisions
    assert result["draft"] == draft
    # Cap fires after max_critic_revisions revise verdicts; analyst called once per verdict.
    assert len(analyst_calls) == settings.max_critic_revisions


def test_force_verdict_escalate_exits_immediately(
    monkeypatch: Any, sample_passage: RetrievedPassage
) -> None:
    """force_verdict='escalate' exits after a single analyst+critic pass."""
    from rra.config import settings

    monkeypatch.setattr(settings, "critic_force_verdict", "escalate")

    analyst_calls: list[int] = []

    def mock_analyst(state: dict[str, Any]) -> dict[str, Any]:
        analyst_calls.append(0)
        return _analyst_output("Refusal text.")

    with (
        patch("rra.graph.run_planner", return_value=_planner_output()),
        patch("rra.graph.run_researcher", return_value=_researcher_output([sample_passage])),
        patch("rra.graph.run_analyst", side_effect=mock_analyst),
        patch("rra.graph._get_checkpointer", return_value=MemorySaver()),
    ):
        from rra.graph import run_graph

        result = run_graph(INITIAL_STATE)

    assert result["verdict"] == "escalate"
    assert result["cap_hit"] is False
    assert result["revision_count"] == 0
    # Escalate exits immediately — analyst is called exactly once.
    assert len(analyst_calls) == 1
