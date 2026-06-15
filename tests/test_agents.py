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


def _make_llm_mock(msg: MagicMock) -> MagicMock:
    """Return a mock LLMPort whose .complete() returns *msg*.

    After the ports refactor agents call get_llm().complete(**kwargs) rather
    than Anthropic(api_key=...).messages.create(**kwargs).  Tests patch
    rra.ports.llm.get_llm to return this mock so the call-path is:
      run_<agent>()  →  get_llm()  →  llm_mock.complete(**kwargs)  →  msg
    """
    llm_mock = MagicMock()
    llm_mock.complete.return_value = msg
    return llm_mock


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
        with patch("rra.agents.planner.get_llm", return_value=_make_llm_mock(tool_resp)):
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
        with patch("rra.agents.planner.get_llm", return_value=_make_llm_mock(tool_resp)):
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
        with patch("rra.agents.planner.get_llm", return_value=_make_llm_mock(tool_resp)):
            from rra.agents.planner import run_planner

            result = run_planner({"query": "q", "product_context": "", "session_id": "t"})

        assert len(result["sub_questions"]) == 4


# ─── Researcher ────────────────────────────────────────────────────────────────

def _make_transport_mock(call_tool_fn: Any) -> MagicMock:
    """Return a mock ToolTransportPort whose .call_tool() uses call_tool_fn.

    After the ports refactor, researcher.py calls get_tool_transport().call_tool(...)
    rather than _tool_search_corpus directly.  Tests patch rra.agents.researcher.get_tool_transport
    to return this mock so the call-path is:
      run_researcher()  →  get_tool_transport()  →  transport_mock.call_tool(...)

    Note: call_tool now has signature (tool, arguments, principal).  Test
    helpers that use side_effect must accept 3 positional arguments.
    """
    transport_mock = MagicMock()
    transport_mock.call_tool.side_effect = call_tool_fn
    return transport_mock


