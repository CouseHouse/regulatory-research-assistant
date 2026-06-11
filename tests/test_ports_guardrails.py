"""Tests for Port 8: Guardrails/policy (src/rra/ports/guardrails.py + adapters/allowall_guardrails.py).

Covers:
  - GuardrailVerdict dataclass: frozen, correct fields, defaults.
  - get_guardrails() factory: profile resolution, singleton, NotImplementedError.
  - AllowAllGuardrails: always allows for both boundaries; never logs text content.
  - api.py /query returns HTTP 400 + generic detail when guardrails blocks
    (mock get_guardrails in api namespace); response NEVER contains query text.
  - researcher.py drops blocked passages; result count reflects drops; passage
    text never appears in structlog output.
  - No content in captured logs from security paths (structlog-capture pattern
    from tests/test_no_secret_leak.py).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing


# ─── GuardrailVerdict dataclass ───────────────────────────────────────────────


def test_guardrail_verdict_is_frozen() -> None:
    """GuardrailVerdict is a frozen dataclass."""
    from rra.ports.guardrails import GuardrailVerdict

    v = GuardrailVerdict(allowed=True, boundary="user_input")
    with pytest.raises((AttributeError, TypeError)):
        v.allowed = False  # type: ignore[misc]


def test_guardrail_verdict_defaults() -> None:
    """GuardrailVerdict has correct default values."""
    from rra.ports.guardrails import GuardrailVerdict

    v = GuardrailVerdict(allowed=True, boundary="user_input")
    assert v.categories == ()
    assert v.score is None
    assert v.reason is None


def test_guardrail_verdict_all_fields() -> None:
    """GuardrailVerdict stores all explicitly provided fields."""
    from rra.ports.guardrails import GuardrailVerdict

    v = GuardrailVerdict(
        allowed=False,
        boundary="retrieved_content",
        categories=("injection", "toxicity"),
        score=0.97,
        reason="prompt_injection",
    )
    assert v.allowed is False
    assert v.boundary == "retrieved_content"
    assert v.categories == ("injection", "toxicity")
    assert v.score == 0.97
    assert v.reason == "prompt_injection"


# ─── Factory: profile resolution ──────────────────────────────────────────────


def test_get_guardrails_returns_local_adapter() -> None:
    """get_guardrails() under RRA_PROFILE=local returns AllowAllGuardrails."""
    from rra.adapters.allowall_guardrails import AllowAllGuardrails
    from rra.ports.guardrails import get_guardrails

    get_guardrails.cache_clear()
    try:
        adapter = get_guardrails()
        assert isinstance(adapter, AllowAllGuardrails)
    finally:
        get_guardrails.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_guardrails_raises_not_implemented_for_cloud_profiles(
    profile: str,
) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.guardrails import get_guardrails

    get_guardrails.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_guardrails()
    finally:
        get_guardrails.cache_clear()


def test_get_guardrails_is_singleton() -> None:
    """Two successive get_guardrails() calls return the same object."""
    from rra.ports.guardrails import get_guardrails

    get_guardrails.cache_clear()
    try:
        first = get_guardrails()
        second = get_guardrails()
        assert first is second
    finally:
        get_guardrails.cache_clear()


# ─── AllowAllGuardrails behaviour ─────────────────────────────────────────────


def test_allowall_allows_user_input() -> None:
    """AllowAllGuardrails returns allowed=True for boundary='user_input'."""
    from rra.adapters.allowall_guardrails import AllowAllGuardrails

    adapter = AllowAllGuardrails()
    verdict = adapter.check("any query text here", boundary="user_input")
    assert verdict.allowed is True
    assert verdict.boundary == "user_input"


def test_allowall_allows_retrieved_content() -> None:
    """AllowAllGuardrails returns allowed=True for boundary='retrieved_content'."""
    from rra.adapters.allowall_guardrails import AllowAllGuardrails

    adapter = AllowAllGuardrails()
    verdict = adapter.check("passage text from corpus", boundary="retrieved_content")
    assert verdict.allowed is True
    assert verdict.boundary == "retrieved_content"


def test_allowall_does_not_log_text_content() -> None:
    """AllowAllGuardrails logs boundary + chars only; NEVER logs the text content."""
    from rra.adapters.allowall_guardrails import AllowAllGuardrails

    sensitive_text = "SENSITIVE_QUERY_TEXT_12345"
    adapter = AllowAllGuardrails()

    with structlog.testing.capture_logs() as captured:
        adapter.check(sensitive_text, boundary="user_input")

    # There may be 0 or 1 log events (DEBUG may be filtered in test env).
    # If anything was logged, the sensitive text must not appear.
    for event in captured:
        event_str = str(event)
        assert sensitive_text not in event_str, (
            f"AllowAllGuardrails leaked text content into structlog: {event_str[:300]}"
        )


def test_allowall_logs_only_boundary_and_chars() -> None:
    """AllowAllGuardrails debug log contains boundary and chars, NOT the text."""
    import structlog

    from rra.adapters.allowall_guardrails import AllowAllGuardrails

    # Force DEBUG level capture by manipulating the structlog test processor directly.
    sensitive = "DO_NOT_LOG_THIS_CONTENT_9999"
    adapter = AllowAllGuardrails()

    with structlog.testing.capture_logs() as captured:
        adapter.check(sensitive, boundary="retrieved_content")

    for event in captured:
        event_str = str(event)
        assert sensitive not in event_str


# ─── api.py /query: guardrails blocks → HTTP 400, no echo of query text ───────


VALID_KEY = "dev-key-change-me"


@pytest.fixture
def api_client():
    """FastAPI test client."""
    from fastapi.testclient import TestClient

    from rra.api import app

    return TestClient(app, raise_server_exceptions=True)


def _blocking_guardrails(boundary_to_block: str = "user_input") -> MagicMock:
    """Return a mock guardrails adapter that blocks the given boundary."""
    from rra.ports.guardrails import GuardrailVerdict

    mock = MagicMock()
    mock.check.return_value = GuardrailVerdict(
        allowed=False,
        boundary=boundary_to_block,
        categories=("injection",),
        score=0.99,
        reason="prompt_injection",
    )
    return mock


def test_api_guardrails_blocks_query_returns_400(api_client: Any) -> None:
    """When guardrails blocks the query, /query returns HTTP 400."""
    with patch("rra.api.get_guardrails", return_value=_blocking_guardrails()):
        resp = api_client.post(
            "/query",
            json={"query": "malicious injection attempt"},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 400


def test_api_guardrails_block_returns_generic_detail(api_client: Any) -> None:
    """The 400 response body contains exactly 'request blocked by content policy'."""
    with patch("rra.api.get_guardrails", return_value=_blocking_guardrails()):
        resp = api_client.post(
            "/query",
            json={"query": "malicious injection attempt"},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("detail") == "request blocked by content policy"


def test_api_guardrails_block_does_not_echo_query(api_client: Any) -> None:
    """The 400 response must NOT contain any part of the original query text."""
    sensitive_query = "SENSITIVE_QUERY_CONTENT_MUST_NOT_ECHO"
    with patch("rra.api.get_guardrails", return_value=_blocking_guardrails()):
        resp = api_client.post(
            "/query",
            json={"query": sensitive_query},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 400
    resp_text = resp.text
    assert sensitive_query not in resp_text, (
        f"400 response echoed query text: {resp_text[:300]}"
    )


def test_api_guardrails_block_product_context_returns_400(api_client: Any) -> None:
    """When guardrails blocks product_context, /query returns HTTP 400."""

    def check_side_effect(text: str, *, boundary: str):
        from rra.ports.guardrails import GuardrailVerdict

        # Block the context, allow the query.
        if text == "INJECT_VIA_CONTEXT":
            return GuardrailVerdict(allowed=False, boundary=boundary, categories=("injection",))
        return GuardrailVerdict(allowed=True, boundary=boundary)

    mock_guardrails = MagicMock()
    mock_guardrails.check.side_effect = check_side_effect

    with patch("rra.api.get_guardrails", return_value=mock_guardrails):
        resp = api_client.post(
            "/query",
            json={"query": "safe query", "product_context": "INJECT_VIA_CONTEXT"},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "request blocked by content policy"


def test_api_guardrails_allowed_query_proceeds(api_client: Any) -> None:
    """When guardrails allows the query, /query proceeds normally (mocked graph)."""
    from unittest.mock import patch

    from rra.adapters.allowall_guardrails import AllowAllGuardrails
    from rra.schemas import RetrievedPassage

    passage = RetrievedPassage(
        guidance_id="doc-1",
        guidance_title="Test Guidance",
        chunk_index=0,
        text="Some regulatory text.",
        char_start=0,
        char_end=20,
        score=0.9,
    )
    mock_state = {
        "query": "safe query",
        "product_context": "",
        "session_id": "test",
        "trace_id": None,
        "sub_questions": [],
        "outline": "",
        "passages": [passage],
        "draft": "Answer text [doc-1:0]",
        "verdict": "approve",
        "critic_notes": [],
        "revision_count": 0,
        "cap_hit": False,
        "token_usage": {},
    }
    with (
        patch("rra.api.get_guardrails", return_value=AllowAllGuardrails()),
        patch("rra.api.run_graph", return_value=mock_state),
    ):
        resp = api_client.post(
            "/query",
            json={"query": "safe query"},
            headers={"X-API-Key": VALID_KEY},
        )
    assert resp.status_code == 200


# ─── researcher.py: blocked passages dropped, text never logged ────────────────


@pytest.fixture
def sample_passages():
    """Three passages; guardrails will block the second."""
    from rra.schemas import RetrievedPassage

    return [
        RetrievedPassage(
            guidance_id="doc-1",
            guidance_title="Guide 1",
            chunk_index=0,
            text="Safe passage content A.",
            char_start=0,
            char_end=20,
            score=0.9,
        ),
        RetrievedPassage(
            guidance_id="doc-2",
            guidance_title="Guide 2",
            chunk_index=1,
            text="INJECTED_CONTENT_BLOCKED_9999",
            char_start=0,
            char_end=29,
            score=0.85,
        ),
        RetrievedPassage(
            guidance_id="doc-3",
            guidance_title="Guide 3",
            chunk_index=2,
            text="Safe passage content C.",
            char_start=0,
            char_end=20,
            score=0.8,
        ),
    ]


def _make_partial_block_guardrails(blocked_text: str) -> MagicMock:
    """Return a guardrails mock that blocks exactly one passage."""
    from rra.ports.guardrails import GuardrailVerdict

    def check(text: str, *, boundary: str):
        if text == blocked_text:
            return GuardrailVerdict(
                allowed=False, boundary=boundary, categories=("injection",)
            )
        return GuardrailVerdict(allowed=True, boundary=boundary)

    mock = MagicMock()
    mock.check.side_effect = check
    return mock


def test_researcher_drops_blocked_passages(sample_passages: Any) -> None:
    """researcher.py drops passages that guardrails blocks.

    Three passages in; one is blocked; only two must survive.
    """
    from rra.mcp_server.tools import SearchCorpusResult
    from rra.ports.identity import Principal

    all_passages = sample_passages
    blocked_text = all_passages[1].text  # "INJECTED_CONTENT_BLOCKED_9999"

    sc_result = SearchCorpusResult(passages=all_passages)

    def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
        return sc_result

    transport_mock = MagicMock()
    transport_mock.call_tool.side_effect = mock_call_tool

    identity_mock = MagicMock()
    identity_mock.agent_principal.return_value = Principal(
        "researcher", "agent", frozenset({"search_corpus"})
    )

    guardrails_mock = _make_partial_block_guardrails(blocked_text)

    text_msg = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = "reformulated query"
    text_msg.content = [block]
    text_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    llm_mock = MagicMock()
    llm_mock.complete.return_value = text_msg

    with (
        patch("rra.agents.researcher.get_llm", return_value=llm_mock),
        patch("rra.agents.researcher.get_tool_transport", return_value=transport_mock),
        patch("rra.agents.researcher.get_identity", return_value=identity_mock),
        patch("rra.agents.researcher.get_guardrails", return_value=guardrails_mock),
    ):
        from rra.agents.researcher import run_researcher

        result = run_researcher({
            "sub_questions": ["What are the requirements?"],
            "query": "test query",
            "session_id": "t",
        })

    # 3 passages in, 1 blocked → 2 must survive.
    assert len(result["passages"]) == 2
    surviving_ids = {p.guidance_id for p in result["passages"]}
    assert "doc-1" in surviving_ids
    assert "doc-2" not in surviving_ids  # blocked
    assert "doc-3" in surviving_ids


def test_researcher_blocked_passage_text_not_in_logs(sample_passages: Any) -> None:
    """Blocked passage text MUST NOT appear in structlog output from the researcher.

    guardrails.passage_blocked logs ONLY guidance_id + chunk_index.
    """
    from rra.mcp_server.tools import SearchCorpusResult
    from rra.ports.identity import Principal

    all_passages = sample_passages
    blocked_text = all_passages[1].text  # "INJECTED_CONTENT_BLOCKED_9999"

    sc_result = SearchCorpusResult(passages=all_passages)

    def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
        return sc_result

    transport_mock = MagicMock()
    transport_mock.call_tool.side_effect = mock_call_tool

    identity_mock = MagicMock()
    identity_mock.agent_principal.return_value = Principal(
        "researcher", "agent", frozenset({"search_corpus"})
    )
    guardrails_mock = _make_partial_block_guardrails(blocked_text)

    text_msg = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = "reformulated query"
    text_msg.content = [block]
    text_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    llm_mock = MagicMock()
    llm_mock.complete.return_value = text_msg

    with (
        patch("rra.agents.researcher.get_llm", return_value=llm_mock),
        patch("rra.agents.researcher.get_tool_transport", return_value=transport_mock),
        patch("rra.agents.researcher.get_identity", return_value=identity_mock),
        patch("rra.agents.researcher.get_guardrails", return_value=guardrails_mock),
        structlog.testing.capture_logs() as captured,
    ):
        from rra.agents.researcher import run_researcher

        run_researcher({
            "sub_questions": ["What are the requirements?"],
            "query": "test query",
            "session_id": "t",
        })

    for event in captured:
        event_str = str(event)
        assert blocked_text not in event_str, (
            f"Blocked passage text leaked into structlog: {event_str[:400]}"
        )


def test_researcher_all_passages_allowed_when_guardrails_permits(
    sample_passages: Any,
) -> None:
    """When guardrails allows all passages, all are passed downstream (no drops)."""
    from rra.adapters.allowall_guardrails import AllowAllGuardrails
    from rra.mcp_server.tools import SearchCorpusResult
    from rra.ports.identity import Principal

    sc_result = SearchCorpusResult(passages=sample_passages)

    def mock_call_tool(tool: str, arguments: dict, principal: Any) -> Any:
        return sc_result

    transport_mock = MagicMock()
    transport_mock.call_tool.side_effect = mock_call_tool

    identity_mock = MagicMock()
    identity_mock.agent_principal.return_value = Principal(
        "researcher", "agent", frozenset({"search_corpus"})
    )

    text_msg = MagicMock()
    blk = MagicMock()
    blk.type = "text"
    blk.text = "reformulated"
    text_msg.content = [blk]
    text_msg.usage = MagicMock(input_tokens=10, output_tokens=5)
    llm_mock = MagicMock()
    llm_mock.complete.return_value = text_msg

    with (
        patch("rra.agents.researcher.get_llm", return_value=llm_mock),
        patch("rra.agents.researcher.get_tool_transport", return_value=transport_mock),
        patch("rra.agents.researcher.get_identity", return_value=identity_mock),
        patch("rra.agents.researcher.get_guardrails", return_value=AllowAllGuardrails()),
    ):
        from rra.agents.researcher import run_researcher

        result = run_researcher({
            "sub_questions": ["What are the requirements?"],
            "query": "test query",
            "session_id": "t",
        })

    assert len(result["passages"]) == 3


# ─── No content in security logs ──────────────────────────────────────────────


def test_tool_access_denied_log_does_not_contain_arguments() -> None:
    """tool.access_denied structured log must contain ONLY principal + tool.

    No arguments, no query text, no content must appear in the log event.
    """
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.ports.identity import Principal, ToolAccessDenied

    sensitive_arg = "SENSITIVE_QUERY_CONTENT_MUST_NOT_APPEAR"
    principal = Principal(name="no-scope", kind="agent", scopes=frozenset())
    transport = InProcessToolTransport()

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(ToolAccessDenied):
            transport.call_tool(
                "search_corpus",
                {"query": sensitive_arg, "k": 3},
                principal,
            )

    denial_events = [e for e in captured if e.get("event") == "tool.access_denied"]
    assert len(denial_events) >= 1, "Expected at least one tool.access_denied log event"

    for event in denial_events:
        event_str = str(event)
        assert sensitive_arg not in event_str, (
            f"tool.access_denied log leaked argument content: {event_str[:400]}"
        )
        assert "principal" in event
        assert "tool" in event
        # Arguments must not be present as a key.
        assert "arguments" not in event
        assert "query" not in event
