"""Tests for the LLM port and its local adapter.

Covers:
  - get_llm() returns the local adapter under RRA_PROFILE=local (default).
  - get_llm() raises NotImplementedError for aws/azure/gcp.
  - AnthropicLLMAdapter.complete() forwards kwargs to the underlying client.
  - get_llm() is a process-lifetime singleton (two calls → same object).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from unittest.mock import MagicMock, patch

import pytest


# ─── Factory: profile resolution ───────────────────────────────────────────────

def test_get_llm_returns_local_adapter_under_local_profile() -> None:
    """get_llm() with RRA_PROFILE=local returns an AnthropicLLMAdapter."""
    from rra.adapters.anthropic_llm import AnthropicLLMAdapter
    from rra.ports.llm import get_llm

    get_llm.cache_clear()
    try:
        with patch("rra.adapters.anthropic_llm.Anthropic"):
            adapter = get_llm()
        assert isinstance(adapter, AnthropicLLMAdapter)
    finally:
        get_llm.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_llm_raises_not_implemented_for_cloud_profiles(profile: str) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.llm import get_llm

    get_llm.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_llm()
    finally:
        get_llm.cache_clear()


# ─── Singleton behaviour ────────────────────────────────────────────────────────

def test_get_llm_is_singleton() -> None:
    """Two successive get_llm() calls return the same object (lru_cache)."""
    from rra.ports.llm import get_llm

    get_llm.cache_clear()
    try:
        with patch("rra.adapters.anthropic_llm.Anthropic"):
            first = get_llm()
            second = get_llm()
        assert first is second
    finally:
        get_llm.cache_clear()


# ─── Adapter delegation ────────────────────────────────────────────────────────

def test_adapter_complete_delegates_kwargs_to_client() -> None:
    """AnthropicLLMAdapter.complete(**kwargs) forwards all kwargs unchanged."""
    from rra.adapters.anthropic_llm import AnthropicLLMAdapter

    mock_client = MagicMock()
    fake_message = MagicMock()
    mock_client.messages.create.return_value = fake_message

    adapter = AnthropicLLMAdapter.__new__(AnthropicLLMAdapter)
    adapter._client = mock_client

    result = adapter.complete(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert result is fake_message
    mock_client.messages.create.assert_called_once_with(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": "hello"}],
    )


def test_adapter_complete_passes_extra_kwargs() -> None:
    """complete() passes unknown kwargs unchanged (tools, tool_choice, system, …)."""
    from rra.adapters.anthropic_llm import AnthropicLLMAdapter

    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock()

    adapter = AnthropicLLMAdapter.__new__(AnthropicLLMAdapter)
    adapter._client = mock_client

    sentinel = object()
    adapter.complete(model="m", max_tokens=1, messages=[], tools=sentinel)

    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tools"] is sentinel