class TestResearcherAgent:
    def test_reformulates_and_retrieves(self, sample_passage: RetrievedPassage) -> None:
        """Haiku reformulates each sub-question; search_corpus returns passages."""
        from rra.mcp_server.tools import SearchCorpusResult

        text_msg = _make_text_message("substantial equivalence 510(k) premarket notification")
        sc_result = SearchCorpusResult(passages=[sample_passage])

        def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
            assert tool == "search_corpus"
            return sc_result

        with (
            patch("rra.agents.researcher.get_llm", return_value=_make_llm_mock(text_msg)),
            patch("rra.agents.researcher.get_tool_transport", return_value=_make_transport_mock(mock_call_tool)),
            patch("rra.agents.researcher.get_identity"),
            patch("rra.agents.researcher.get_guardrails"),
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
        from rra.mcp_server.tools import SearchCorpusResult

        high_score = RetrievedPassage(**{**sample_passage.model_dump(), "score": 0.95})
        low_score = RetrievedPassage(**{**sample_passage.model_dump(), "score": 0.70})

        call_count = [0]

        def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
            n = call_count[0]
            call_count[0] += 1
            passages = [high_score] if n == 0 else [low_score]
            return SearchCorpusResult(passages=passages)

        text_msg = _make_text_message("reformulated query")

        with (
            patch("rra.agents.researcher.get_llm", return_value=_make_llm_mock(text_msg)),
            patch("rra.agents.researcher.get_tool_transport", return_value=_make_transport_mock(mock_call_tool)),
            patch("rra.agents.researcher.get_identity"),
            patch("rra.agents.researcher.get_guardrails"),
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
        from rra.mcp_server.tools import SearchCorpusResult

        p_low = RetrievedPassage(**{**sample_passage.model_dump(), "chunk_index": 10, "score": 0.5})
        p_high = RetrievedPassage(**{**sample_passage.model_dump(), "chunk_index": 11, "score": 0.9})

        call_count = [0]

        def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
            n = call_count[0]
            call_count[0] += 1
            passages = [p_low] if n == 0 else [p_high]
            return SearchCorpusResult(passages=passages)

        text_msg = _make_text_message("reformulated")

        with (
            patch("rra.agents.researcher.get_llm", return_value=_make_llm_mock(text_msg)),
            patch("rra.agents.researcher.get_tool_transport", return_value=_make_transport_mock(mock_call_tool)),
            patch("rra.agents.researcher.get_identity"),
            patch("rra.agents.researcher.get_guardrails"),
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

        with patch("rra.agents.analyst.get_llm", return_value=_make_llm_mock(msg)):
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

        with patch("rra.agents.analyst.get_llm", return_value=_make_llm_mock(msg)):
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

        with patch("rra.agents.analyst.get_llm", return_value=_make_llm_mock(msg)):
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

        with patch("rra.agents.critic.get_llm", return_value=_make_llm_mock(tool_resp)):
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

        with patch("rra.agents.critic.get_llm", return_value=_make_llm_mock(tool_resp)):
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

        with patch("rra.agents.critic.get_llm", return_value=_make_llm_mock(tool_resp)):
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

        with patch("rra.agents.critic.get_llm", return_value=_make_llm_mock(tool_resp)):
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

    def test_malformed_output_falls_back_to_escalate(
        self, sample_passage: RetrievedPassage
    ) -> None:
        """No tool-use block in response → RT-2 fix: escalate to human review, NOT approve.

        The old default-approve was a fail-open path: an injection that merely
        disrupts the critic's tool call yields automatic approval of a fabricated
        draft.  'escalate' exits the graph immediately (ADR 0009) and is a safe,
        non-looping fallback (RT-2 fix, docs/refactor/RT-redteam.md).
        """
        text_msg = _make_text_message("I could not parse this.")

        with patch("rra.agents.critic.get_llm", return_value=_make_llm_mock(text_msg)):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": "draft [gd-100:5]",
                "passages": [sample_passage],
                "query": "q",
                "revision_count": 0,
                "session_id": "t",
            })

        # RT-2: malformed output → escalate (fail closed), NOT approve (fail open).
        assert result["verdict"] == "escalate"
        # escalate does not increment revision_count (same as normal escalate path).
        assert result["revision_count"] == 0
        assert result["cap_hit"] is False


# ─── Critic flip: quote-faithfulness wiring (Day 8, ADR 0013) ──────────────────

class TestCriticFlipFaithfulness:
    """The critic-flip wires the analyst's <q> supporting quote into check_citation
    and surfaces address-vs-quote outcomes in <citation_checks>. These tests pin the
    deterministic pre-validation contract; the verdict itself is the LLM's job and is
    mocked. check_citation is mocked too, so there is no Postgres dependency.
    """

    @staticmethod
    def _run(
        draft: str,
        cc_return: Any = None,
        cc_side_effect: Any = None,
    ) -> tuple[dict[str, Any], str, MagicMock]:
        """Run run_critic with check_citation + LLM port mocked.

        Returns (result, citation_checks_xml, check_citation_mock). The second
        element is ONLY the <citation_checks>…</citation_checks> block, sliced out
        of the user message — the standing instruction paragraph also names every
        reason code (quote_unfaithful, address_not_found), so asserting against the
        whole message would false-positive on the guidance text.
        """
        approve = _make_tool_message("submit_verdict", {"verdict": "approve", "notes": []})
        llm_mock = _make_llm_mock(approve)

        cc_mock = MagicMock()
        if cc_side_effect is not None:
            cc_mock.side_effect = cc_side_effect
        else:
            cc_mock.return_value = cc_return

        # The critic now reaches check_citation through the tool-transport
        # chokepoint (ADR 0021). The transport mock unpacks the MCP-shaped
        # arguments dict into kwargs so the existing cc_mock assertions
        # (call_count, call_args.kwargs) keep their full strength.
        transport_mock = MagicMock()
        transport_mock.call_tool.side_effect = (
            lambda tool, arguments, principal: cc_mock(**arguments)
        )

        with (
            patch("rra.agents.critic.get_llm", return_value=llm_mock),
            patch("rra.agents.critic.get_tool_transport", return_value=transport_mock),
        ):
            from rra.agents.critic import run_critic

            result = run_critic({
                "draft": draft,
                "passages": [],
                "query": "q",
                "revision_count": 0,
                "session_id": "t",
            })

        user_content = llm_mock.complete.call_args.kwargs[
            "messages"
        ][0]["content"]
        start = user_content.index("<citation_checks>")
        end = user_content.index("</citation_checks>") + len("</citation_checks>")
        checks_xml = user_content[start:end]
        return result, checks_xml, cc_mock

    def test_faithful_quote_produces_clean_quote_check(self) -> None:
        """A faithful <q> quote → check_citation receives the quote; the XML marks
        it verified='true' check='quote' with no spurious quote_unfaithful signal."""
        from rra.mcp_server.tools import CitationCheckResult

        _, xml, cc = self._run(
            draft="The device must show substantial equivalence. "
            "[gd-100:5]<q>substantial equivalence</q>",
            cc_return=CitationCheckResult(
                verified=True,
                source_text="...demonstrate substantial equivalence to a predicate...",
                matched_doc_span=[10, 33],
                similarity_score=None,
            ),
        )
        # 3b: the parsed quote is passed through as quoted_text (not None).
        assert cc.call_count == 1
        assert cc.call_args.kwargs["quoted_text"] == "substantial equivalence"
        assert cc.call_args.kwargs["guidance_id"] == "gd-100"
        assert cc.call_args.kwargs["chunk_index"] == 5
        # 3c: clean faithful quote check — no faithfulness-failure signal.
        assert 'check="quote"' in xml
        assert 'verified="true"' in xml
        assert "quote_unfaithful" not in xml
        assert "<quoted_text>substantial equivalence</quoted_text>" in xml

    def test_unfaithful_quote_flagged_quote_unfaithful(self) -> None:
        """An unfaithful quote (chunk exists, quote not present) → the XML carries
        reason='quote_unfaithful' plus the similarity score, giving the critic the
        signal to revise. Distinct from an address failure."""
        from rra.mcp_server.tools import CitationCheckResult

        _, xml, cc = self._run(
            draft="Bogus claim. [gd-100:5]<q>this text is not in the chunk</q>",
            cc_return=CitationCheckResult(
                verified=False,
                source_text="real chunk text about predicates",
                matched_doc_span=None,
                similarity_score=0.42,
            ),
        )
        assert cc.call_args.kwargs["quoted_text"] == "this text is not in the chunk"
        assert 'reason="quote_unfaithful"' in xml
        assert 'check="quote"' in xml
        assert 'verified="false"' in xml
        assert "address_not_found" not in xml
        assert "0.42" in xml  # similarity surfaced to the critic

    def test_bare_citation_stays_key_existence(self) -> None:
        """A bare citation (no <q>) → check_citation called with quoted_text=None;
        the XML marks it verified='true' check='address' — NOT a faithfulness failure."""
        from rra.mcp_server.tools import CitationCheckResult

        _, xml, cc = self._run(
            draft="A grounded claim. [gd-100:5]",
            cc_return=CitationCheckResult(
                verified=True,
                source_text="real chunk text",
                matched_doc_span=None,
                similarity_score=None,
            ),
        )
        assert cc.call_args.kwargs["quoted_text"] is None
        assert 'check="address"' in xml
        assert 'verified="true"' in xml
        assert "quote_unfaithful" not in xml

    def test_address_not_found_distinct_from_quote_failure(self) -> None:
        """A quote on a missing chunk → address_not_found (source_text==''), never
        mislabeled quote_unfaithful. This is the conflation the flip must avoid."""
        from rra.mcp_server.tools import CitationCheckResult

        _, xml, _cc = self._run(
            draft="Claim. [gd-999:1]<q>whatever</q>",
            cc_return=CitationCheckResult(
                verified=False,
                source_text="",
                matched_doc_span=None,
                similarity_score=None,
            ),
        )
        assert 'reason="address_not_found"' in xml
        assert "quote_unfaithful" not in xml

    def test_same_address_different_quotes_both_checked(self) -> None:
        """De-dedup: the same [guid:idx] with two different quotes must produce two
        checks. The old (guid,idx)-only set collapsed them — the bug the flip fixes."""
        from rra.mcp_server.tools import CitationCheckResult

        ok = CitationCheckResult(
            verified=True, source_text="chunk", matched_doc_span=None, similarity_score=None
        )
        _, xml, cc = self._run(
            draft="First [gd-100:5]<q>quote one</q> and second [gd-100:5]<q>quote two</q>.",
            cc_side_effect=[ok, ok],
        )
        quotes_checked = [call.kwargs["quoted_text"] for call in cc.call_args_list]
        assert quotes_checked == ["quote one", "quote two"]
        assert xml.count('check="quote"') == 2

    def test_exact_duplicate_triples_collapse(self) -> None:
        """The same (guid, idx, quote) repeated → one check (one DB read, one
        <check>). Only DISTINCT quotes are preserved; exact dups still collapse."""
        from rra.mcp_server.tools import CitationCheckResult

        ok = CitationCheckResult(
            verified=True, source_text="chunk", matched_doc_span=None, similarity_score=None
        )
        _, xml, cc = self._run(
            draft="Here [gd-100:5]<q>same span</q> and again [gd-100:5]<q>same span</q>.",
            cc_return=ok,
        )
        assert cc.call_count == 1
        assert xml.count('check="quote"') == 1


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
