"""Tests for the memory/state port and its local adapter.

Covers:
  - get_state() returns the local adapter under RRA_PROFILE=local.
  - get_state() raises NotImplementedError for aws/azure/gcp.
  - PostgresStateAdapter.checkpointer() returns a PostgresSaver and sets it up.
  - get_state() is a process-lifetime singleton (lru_cache).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from unittest.mock import MagicMock, patch

import pytest


# ─── Factory: profile resolution ─────────────────────────────────────────────


def test_get_state_returns_local_adapter() -> None:
    """get_state() with RRA_PROFILE=local returns a PostgresStateAdapter."""
    from rra.adapters.postgres_state import PostgresStateAdapter
    from rra.ports.state import get_state

    get_state.cache_clear()
    try:
        adapter = get_state()
        assert isinstance(adapter, PostgresStateAdapter)
    finally:
        get_state.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_state_raises_not_implemented_for_cloud_profiles(profile: str) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.state import get_state

    get_state.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_state()
    finally:
        get_state.cache_clear()


# ─── Singleton behaviour ──────────────────────────────────────────────────────


def test_get_state_is_singleton() -> None:
    """Two successive get_state() calls return the same object (lru_cache)."""
    from rra.ports.state import get_state

    get_state.cache_clear()
    try:
        first = get_state()
        second = get_state()
        assert first is second
    finally:
        get_state.cache_clear()


# ─── Adapter: checkpointer() ──────────────────────────────────────────────────


def test_checkpointer_returns_postgres_saver() -> None:
    """PostgresStateAdapter.checkpointer() returns a PostgresSaver and calls setup()."""
    import rra.adapters.postgres_state as _state_mod
    from rra.adapters.postgres_state import PostgresStateAdapter

    mock_conn = MagicMock()
    mock_saver = MagicMock()

    # Reset module-level singleton so the lazy-creation path is exercised.
    original = _state_mod._checkpointer
    _state_mod._checkpointer = None
    try:
        with (
            patch("rra.adapters.postgres_state.psycopg.connect", return_value=mock_conn),
            patch("rra.adapters.postgres_state.PostgresSaver", return_value=mock_saver),
        ):
            adapter = PostgresStateAdapter()
            result = adapter.checkpointer()

        assert result is mock_saver
        mock_saver.setup.assert_called_once()
    finally:
        _state_mod._checkpointer = original


def test_checkpointer_is_process_lifetime_singleton() -> None:
    """Calling checkpointer() twice returns the same PostgresSaver object."""
    import rra.adapters.postgres_state as _state_mod
    from rra.adapters.postgres_state import PostgresStateAdapter

    mock_conn = MagicMock()
    mock_saver = MagicMock()

    original = _state_mod._checkpointer
    _state_mod._checkpointer = None
    try:
        with (
            patch("rra.adapters.postgres_state.psycopg.connect", return_value=mock_conn),
            patch("rra.adapters.postgres_state.PostgresSaver", return_value=mock_saver),
        ):
            adapter = PostgresStateAdapter()
            first = adapter.checkpointer()
            second = adapter.checkpointer()

        assert first is second
        # setup() should only be called once (on first creation)
        mock_saver.setup.assert_called_once()
    finally:
        _state_mod._checkpointer = original


def test_checkpointer_uses_autocommit_connection() -> None:
    """PostgresStateAdapter opens the connection with autocommit=True."""
    import rra.adapters.postgres_state as _state_mod
    from rra.adapters.postgres_state import PostgresStateAdapter

    mock_conn = MagicMock()
    mock_saver = MagicMock()

    original = _state_mod._checkpointer
    _state_mod._checkpointer = None
    try:
        with (
            patch("rra.adapters.postgres_state.psycopg.connect", return_value=mock_conn) as mock_connect,
            patch("rra.adapters.postgres_state.PostgresSaver", return_value=mock_saver),
        ):
            adapter = PostgresStateAdapter()
            adapter.checkpointer()

        connect_kwargs = mock_connect.call_args.kwargs
        assert connect_kwargs.get("autocommit") is True
        assert connect_kwargs.get("prepare_threshold") == 0
    finally:
        _state_mod._checkpointer = original
