"""Tests for the embeddings/rerank port and its local adapter.

Covers:
  - get_embeddings() returns the local adapter under RRA_PROFILE=local.
  - get_embeddings() raises NotImplementedError for aws/azure/gcp.
  - VoyageEmbeddingsAdapter.embed_query() uses input_type='query'.
  - VoyageEmbeddingsAdapter.embed_documents() batches at VOYAGE_MAX_BATCH.
  - VoyageEmbeddingsAdapter.rerank() returns RerankResult list.
  - get_embeddings() is a process-lifetime singleton.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from unittest.mock import MagicMock, call, patch

import pytest


# ─── Factory: profile resolution ───────────────────────────────────────────────

def test_get_embeddings_returns_local_adapter() -> None:
    """get_embeddings() with RRA_PROFILE=local returns a VoyageEmbeddingsAdapter."""
    from rra.adapters.voyage_embeddings import VoyageEmbeddingsAdapter
    from rra.ports.embeddings import get_embeddings

    get_embeddings.cache_clear()
    try:
        with patch("rra.adapters.voyage_embeddings.voyageai"):
            adapter = get_embeddings()
        assert isinstance(adapter, VoyageEmbeddingsAdapter)
    finally:
        get_embeddings.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_embeddings_raises_not_implemented_for_cloud_profiles(profile: str) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.embeddings import get_embeddings

    get_embeddings.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_embeddings()
    finally:
        get_embeddings.cache_clear()


# ─── Singleton behaviour ────────────────────────────────────────────────────────

def test_get_embeddings_is_singleton() -> None:
    """Two successive get_embeddings() calls return the same object."""
    from rra.ports.embeddings import get_embeddings

    get_embeddings.cache_clear()
    try:
        with patch("rra.adapters.voyage_embeddings.voyageai"):
            first = get_embeddings()
            second = get_embeddings()
        assert first is second
    finally:
        get_embeddings.cache_clear()


# ─── embed_query ───────────────────────────────────────────────────────────────

def test_embed_query_uses_query_input_type() -> None:
    """ADR 0005: embed_query must call client.embed with input_type='query'."""
    from rra.adapters.voyage_embeddings import VoyageEmbeddingsAdapter
    from rra.config import settings

    mock_client = MagicMock()
    mock_client.embed.return_value = MagicMock(embeddings=[[0.1] * 1024])

    adapter = VoyageEmbeddingsAdapter.__new__(VoyageEmbeddingsAdapter)
    adapter._client = mock_client

    result = adapter.embed_query("what is 510k?")

    mock_client.embed.assert_called_once_with(
        ["what is 510k?"],
        model=settings.embedding_model,
        input_type="query",
    )
    assert len(result) == 1024
    assert result[0] == pytest.approx(0.1)


# ─── embed_documents ───────────────────────────────────────────────────────────

def test_embed_documents_uses_document_input_type() -> None:
    """embed_documents must call client.embed with input_type='document'."""
    from rra.adapters.voyage_embeddings import VoyageEmbeddingsAdapter
    from rra.config import settings

    mock_client = MagicMock()
    mock_client.embed.return_value = MagicMock(embeddings=[[0.2] * 1024])

    adapter = VoyageEmbeddingsAdapter.__new__(VoyageEmbeddingsAdapter)
    adapter._client = mock_client

    result = adapter.embed_documents(["doc text"])

    mock_client.embed.assert_called_once_with(
        ["doc text"],
        model=settings.embedding_model,
        input_type="document",
    )
    assert len(result) == 1
    assert result[0][0] == pytest.approx(0.2)


def test_embed_documents_batches_at_voyage_max() -> None:
    """embed_documents splits large lists into batches of at most VOYAGE_MAX_BATCH."""
    from rra.adapters.voyage_embeddings import VOYAGE_MAX_BATCH, VoyageEmbeddingsAdapter

    n = VOYAGE_MAX_BATCH + 10
    texts = [f"text {i}" for i in range(n)]

    mock_client = MagicMock()

    def fake_embed(batch, model, input_type):
        return MagicMock(embeddings=[[0.0] * 1024 for _ in batch])

    mock_client.embed.side_effect = fake_embed

    adapter = VoyageEmbeddingsAdapter.__new__(VoyageEmbeddingsAdapter)
    adapter._client = mock_client

    result = adapter.embed_documents(texts)

    assert len(result) == n
    # Should have been called twice: one full batch + one partial batch.
    assert mock_client.embed.call_count == 2
    first_batch_size = len(mock_client.embed.call_args_list[0].args[0])
    second_batch_size = len(mock_client.embed.call_args_list[1].args[0])
    assert first_batch_size == VOYAGE_MAX_BATCH
    assert second_batch_size == 10


# ─── rerank ────────────────────────────────────────────────────────────────────

def test_rerank_returns_rerank_result_list() -> None:
    """rerank() returns a list of RerankResult with correct index and score."""
    from rra.adapters.voyage_embeddings import VoyageEmbeddingsAdapter
    from rra.config import settings
    from rra.ports.embeddings import RerankResult

    mock_result_0 = MagicMock(index=1, relevance_score=0.95)
    mock_result_1 = MagicMock(index=0, relevance_score=0.80)
    mock_client = MagicMock()
    mock_client.rerank.return_value = MagicMock(results=[mock_result_0, mock_result_1])

    adapter = VoyageEmbeddingsAdapter.__new__(VoyageEmbeddingsAdapter)
    adapter._client = mock_client

    results = adapter.rerank("query", ["doc0", "doc1"], top_k=2)

    mock_client.rerank.assert_called_once_with(
        "query", ["doc0", "doc1"], model=settings.rerank_model, top_k=2
    )
    assert len(results) == 2
    assert isinstance(results[0], RerankResult)
    assert results[0].index == 1
    assert results[0].relevance_score == pytest.approx(0.95)
    assert results[1].index == 0
    assert results[1].relevance_score == pytest.approx(0.80)
