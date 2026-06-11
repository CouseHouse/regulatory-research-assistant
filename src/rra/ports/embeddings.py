"""Embeddings/rerank port: stable protocol that retrieval and ingest target.

Design note (ADR 0005):
  Voyage AI is the current embedding and rerank provider.  The port abstracts
  the THREE operations retrieval.py and ingest.py use today:

    embed_query   — single query string → vector (input_type="query")
    embed_documents — batch of document strings → list of vectors
                      (input_type="document"; used by ingest)
    rerank        — re-order candidate texts for a query

  The asymmetry between embed_query (no tenacity retry in retrieval) and
  embed_documents (tenacity retry in ingest, VOYAGE_MAX_BATCH=128 batching)
  is INTENTIONAL and preserved in the adapter.  embed_documents wraps the
  retried batch loop; embed_query is a single call with no retry.

Factory:
  ``get_embeddings()`` is the only entry point.  Profile-resolved and
  lru_cache'd to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from rra.config import settings


@dataclass(frozen=True, slots=True)
class RerankResult:
    """One ranked item returned by rerank().

    Matches the attribute contract that retrieval.py consumes from the
    voyageai response (``r.index``, ``r.relevance_score``).
    """

    index: int
    relevance_score: float


class EmbeddingsPort(Protocol):
    """Protocol for embedding and rerank operations."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string.

        Uses input_type="query" (asymmetric model; corpus was indexed with
        "document" — these are NOT interchangeable per ADR 0005).
        No tenacity retry: single fast call on the retrieval hot-path.
        """
        ...  # pragma: no cover

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document strings.

        Uses input_type="document".  The adapter batches internally at
        VOYAGE_MAX_BATCH (128) with tenacity retry on transient errors —
        this is the ingest hot-path.  ``texts`` may be any length.
        """
        ...  # pragma: no cover

    def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[RerankResult]:
        """Re-rank *documents* for *query* and return the top *top_k*.

        Returns a list of RerankResult (index into *documents*, relevance_score)
        in descending relevance order — the same shape retrieval.py iterates.
        """
        ...  # pragma: no cover


@lru_cache(maxsize=1)
def get_embeddings() -> EmbeddingsPort:
    """Return the profile-resolved embeddings adapter (process-lifetime singleton).

    Adapter modules are imported lazily here so that future cloud adapters
    are not imported in the local profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.voyage_embeddings import VoyageEmbeddingsAdapter  # lazy

        return VoyageEmbeddingsAdapter()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
