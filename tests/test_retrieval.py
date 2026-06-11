"""Tests for rra.retrieval.search_corpus.

Unit tests mock the Voyage client and DB. Integration tests (marked
@pytest.mark.integration) require a live docker-compose Postgres.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rra.schemas import RetrievedPassage


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _db_rows(n: int = 3) -> list[dict[str, Any]]:
    """Fake corpus.chunks rows as returned by a dict_row cursor."""
    return [
        {
            "guidance_id": f"doc{i}",
            "guidance_title": f"FDA Guidance {i}",
            "chunk_index": i,
            "text": f"Regulatory text for chunk {i}. " * 5,
            "char_start": i * 200,
            "char_end": i * 200 + 150,
            "score": round(0.9 - i * 0.05, 2),
        }
        for i in range(n)
    ]


def _mock_embeddings_port(
    rerank_indices: list[int],
    rerank_scores: list[float],
) -> MagicMock:
    """Return a mock EmbeddingsPort for the given rerank result."""
    from rra.ports.embeddings import RerankResult

    mock_port = MagicMock()
    mock_port.embed_query.return_value = [0.1] * 1024
    mock_port.rerank.return_value = [
        RerankResult(index=idx, relevance_score=score)
        for idx, score in zip(rerank_indices, rerank_scores)
    ]
    return mock_port


def _mock_get_conn(db_rows: list[dict[str, Any]]) -> MagicMock:
    """Return a mock get_conn context manager preconfigured for the given rows."""
    mock_cur = MagicMock()
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = db_rows

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur

    mock_get_conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = mock_conn
    mock_get_conn.return_value.__exit__.return_value = False
    return mock_get_conn


# ─── Unit tests ────────────────────────────────────────────────────────────────

def test_embed_uses_query_input_type() -> None:
    """ADR 0005: search_corpus must embed the query with input_type='query'.

    After the ports refactor, retrieval delegates to get_embeddings().embed_query()
    which uses input_type='query' in VoyageEmbeddingsAdapter. This test pins the
    port contract: embed_query is called (not embed_documents).
    """
    from rra.retrieval import search_corpus

    rows = _db_rows(1)
    mock_port = _mock_embeddings_port([0], [0.9])
    mgc = _mock_get_conn(rows)

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        search_corpus("what are the SaMD requirements?")

    mock_port.embed_query.assert_called_once_with("what are the SaMD requirements?")
    mock_port.embed_documents.assert_not_called()


def test_embed_not_document_input_type() -> None:
    """ADR 0005: guard against using embed_documents (input_type='document') for queries."""
    from rra.retrieval import search_corpus

    rows = _db_rows(1)
    mock_port = _mock_embeddings_port([0], [0.9])
    mgc = _mock_get_conn(rows)

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        search_corpus("test")

    # embed_documents uses input_type='document' — must NOT be called for a query.
    mock_port.embed_documents.assert_not_called()


def test_reranker_called_with_query_and_texts() -> None:
    """Reranker receives the original query and the candidate passage texts."""
    from rra.retrieval import search_corpus

    rows = _db_rows(2)
    mock_port = _mock_embeddings_port([1, 0], [0.95, 0.80])
    mgc = _mock_get_conn(rows)

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        search_corpus("software validation requirements")

    rerank_call = mock_port.rerank.call_args
    assert rerank_call.args[0] == "software validation requirements"
    assert len(rerank_call.args[1]) == 2


def test_returns_in_rerank_order() -> None:
    """Passages are returned in reranker order, not original DB order."""
    from rra.retrieval import search_corpus

    rows = _db_rows(3)
    # Reranker says best=index 2, second=index 0 (drops index 1)
    mock_port = _mock_embeddings_port([2, 0], [0.99, 0.75])
    mgc = _mock_get_conn(rows)

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        results = search_corpus("test query", k=2)

    assert len(results) == 2
    assert results[0].guidance_id == "doc2"
    assert results[1].guidance_id == "doc0"
    assert results[0].score == pytest.approx(0.99)
    assert results[1].score == pytest.approx(0.75)


def test_empty_corpus_returns_empty_without_rerank() -> None:
    """Empty DB result returns [] immediately; reranker is never called."""
    from rra.retrieval import search_corpus

    mock_port = _mock_embeddings_port([], [])
    mgc = _mock_get_conn([])

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        results = search_corpus("test query")

    assert results == []
    mock_port.rerank.assert_not_called()


def test_guidance_ids_filter_adds_any_clause() -> None:
    """When guidance_ids filter is set, the SQL contains an ANY clause."""
    from rra.retrieval import search_corpus

    rows = _db_rows(1)
    mock_port = _mock_embeddings_port([0], [0.9])
    mgc = _mock_get_conn(rows)

    mock_conn = mgc.return_value.__enter__.return_value
    mock_cur = mock_conn.cursor.return_value.__enter__.return_value

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        search_corpus("test", filters={"guidance_ids": ["doc1", "doc2"]})

    sql_executed: str = mock_cur.execute.call_args.args[0]
    assert "ANY" in sql_executed


def test_empty_guidance_ids_filter_ignored() -> None:
    """An empty guidance_ids list should behave as no filter (no ANY clause)."""
    from rra.retrieval import search_corpus

    rows = _db_rows(1)
    mock_port = _mock_embeddings_port([0], [0.9])
    mgc = _mock_get_conn(rows)

    mock_conn = mgc.return_value.__enter__.return_value
    mock_cur = mock_conn.cursor.return_value.__enter__.return_value

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        search_corpus("test", filters={"guidance_ids": []})

    sql_executed: str = mock_cur.execute.call_args.args[0]
    assert "ANY" not in sql_executed


def test_returned_passages_have_correct_schema() -> None:
    """Each returned passage validates against RetrievedPassage."""
    from rra.retrieval import search_corpus

    rows = _db_rows(2)
    mock_port = _mock_embeddings_port([0, 1], [0.9, 0.8])
    mgc = _mock_get_conn(rows)

    with (
        patch("rra.retrieval.get_embeddings", return_value=mock_port),
        patch("rra.retrieval.get_conn", mgc),
    ):
        results = search_corpus("test")

    assert all(isinstance(p, RetrievedPassage) for p in results)
    for p in results:
        assert p.guidance_id
        assert p.chunk_index >= 0
        assert p.char_end > p.char_start


# ─── Integration tests ─────────────────────────────────────────────────────────

def _postgres_reachable() -> bool:
    try:
        import psycopg
        from rra.config import settings

        with psycopg.connect(settings.pg_dsn, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=False)
def require_postgres_for_retrieval() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres unavailable — run `docker compose up -d` first")


@pytest.mark.integration
def test_search_corpus_returns_results_from_live_db(
    require_postgres_for_retrieval: None,
) -> None:
    """search_corpus returns non-empty results against the real corpus."""
    from rra.retrieval import search_corpus

    # Clear the lru_cache so the real Voyage client is constructed.
    search_corpus.cache_clear() if hasattr(search_corpus, "cache_clear") else None  # type: ignore[attr-defined]

    results = search_corpus("software validation requirements for medical devices")

    assert len(results) > 0, "Expected at least one passage from the live corpus"
    assert all(isinstance(p, RetrievedPassage) for p in results)
    first = results[0]
    assert first.guidance_id
    assert first.text
    assert first.score > 0
