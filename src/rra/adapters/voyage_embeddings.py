"""Voyage AI embeddings/rerank adapter for the local profile.

Consolidates the two former call-sites:
  - retrieval.py: ``_voyage_client()`` singleton + embed (no retry) + rerank
  - ingest.py: ``voyageai.Client(...)`` per ``_embed_batch`` call + tenacity retry

Both had a voyageai.Client; this adapter gives the process one shared client
(matching the ``_voyage_client()`` singleton pattern that already existed in
retrieval.py).

Asymmetry is INTENTIONAL and preserved:
  - ``embed_query`` is a single call with NO tenacity retry: this runs on the
    retrieval hot-path where a single query must be fast.
  - ``embed_documents`` uses a tenacity retry loop with exponential back-off
    on transient Voyage errors (rate-limit, server error, timeout, …):
    this is the ingest batch path where retrying is correct.

``VOYAGE_MAX_BATCH`` (128) is a provider limit and belongs here, not in ingest.
"""
from __future__ import annotations

import voyageai
import voyageai.error as _voyageai_error
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from rra.config import settings
from rra.ports.embeddings import RerankResult

# Voyage AI hard limit: max items per embed call.
# Moved from ingest.py where it was a module-level constant — it is a
# provider limit and belongs to the adapter.
VOYAGE_MAX_BATCH: int = 128


def _is_embed_retryable(exc: BaseException) -> bool:
    """True for transient Voyage errors; False for permanent client errors.

    Mirrors the predicate formerly in ingest._is_embed_retryable.
    AuthenticationError and InvalidRequestError are permanent — retrying
    wastes time and still fails.
    """
    return isinstance(
        exc,
        (
            _voyageai_error.RateLimitError,
            _voyageai_error.ServerError,
            _voyageai_error.ServiceUnavailableError,
            _voyageai_error.APIConnectionError,
            _voyageai_error.TryAgain,
            _voyageai_error.Timeout,
        ),
    )


class VoyageEmbeddingsAdapter:
    """EmbeddingsPort implementation backed by ``voyageai.Client``."""

    def __init__(self) -> None:
        self._client: voyageai.Client = voyageai.Client(  # type: ignore[attr-defined,name-defined]
            api_key=settings.voyage_api_key.get_secret_value()
        )

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query string (input_type='query', no retry).

        ADR 0005: query must use input_type='query'; corpus was indexed with
        'document'. These are NOT interchangeable — using the wrong type
        silently degrades recall.
        """
        resp = self._client.embed(
            [text], model=settings.embedding_model, input_type="query"
        )
        return [float(v) for v in resp.embeddings[0]]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document strings, batched at VOYAGE_MAX_BATCH with retry.

        The full ``texts`` list is chunked into sub-batches of at most
        VOYAGE_MAX_BATCH items.  Each sub-batch is sent through
        ``_embed_batch_retried`` which carries tenacity retry on transient
        errors.

        Asymmetry note: unlike embed_query, this method retries on transient
        failures because it runs in the ingest pipeline where correctness and
        reproducibility beat latency.
        """
        result: list[list[float]] = []
        for start in range(0, len(texts), VOYAGE_MAX_BATCH):
            batch = texts[start : start + VOYAGE_MAX_BATCH]
            embeddings = self._embed_batch_retried(batch)
            if len(embeddings) != len(batch):
                raise RuntimeError(
                    f"Voyage returned {len(embeddings)} embeddings for {len(batch)} inputs"
                )
            result.extend(embeddings)
        return result

    @retry(
        retry=retry_if_exception(_is_embed_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _embed_batch_retried(self, texts: list[str]) -> list[list[float]]:
        """Call Voyage embed for a single batch (≤ VOYAGE_MAX_BATCH) with retry.

        Moved verbatim from ingest._embed_batch; tenacity decorator preserved.
        """
        response = self._client.embed(
            texts, model=settings.embedding_model, input_type="document"
        )
        # voyageai stubs type .embeddings as list[list[float]] | list[list[int]];
        # explicit float conversion ensures the return type is always list[list[float]].
        return [[float(v) for v in emb] for emb in response.embeddings]

    def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[RerankResult]:
        """Re-rank documents and return the top top_k as RerankResult list.

        Wraps voyageai's rerank response into RerankResult so that retrieval.py
        can consume it without importing voyageai directly.
        """
        resp = self._client.rerank(
            query, documents, model=settings.rerank_model, top_k=top_k
        )
        return [
            RerankResult(index=r.index, relevance_score=float(r.relevance_score))
            for r in resp.results
        ]
