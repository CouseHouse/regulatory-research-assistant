"""Tests for the tool transport port and its local adapter.

Covers:
  - get_tool_transport() returns the local adapter under RRA_PROFILE=local.
  - get_tool_transport() raises NotImplementedError for aws/azure/gcp.
  - InProcessToolTransport.call_tool() dispatches to the correct tool function.
  - Unknown tool name raises ToolError with code="UNKNOWN".
  - get_tool_transport() is a process-lifetime singleton (lru_cache).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ─── Factory: profile resolution ─────────────────────────────────────────────


def test_get_tool_transport_returns_local_adapter() -> None:
    """get_tool_transport() under RRA_PROFILE=local returns an InProcessToolTransport."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.ports.tools import get_tool_transport

    get_tool_transport.cache_clear()
    try:
        adapter = get_tool_transport()
        assert isinstance(adapter, InProcessToolTransport)
    finally:
        get_tool_transport.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_tool_transport_raises_not_implemented_for_cloud_profiles(
    profile: str,
) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.tools import get_tool_transport

    get_tool_transport.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_tool_transport()
    finally:
        get_tool_transport.cache_clear()


# ─── Singleton behaviour ──────────────────────────────────────────────────────


def test_get_tool_transport_is_singleton() -> None:
    """Two successive get_tool_transport() calls return the same object."""
    from rra.ports.tools import get_tool_transport

    get_tool_transport.cache_clear()
    try:
        first = get_tool_transport()
        second = get_tool_transport()
        assert first is second
    finally:
        get_tool_transport.cache_clear()


# ─── Dispatch correctness ─────────────────────────────────────────────────────


def test_call_tool_search_corpus_dispatches_correctly() -> None:
    """call_tool('search_corpus', {...}) calls the registered search_corpus function."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import SearchCorpusResult

    fake_result = SearchCorpusResult(passages=[])
    mock_fn = MagicMock(return_value=fake_result)

    transport = InProcessToolTransport()
    # Patch the registry entry directly to avoid the class-level binding issue.
    transport._REGISTRY = dict(transport._REGISTRY)  # copy so we don't mutate the class
    transport._REGISTRY["search_corpus"] = mock_fn

    result = transport.call_tool("search_corpus", {"query": "510(k)", "k": 3})

    assert result is fake_result
    mock_fn.assert_called_once_with(query="510(k)", k=3)


def test_call_tool_check_citation_dispatches_correctly() -> None:
    """call_tool('check_citation', {...}) calls the registered check_citation function."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import CitationCheckResult

    fake_result = CitationCheckResult(
        verified=True,
        source_text="chunk text",
        matched_doc_span=None,
        similarity_score=None,
    )
    mock_fn = MagicMock(return_value=fake_result)

    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["check_citation"] = mock_fn

    result = transport.call_tool(
        "check_citation",
        {"claim": "some claim", "guidance_id": "gd-1", "chunk_index": 0},
    )

    assert result is fake_result
    mock_fn.assert_called_once_with(
        claim="some claim", guidance_id="gd-1", chunk_index=0
    )


def test_call_tool_fetch_guidance_dispatches_correctly() -> None:
    """call_tool('fetch_guidance', {...}) calls the registered fetch_guidance function."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import FetchGuidanceResult

    fake_result = FetchGuidanceResult(
        guidance_id="gd-1", guidance_title="Title", text="body", chunk_count=1
    )
    mock_fn = MagicMock(return_value=fake_result)

    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["fetch_guidance"] = mock_fn

    result = transport.call_tool("fetch_guidance", {"guidance_id": "gd-1"})

    assert result is fake_result
    mock_fn.assert_called_once_with(guidance_id="gd-1")


def test_call_tool_list_recent_guidances_dispatches_correctly() -> None:
    """call_tool('list_recent_guidances', {...}) calls the registered function."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import ListRecentGuidancesResult

    fake_result = ListRecentGuidancesResult(guidances=[])
    mock_fn = MagicMock(return_value=fake_result)

    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["list_recent_guidances"] = mock_fn

    result = transport.call_tool("list_recent_guidances", {"since_date": "2026-01-01"})

    assert result is fake_result
    mock_fn.assert_called_once_with(since_date="2026-01-01")


# ─── Unknown tool error ───────────────────────────────────────────────────────


def test_call_tool_unknown_name_raises_tool_error() -> None:
    """call_tool with an unknown tool name raises ToolError(code='UNKNOWN', retryable=False)."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import ToolError

    transport = InProcessToolTransport()
    with pytest.raises(ToolError) as exc_info:
        transport.call_tool("nonexistent_tool", {})

    err = exc_info.value
    assert err.code == "UNKNOWN"
    assert err.retryable is False
    assert "nonexistent_tool" in err.message


# ─── ToolError propagation ────────────────────────────────────────────────────


def test_call_tool_propagates_tool_error_unchanged() -> None:
    """ToolError raised by the underlying tool function is NOT re-wrapped."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import ToolError

    original_err = ToolError(
        code="DB_ERROR", message="db is down", tool="check_citation", retryable=True
    )
    mock_fn = MagicMock(side_effect=original_err)

    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["check_citation"] = mock_fn

    with pytest.raises(ToolError) as exc_info:
        transport.call_tool("check_citation", {"claim": "c", "guidance_id": "g", "chunk_index": 0})

    assert exc_info.value is original_err  # same object, not a wrap
