"""Tests for the observability port and its adapters.

Covers:
  - get_observability() returns LangfuseObservabilityAdapter when langfuse_enabled.
  - get_observability() returns NoopObservabilityAdapter when langfuse disabled.
  - get_observability() raises NotImplementedError for aws/azure/gcp.
  - get_observability() is a process-lifetime singleton (lru_cache).
  - NoopObservabilityAdapter: start_span yields a handle; update() is a no-op;
    current_trace_id() returns None; flush() is a no-op; propagate_session
    returns a nullcontext.
  - LangfuseObservabilityAdapter delegates correctly to the Langfuse client
    (start_span, current_trace_id, flush, propagate_session) with correct kwargs.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ─── Factory: profile resolution ─────────────────────────────────────────────


def test_get_observability_returns_noop_when_langfuse_disabled() -> None:
    """get_observability() returns NoopObservabilityAdapter when langfuse keys absent."""
    from pydantic import SecretStr

    from rra.config import settings
    from rra.ports.observability import NoopObservabilityAdapter, get_observability

    get_observability.cache_clear()
    try:
        # langfuse_enabled is a computed property that reads langfuse_public_key and
        # langfuse_secret_key; nulling the keys is the correct way to make it False.
        with (
            patch.object(settings, "langfuse_public_key", None),
            patch.object(settings, "langfuse_secret_key", None),
        ):
            adapter = get_observability()
        assert isinstance(adapter, NoopObservabilityAdapter)
    finally:
        get_observability.cache_clear()


def test_get_observability_returns_langfuse_adapter_when_enabled() -> None:
    """get_observability() returns LangfuseObservabilityAdapter when langfuse_enabled."""
    from pydantic import SecretStr

    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter
    from rra.config import settings
    from rra.ports.observability import get_observability

    get_observability.cache_clear()
    try:
        with (
            patch.object(settings, "langfuse_public_key", SecretStr("pk-test")),
            patch.object(settings, "langfuse_secret_key", SecretStr("sk-test")),
            patch("rra.adapters.langfuse_observability.get_langfuse", return_value=MagicMock()),
        ):
            adapter = get_observability()
        assert isinstance(adapter, LangfuseObservabilityAdapter)
    finally:
        get_observability.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_observability_raises_not_implemented_for_cloud_profiles(
    profile: str,
) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.observability import get_observability

    get_observability.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_observability()
    finally:
        get_observability.cache_clear()


# ─── Singleton behaviour ──────────────────────────────────────────────────────


def test_get_observability_is_singleton() -> None:
    """Two successive get_observability() calls return the same object."""
    from rra.config import settings
    from rra.ports.observability import get_observability

    get_observability.cache_clear()
    try:
        with (
            patch.object(settings, "langfuse_public_key", None),
            patch.object(settings, "langfuse_secret_key", None),
        ):
            first = get_observability()
            second = get_observability()
        assert first is second
    finally:
        get_observability.cache_clear()


# ─── NoopObservabilityAdapter ────────────────────────────────────────────────


def test_noop_adapter_start_span_yields_handle() -> None:
    """NoopObservabilityAdapter.start_span() is a context manager that yields a handle."""
    from rra.ports.observability import NoopObservabilityAdapter

    adapter = NoopObservabilityAdapter()
    with adapter.start_span("test_span", as_type="span") as span:
        assert span is not None
        # update() must not raise
        span.update(output={"key": "value"})


def test_noop_adapter_nested_span_yields_handle() -> None:
    """Nested start_as_current_observation on noop span handle also yields a handle."""
    from rra.ports.observability import NoopObservabilityAdapter

    adapter = NoopObservabilityAdapter()
    with adapter.start_span("outer") as outer:
        with outer.start_as_current_observation(name="inner", as_type="generation") as inner:
            assert inner is not None
            inner.update(usage_details={"input": 10, "output": 5})


def test_noop_adapter_current_trace_id_returns_none() -> None:
    """NoopObservabilityAdapter.current_trace_id() returns None."""
    from rra.ports.observability import NoopObservabilityAdapter

    adapter = NoopObservabilityAdapter()
    assert adapter.current_trace_id() is None


def test_noop_adapter_flush_is_no_op() -> None:
    """NoopObservabilityAdapter.flush() does not raise."""
    from rra.ports.observability import NoopObservabilityAdapter

    adapter = NoopObservabilityAdapter()
    adapter.flush()  # must not raise


def test_noop_adapter_propagate_session_is_nullcontext() -> None:
    """NoopObservabilityAdapter.propagate_session() returns a usable context manager."""
    from rra.ports.observability import NoopObservabilityAdapter

    adapter = NoopObservabilityAdapter()
    with adapter.propagate_session("session-abc"):
        pass  # must not raise


# ─── LangfuseObservabilityAdapter ────────────────────────────────────────────


def _make_langfuse_mock() -> MagicMock:
    """Build a mock Langfuse client that simulates start_as_current_observation."""
    lf = MagicMock()
    # start_as_current_observation must behave as a context manager
    span_mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span_mock)
    cm.__exit__ = MagicMock(return_value=False)
    lf.start_as_current_observation.return_value = cm
    lf.get_current_trace_id.return_value = "trace-xyz"
    return lf


def test_langfuse_adapter_start_span_delegates() -> None:
    """LangfuseObservabilityAdapter.start_span() calls start_as_current_observation."""
    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter

    lf_mock = _make_langfuse_mock()
    with patch("rra.adapters.langfuse_observability.get_langfuse", return_value=lf_mock):
        adapter = LangfuseObservabilityAdapter()
        with adapter.start_span(
            "query",
            as_type="span",
            input={"q": "test"},
            metadata={"session_id": "s1"},
        ):
            pass

    lf_mock.start_as_current_observation.assert_called_once_with(
        name="query",
        as_type="span",
        input={"q": "test"},
        metadata={"session_id": "s1"},
    )


def test_langfuse_adapter_start_span_omits_none_kwargs() -> None:
    """Kwargs that are None are NOT forwarded to avoid polluting traces."""
    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter

    lf_mock = _make_langfuse_mock()
    with patch("rra.adapters.langfuse_observability.get_langfuse", return_value=lf_mock):
        adapter = LangfuseObservabilityAdapter()
        with adapter.start_span("span_name"):
            pass

    call_kwargs = lf_mock.start_as_current_observation.call_args.kwargs
    # Only name is set; no as_type / input / etc. since all were None
    assert call_kwargs == {"name": "span_name"}


def test_langfuse_adapter_current_trace_id_delegates() -> None:
    """LangfuseObservabilityAdapter.current_trace_id() delegates to get_current_trace_id."""
    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter

    lf_mock = _make_langfuse_mock()
    with patch("rra.adapters.langfuse_observability.get_langfuse", return_value=lf_mock):
        adapter = LangfuseObservabilityAdapter()
        tid = adapter.current_trace_id()

    assert tid == "trace-xyz"
    lf_mock.get_current_trace_id.assert_called_once()


def test_langfuse_adapter_flush_delegates() -> None:
    """LangfuseObservabilityAdapter.flush() calls lf.flush()."""
    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter

    lf_mock = _make_langfuse_mock()
    with patch("rra.adapters.langfuse_observability.get_langfuse", return_value=lf_mock):
        adapter = LangfuseObservabilityAdapter()
        adapter.flush()

    lf_mock.flush.assert_called_once()


def test_langfuse_adapter_propagate_session_delegates() -> None:
    """LangfuseObservabilityAdapter.propagate_session() calls propagate_attributes."""
    from rra.adapters.langfuse_observability import LangfuseObservabilityAdapter

    lf_mock = _make_langfuse_mock()
    fake_cm = MagicMock()
    fake_cm.__enter__ = MagicMock(return_value=None)
    fake_cm.__exit__ = MagicMock(return_value=False)

    mock_pa = MagicMock(return_value=fake_cm)

    # propagate_attributes is imported lazily from langfuse inside propagate_session().
    # Patch it at the langfuse module level via sys.modules so the lazy import sees it.
    import sys

    fake_langfuse_mod = MagicMock()
    fake_langfuse_mod.propagate_attributes = mock_pa

    with (
        patch("rra.adapters.langfuse_observability.get_langfuse", return_value=lf_mock),
        patch.dict(sys.modules, {"langfuse": fake_langfuse_mod}),
    ):
        adapter = LangfuseObservabilityAdapter()
        with adapter.propagate_session("session-abc"):
            pass

    mock_pa.assert_called_once_with(session_id="session-abc")
