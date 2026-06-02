"""Tests for individual agent nodes: planner, researcher, analyst, critic.

Each test mocks the Anthropic client to verify the agent's input/output
contract without making real API calls.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rra.schemas import RetrievedPassage


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_text_message(text: str, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    """Fake Anthropic message with a TextBlock response."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


def _make_tool_message(
    tool_name: str,
    tool_input: dict[str, Any],
    input_tokens: int = 150,
    output_tokens: int = 80,
) -> MagicMock:
    """Fake Anthropic message with a ToolUseBlock response."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return msg


def _make_anthropic_mock(msg: MagicMock) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = msg
    anthropic_cls = MagicMock(return_value=client)
    return anthropic_cls


@pytest.fixture
def sample_passage() -> RetrievedPassage:
    return RetrievedPassage(
        guidance_id="gd-100",
        guidance_title="Premarket Notification 510(k) Program",
        chunk_index=5,
        text="The 510(k) submission must demonstrate substantial equivalence to a predicate.",
        char_start=200,
        char_end=290,
        score=0.88,
    )


# ─── Planner ───────────────────────────────────────────────────────────────────

class TestPlannerAgent:
    def test_returns_sub_questions_and_outline(self) -> None:
        tool_resp = _make_tool_message(
            "plan_query",
            {
                "sub_questions": [
                    "What changes trigger a new 510(k)?",
                    "What is the substantial equivalence standard?",
                ],
                "outline": "1. Trigger criteria\n2. Substantial equivalence",
            },
        )
        with patch("rra.agents.planner.Anthropic", return_value=tool_resp.return_value if False else _make_anthropic_mock(tool_resp).return_value):
            with patch("rra.agents.planner.Anthropic", _make_anthropic_mock(tool_resp)):
                from rra.agents.planner import run_planner

                state: dict[str, Any] = {
                    "query": "When is a new 510(k) required?",
                    "product_context": "",
                    "session_id": "test",
                }
                result = run_planner(state)

        assert len(result["sub_questions"]) == 2
        assert "510(k)" in result["sub_questions"][0]
        assert result["outline"].startswith("1.")
        assert "planner_input" in result["token_usage"]
        assert "planner_output" in result["token_usage"]

    def test_falls_back_to_query_on_parse_error(self) -> None:
        """Malformed tool output → fallback to [query] as sole sub-question."""
        tool_resp = _make_tool_message("plan_query", {"bad_key": "garbage"})
        with patch("rra.agents.planner.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.planner import run_planner

            original_query = "What is the De Novo pathway?"
            state: dict[str, Any] = {
                "query": original_query,
                "product_context": "",
                "session_id": "test",
            }
            result = run_planner(state)

        # Fallback: the raw query becomes the single sub-question.
        assert result["sub_questions"] == [original_query]

    def test_truncates_sub_questions_to_four(self) -> None:
        tool_resp = _make_tool_message(
            "plan_query",
            {
                "sub_questions": ["q1", "q2", "q3", "q4", "q5"],
                "outline": "outline",
            },
        )
        with patch("rra.agents.planner.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.planner import run_planner

            result = run_planner({"query": "q", "product_context": "", "session_id": "t"})

        assert len(result["sub_questions"]) == 4


# ─── Researcher ────────────────────────────────────────────────────────────────

class TestResearcherAgent:
    def test_reformulates_and_retrieves(self, sample_passage: RetrievedPassage) -> None:
        """Haiku reformulates each sub-question; search_corpus returns passages."""
        from rra.mcp_server.tools import SearchCorpusResult

        text_msg = _make_text_message("substantial equivalence 510(k) premarket notification")

        with (
            patch("rra.agents.researcher.Anthropic", _make_anthropic_mock(text_msg)),
            patch(
                "rra.agents.researcher._tool_search_corpus",
                return_value=SearchCorpusResult(passages=[sample_passage]),
            ),
        ):
            from rra.agents.researcher import run_researcher

            state: dict[str, Any] = {
                "sub_questions": ["What is substantial equivalence?"],
                "query": "510(k) requirements",
                "session_id": "test",
            }
            result = run_researcher(state)

        assert len(result["passages"]) == 1
        assert result["passages"][0].guidance_id == "gd-100"
        assert "researcher_input" in result["token_usage"]

    def test_deduplicates_same_chunk_from_multiple_sub_questions(
        self, sample_passage: RetrievedPassage
    ) -> None:
        """If two sub-questions return the same chunk, keep the higher score copy."""
        high_score = RetrievedPassage(**{**sample_passage.model_dump(), "score": 0.95})
        low_score = RetrievedPassage(**{**sample_passage.model_dump(), "score": 0.70})

        call_count = [0]

        from rra.mcp_server.tools import SearchCorpusResult

        def mock_search(query: str, k: int | None = None) -> SearchCorpusResult:
            n = call_count[0]
            call_count[0] += 1
            return SearchCorpusResult(passages=[high_score] if n == 0 else [low_score])

        text_msg = _make_text_message("reformulated query")

        with (
            patch("rra.agents.researcher.Anthropic", _make_anthropic_mock(text_msg)),
            patch("rra.agents.researcher._tool_search_corpus", side_effect=mock_search),
        ):
            from rra.agents.researcher import run_researcher

            result = run_researcher({
                "sub_questions": ["q1", "q2"],
                "query": "test",
                "session_id": "t",
            })

        # One unique chunk, and the higher-score copy was kept.
        assert len(result["passages"]) == 1
        assert result["passages"][0].score == 0.95

    def test_empty_sub_questions_returns_empty_passages(self) -> None:
        from rra.agents.researcher import run_researcher

        result = run_researcher({"sub_questions": [], "query": "q", "session_id": "t"})
        assert result["passages"] == []

    def test_passages_sorted_by_score_descending(self, sample_passage: RetrievedPassage) -> None:
        p_low = RetrievedPassage(**{**sample_passage.model_dump(), "chunk_index": 10, "score": 0.5})
        p_high = RetrievedPassage(**{**sample_passage.model_dump(), "chunk_index": 11, "score": 0.9})

        call_count = [0]

        from rra.mcp_server.tools import SearchCorpusResult

        def mock_search(query: str, k: int | None = None) -> SearchCorpusResult:
            n = call_count[0]
            call_count[0] += 1
            return SearchCorpusResult(passages=[p_low] if n == 0 else [p_high])

        text_msg = _make_text_message("reformulated")

        with (
            patch("rra.agents.researcher.Anthropic", _make_anthropic_mock(text_msg)),
            patch("rra.agents.researcher._tool_search_corpus", side_effect=mock_search),
        ):
            from rra.agents.researcher import run_researcher

            result = run_researcher({"sub_questions": ["q1", "q2"], "query": "q", "session_id": "t"})

        scores = [p.score for p in result["passages"]]
        assert scores == sorted(scores, reverse=True)


# ─── Analyst ───────────────────────────────────────────────────────────────────

class TestAnalystAgent:
    def test_first_pass_synthesis(self, sample_passage: RetrievedPassage) -> None:
        draft = "The 510(k) requires substantial equivalence. [gd-100:5]"
        msg = _make_text_message(draft)

        with patch("rra.agents.analyst.Anthropic", _make_anthropic_mock(msg)):
            from rra.agents.analyst import run_analyst

            state: dict[str, Any] = {
                "query": "What is a 510(k)?",
                "product_context": "",
                "outline": "1. Definition\n2. Requirements",
                "passages": [sample_passage],
                "critic_notes": [],
                "revision_count": 0,
                "session_id": "t",
                "draft": "",
            }
            result = run_analyst(state)

        assert result["draft"] == draft
        assert "analyst_input" in result["token_usage"]

    def test_revision_pass_uses_critic_notes(self, sample_passage: RetrievedPassage) -> None:
        """revision_count > 0 triggers edit-in-place path."""
        from rra.agents.types import CriticNote

        revised_draft = "The 510(k) requires substantial equivalence to a predicate. [gd-100:5]"
        msg = _make_text_message(revised_draft)

        with patch("rra.agents.analyst.Anthropic", _make_anthropic_mock(msg)) as mock_cls:
            from rra.agents.analyst import run_analyst

            note = CriticNote(
                citation_key="gd-100:5",
                issue="Claim is too vague.",
                severity="hard",
            )
            state: dict[str, Any] = {
                "query": "What is a 510(k)?",
                "product_context": "",
                "outline": "1. Definition",
                "passages": [sample_passage],
                "critic_notes": [note],
                "revision_count": 1,
                "draft": "The 510(k) requires substantial equivalence. [gd-100:5]",
                "session_id": "t",
            }
            result = run_analyst(state)

        assert result["draft"] == revised_draft
        # Revision token key includes _rev1 suffix.
        assert "analyst_input_rev1" in result["token_usage"]

    def test_token_usage_contains_input_and_output(self, sample_passage: RetrievedPassage) -> None:
        msg = _make_text_message("draft text", input_tokens=300, output_tokens=120)

        with patch("rra.agents.analyst.Anthropic", _make_anthropic_mock(msg)):
            from rra.agents.analyst import run_analyst

            result = run_analyst({
                "query": "q",
                "product_context": "",
                "outline": "",
                "passages": [sample_passage],
                "critic_notes": [],
                "revision_count": 0,
                "draft": "",
                "session_id": "t",
            })

        assert result["token_usage"]["analyst_input"] == 300
        assert result["token_usage"]["analyst_output"] == 120


# ─── Critic ────────────────────────────────────────────────────────────────────

class TestCriticAgent:
    def test_approve_verdict(self, sample_passage: RetrievedPassage) -> None:
        tool_resp = _make_tool_message("submit_verdict", {"verdict": "approve", "notes": []})

        with patch("rra.agents.critic.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "The 510(k) requires substantial equivalence. [gd-100:5]",
                "passages": [sample_passage],
                "query": "What is a 510(k)?",
                "revision_count": 0,
                "session_id": "t",
            })

        assert result["verdict"] == "approve"
        assert result["critic_notes"] == []
        assert result["cap_hit"] is False
        assert result["revision_count"] == 0

    def test_revise_increments_revision_count(self, sample_passage: RetrievedPassage) -> None:
        tool_resp = _make_tool_message(
            "submit_verdict",
            {
                "verdict": "revise",
                "notes": [
                    {
                        "citation_key": "gd-100:5",
                        "issue": "Claim does not match cited passage.",
                        "severity": "hard",
                    }
                ],
            },
        )

        with patch("rra.agents.critic.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "Something wrong. [gd-100:5]",
                "passages": [sample_passage],
                "query": "q",
                "revision_count": 0,
                "session_id": "t",
            })

        assert result["verdict"] == "revise"
        assert result["revision_count"] == 1
        assert result["cap_hit"] is False
        assert len(result["critic_notes"]) == 1
        assert result["critic_notes"][0].severity == "hard"

    def test_cap_hit_when_revision_count_reaches_max(
        self, sample_passage: RetrievedPassage
    ) -> None:
        """When revision_count + 1 >= max_critic_revisions, cap_hit is True."""
        from rra.config import settings

        tool_resp = _make_tool_message(
            "submit_verdict",
            {"verdict": "revise", "notes": [{"citation_key": None, "issue": "gap", "severity": "soft"}]},
        )
        # Start at max_critic_revisions - 1 so the next increment hits the cap.
        starting_count = settings.max_critic_revisions - 1

        with patch("rra.agents.critic.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "answer [gd-100:5]",
                "passages": [sample_passage],
                "query": "q",
                "revision_count": starting_count,
                "session_id": "t",
            })

        assert result["cap_hit"] is True
        assert result["revision_count"] == settings.max_critic_revisions

    def test_escalate_verdict(self, sample_passage: RetrievedPassage) -> None:
        tool_resp = _make_tool_message("submit_verdict", {"verdict": "escalate", "notes": []})

        with patch("rra.agents.critic.Anthropic", _make_anthropic_mock(tool_resp)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "The corpus does not contain sufficient evidence.",
                "passages": [sample_passage],
                "query": "q",
                "revision_count": 0,
                "session_id": "t",
            })

        assert result["verdict"] == "escalate"
        assert result["cap_hit"] is False
        assert result["revision_count"] == 0  # escalate does not increment

    def test_malformed_output_falls_back_to_approve(
        self, sample_passage: RetrievedPassage
    ) -> None:
        """No tool-use block in response → fallback to approve to avoid infinite loop."""
        text_msg = _make_text_message("I could not parse this.")

        with patch("rra.agents.critic.Anthropic", _make_anthropic_mock(text_msg)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "draft [gd-100:5]",
                "passages": [sample_passage],
                "query": "q",
                "revision_count": 0,
                "session_id": "t",
            })

        assert result["verdict"] == "approve"


# ─── format_user_prompt (moved from api.py) ────────────────────────────────────

class TestFormatUserPrompt:
    def test_passage_xml_contains_required_attributes(
        self, sample_passage: RetrievedPassage
    ) -> None:
        from rra.agents.analyst import format_user_prompt
        from rra.schemas import QueryRequest

        req = QueryRequest(query="What is substantial equivalence?")
        prompt = format_user_prompt(req, [sample_passage])

        assert f'guidance_id="{sample_passage.guidance_id}"' in prompt
        assert f'chunk_index="{sample_passage.chunk_index}"' in prompt
        assert sample_passage.text in prompt
        assert req.query in prompt

    def test_product_context_included_when_provided(
        self, sample_passage: RetrievedPassage
    ) -> None:
        from rra.agents.analyst import format_user_prompt
        from rra.schemas import QueryRequest

        req = QueryRequest(
            query="What is substantial equivalence?",
            product_context="Class II IVD device",
        )
        prompt = format_user_prompt(req, [sample_passage])

        assert "Class II IVD device" in prompt
