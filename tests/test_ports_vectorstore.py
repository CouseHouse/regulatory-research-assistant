"""Tests for the vector store port and its local adapter.

Covers:
  - get_vector_store() returns the local adapter under RRA_PROFILE=local.
  - get_vector_store() raises NotImplementedError for aws/azure/gcp.
  - PgVectorStoreAdapter delegates each method to get_conn / psycopg correctly.
  - get_vector_store() is a process-lifetime singleton (lru_cache).

Integration tests (marked @pytest.mark.integration) are already provided by
test_retrieval and test_ingest_integration which exercise the SQL end-to-end
against a live Postgres; we do not duplicate them here.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_cursor(rows: list[dict] | dict | None) -> MagicMock:
    """Return a mock dict_row cursor."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    if isinstance(rows, list):
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
    else:
        cur.fetchone.return_value = rows
        cur.fetchall.return_value = [] if rows is None else [rows]
    return cur


def _make_get_conn(rows: list[dict] | dict | None):
    """Return a patched get_conn context manager returning the given rows."""
    cur = _make_cursor(rows)
    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    mock_get_conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = conn
    mock_get_conn.return_value.__exit__.return_value = False
    return mock_get_conn, cur


# ─── Factory: profile resolution ─────────────────────────────────────────────


def test_get_vector_store_returns_local_adapter() -> None:
    """get_vector_store() with RRA_PROFILE=local returns a PgVectorStoreAdapter."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter
    from rra.ports.vectorstore import get_vector_store

    get_vector_store.cache_clear()
    try:
        adapter = get_vector_store()
        assert isinstance(adapter, PgVectorStoreAdapter)
    finally:
        get_vector_store.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_vector_store_raises_not_implemented_for_cloud_profiles(
    profile: str,
) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.vectorstore import get_vector_store

    get_vector_store.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_vector_store()
    finally:
        get_vector_store.cache_clear()


# ─── Singleton behaviour ──────────────────────────────────────────────────────


def test_get_vector_store_is_singleton() -> None:
    """Two successive get_vector_store() calls return the same object."""
    from rra.ports.vectorstore import get_vector_store

    get_vector_store.cache_clear()
    try:
        first = get_vector_store()
        second = get_vector_store()
        assert first is second
    finally:
        get_vector_store.cache_clear()


# ─── similarity_search ────────────────────────────────────────────────────────


def test_similarity_search_returns_rows() -> None:
    """similarity_search builds the SQL internally and returns all rows."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    expected = [{"guidance_id": "g1", "score": 0.9}]
    mgc, cur = _make_get_conn(expected)

    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        rows = adapter.similarity_search(embedding=[0.1, 0.2], top_k=5)

    assert rows == expected
    cur.execute.assert_called_once()
    sql, params = cur.execute.call_args.args
    assert "ORDER BY embedding <=>" in sql
    assert "ANY" not in sql  # no filter requested
    assert params["query_embedding"] == "[0.1,0.2]"
    assert params["limit"] == 5


def test_similarity_search_guidance_ids_filter() -> None:
    """A guidance_ids filter produces the ANY clause and param inside the adapter."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    mgc, cur = _make_get_conn([])
    adapter = PgVectorStoreAdapter()

    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        adapter.similarity_search(
            embedding=[0.1], top_k=10, guidance_ids=["doc1", "doc2"]
        )

    sql, params = cur.execute.call_args.args
    assert "WHERE guidance_id = ANY(%(guidance_ids)s)" in sql
    assert params["guidance_ids"] == ["doc1", "doc2"]
    assert params["limit"] == 10


# ─── fetch_chunk ─────────────────────────────────────────────────────────────


def test_fetch_chunk_returns_row_when_found() -> None:
    """fetch_chunk returns the dict row when the chunk exists."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    row = {"text": "chunk text", "char_start": 0, "char_end": 10}
    mgc, cur = _make_get_conn(row)

    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.fetch_chunk("gd-1", 3)

    assert result == row
    # fetchone is called (not fetchall)
    cur.fetchone.assert_called_once()


def test_fetch_chunk_returns_none_when_not_found() -> None:
    """fetch_chunk returns None when no matching chunk exists."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    mgc, cur = _make_get_conn(None)
    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.fetch_chunk("gd-missing", 99)

    assert result is None


# ─── fetch_guidance_chunks ────────────────────────────────────────────────────


def test_fetch_guidance_chunks_returns_ordered_rows() -> None:
    """fetch_guidance_chunks returns all rows for the given guidance_id."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    rows = [
        {"chunk_index": 0, "guidance_title": "Guide", "text": "Part A"},
        {"chunk_index": 1, "guidance_title": "Guide", "text": "Part B"},
    ]
    mgc, cur = _make_get_conn(rows)
    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.fetch_guidance_chunks("gd-1")

    assert result == rows
    cur.fetchall.assert_called_once()


def test_fetch_guidance_chunks_empty_when_not_found() -> None:
    """fetch_guidance_chunks returns [] when the guidance_id has no chunks."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    mgc, cur = _make_get_conn([])
    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.fetch_guidance_chunks("gd-missing")

    assert result == []


# ─── list_recent_guidances_rows ───────────────────────────────────────────────


def test_list_recent_guidances_rows_returns_rows() -> None:
    """list_recent_guidances_rows returns the grouped rows from the DB."""
    from datetime import datetime, timezone

    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    rows = [
        {
            "guidance_id": "gd-1",
            "guidance_title": "Device Guide",
            "ingest_date": datetime(2026, 1, 15, tzinfo=timezone.utc),
        }
    ]
    mgc, cur = _make_get_conn(rows)
    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.list_recent_guidances_rows("2026-01-01")

    assert result == rows
    cur.fetchall.assert_called_once()


def test_list_recent_guidances_rows_empty() -> None:
    """list_recent_guidances_rows returns [] when no rows match."""
    from rra.adapters.pgvector_store import PgVectorStoreAdapter

    mgc, cur = _make_get_conn([])
    adapter = PgVectorStoreAdapter()
    with patch("rra.adapters.pgvector_store.get_conn", mgc):
        result = adapter.list_recent_guidances_rows("2030-01-01")

    assert result == []
