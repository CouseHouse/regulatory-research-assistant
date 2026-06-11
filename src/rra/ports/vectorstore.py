"""Vector store port: stable protocol that retrieval, ingest, and MCP tools target.

Design note:
  The vector store port abstracts exactly the operations that the three callers
  use today — no more, no less.  The method set is caller-driven:

    similarity_search   — retrieval.py: vector-distance search returning rows
    fetch_chunk         — mcp_server/tools.py (check_citation): single row by
                          (guidance_id, chunk_index)
    fetch_guidance_chunks — mcp_server/tools.py (fetch_guidance): all chunks
                            for a guidance_id, ordered by chunk_index
    list_recent_guidances_rows — mcp_server/tools.py (list_recent_guidances):
                                 MIN(created_at) GROUP BY aggregation
    upsert_chunks       — ingest.py (write_to_postgres): batch upsert with
                          ensure_schema idempotency
    ensure_schema       — ingest.py: DDL bootstrap; also called by upsert_chunks

  Return types are the EXISTING carriers (plain dicts for SQL rows; no new DTOs).
  The callers own the mapping from dict → RetrievedPassage / FetchGuidanceResult
  / etc. — the port is below that layer.

Factory:
  ``get_vector_store()`` is the only entry point.  Profile-resolved and
  lru_cache'd to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from rra.config import settings


class VectorStorePort(Protocol):
    """Protocol for corpus vector-store operations.

    All methods return plain dicts (as returned by psycopg dict_row) or None
    so that callers remain decoupled from the storage layer.
    """

    def similarity_search(
        self,
        embedding: list[float],
        top_k: int,
        guidance_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the *top_k* chunks most similar to *embedding*.

        Each row dict carries: guidance_id, guidance_title, chunk_index, text,
        char_start, char_end, score (cosine similarity in [0, 1]).  When
        *guidance_ids* is given, the search is restricted to those documents.
        The query representation (SQL, index API, vector literal format) is
        the adapter's concern — callers pass raw floats and policy only.
        """
        ...  # pragma: no cover

    def fetch_chunk(
        self,
        guidance_id: str,
        chunk_index: int,
    ) -> dict[str, Any] | None:
        """Fetch a single chunk row by (guidance_id, chunk_index).

        Returns the row dict or None when the chunk does not exist.
        Used by check_citation for key-existence and quote matching.
        """
        ...  # pragma: no cover

    def fetch_guidance_chunks(
        self,
        guidance_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all chunks for a guidance_id, ordered by chunk_index.

        Returns an empty list when the guidance_id is not found.
        Used by fetch_guidance to assemble the full document text.
        """
        ...  # pragma: no cover

    def list_recent_guidances_rows(
        self,
        since_date: str,
    ) -> list[dict[str, Any]]:
        """Return MIN(created_at) rows grouped by (guidance_id, guidance_title).

        Rows with MIN(created_at) >= since_date are included, ordered
        descending by ingest_date.  Used by list_recent_guidances.
        """
        ...  # pragma: no cover

    def upsert_chunks(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        """Upsert a batch of embedded-chunk rows into corpus.chunks.

        Calls ensure_schema() idempotently before the first upsert.
        ``rows`` must contain the keys: guidance_id, guidance_title,
        chunk_index, text, char_start, char_end, token_count, embedding,
        metadata.  All rows in one batch are written in a single transaction.
        """
        ...  # pragma: no cover

    def ensure_schema(self) -> None:
        """Create the corpus schema and chunks table if they do not exist.

        Idempotent: safe to call on every startup or ingest run.  DDL mirrors
        init-db/01-init.sql exactly — update both together.
        """
        ...  # pragma: no cover


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStorePort:
    """Return the profile-resolved vector store adapter (process-lifetime singleton).

    Adapter modules are imported lazily here so that future cloud adapters
    are not imported in the local profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.pgvector_store import PgVectorStoreAdapter  # lazy

        return PgVectorStoreAdapter()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
