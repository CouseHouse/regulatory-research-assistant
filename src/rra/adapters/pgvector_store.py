"""PgVector vector store adapter for the local profile.

Consolidates the SQL and DB access formerly scattered across:
  - rra/retrieval.py     — similarity search (get_conn)
  - rra/mcp_server/tools.py — fetch_chunk, fetch_guidance_chunks,
                               list_recent_guidances_rows (get_conn)
  - rra/ingest.py        — upsert_chunks, ensure_schema (psycopg.connect direct)

Design choice: rra.db (get_pool / get_conn) stays in db.py as an internal
helper; this adapter is the only src/rra module outside tests that calls it for
corpus operations.  api.py keeps its own get_pool import for the /readyz health
probe — that is not a corpus operation and is intentionally outside the port.
"""
from __future__ import annotations

from typing import Any, Final

import psycopg
import structlog
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from rra.config import settings
from rra.db import get_conn

log = structlog.get_logger(__name__)

# ─── SQL strings (verbatim from original callers) ─────────────────────────────

_SIMILARITY_BASE_SQL: Final[str] = """
SELECT
    guidance_id,
    guidance_title,
    chunk_index,
    text,
    char_start,
    char_end,
    1 - (embedding <=> %(query_embedding)s::vector) AS score
FROM corpus.chunks
{where_clause}
ORDER BY embedding <=> %(query_embedding)s::vector
LIMIT %(limit)s
"""

_FETCH_CHUNK_SQL: Final[str] = """
    SELECT text, char_start, char_end
    FROM corpus.chunks
    WHERE guidance_id = %(guidance_id)s AND chunk_index = %(chunk_index)s
"""

_FETCH_GUIDANCE_CHUNKS_SQL: Final[str] = """
    SELECT chunk_index, guidance_title, text
    FROM corpus.chunks
    WHERE guidance_id = %(guidance_id)s
    ORDER BY chunk_index
"""

_LIST_RECENT_GUIDANCES_SQL: Final[str] = """
    SELECT guidance_id, guidance_title, MIN(created_at) AS ingest_date
    FROM corpus.chunks
    GROUP BY guidance_id, guidance_title
    HAVING MIN(created_at) >= %(since_date)s
    ORDER BY ingest_date DESC
"""

_UPSERT_SQL: Final[str] = """
INSERT INTO corpus.chunks
    (guidance_id, guidance_title, chunk_index, text, char_start, char_end, token_count, embedding, metadata)
VALUES
    (%(guidance_id)s, %(guidance_title)s, %(chunk_index)s, %(text)s,
     %(char_start)s, %(char_end)s, %(token_count)s, %(embedding)s, %(metadata)s)
ON CONFLICT (guidance_id, chunk_index) DO UPDATE SET
    guidance_title = EXCLUDED.guidance_title,
    text           = EXCLUDED.text,
    char_start     = EXCLUDED.char_start,
    char_end       = EXCLUDED.char_end,
    token_count    = EXCLUDED.token_count,
    embedding      = EXCLUDED.embedding,
    metadata       = EXCLUDED.metadata
"""


class PgVectorStoreAdapter:
    """VectorStorePort implementation backed by Postgres + pgvector."""

    def similarity_search(
        self,
        embedding: list[float],
        top_k: int,
        guidance_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Cosine-distance search over corpus.chunks, top_k rows.

        The embedding is formatted as a pgvector text literal '[v1,v2,...]'
        and cast with ::vector in SQL.  This sidesteps psycopg3 adapter
        registration entirely — the adapter (register_vector) does not
        propagate reliably on pooled connections under asyncio's threadpool.
        Postgres's own text→vector cast is unambiguous and always works.
        """
        query_embedding: str = "[" + ",".join(map(str, embedding)) + "]"

        where_clause = (
            "WHERE guidance_id = ANY(%(guidance_ids)s)" if guidance_ids else ""
        )
        sql = _SIMILARITY_BASE_SQL.format(where_clause=where_clause)

        params: dict[str, Any] = {
            "query_embedding": query_embedding,
            "limit": top_k,
        }
        if guidance_ids:
            params["guidance_ids"] = guidance_ids

        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def fetch_chunk(
        self,
        guidance_id: str,
        chunk_index: int,
    ) -> dict[str, Any] | None:
        """Fetch a single chunk row by (guidance_id, chunk_index)."""
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _FETCH_CHUNK_SQL,
                    {"guidance_id": guidance_id, "chunk_index": chunk_index},
                )
                row: dict[str, Any] | None = cur.fetchone()
        return row

    def fetch_guidance_chunks(
        self,
        guidance_id: str,
    ) -> list[dict[str, Any]]:
        """Fetch all chunks for a guidance_id, ordered by chunk_index."""
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_FETCH_GUIDANCE_CHUNKS_SQL, {"guidance_id": guidance_id})
                rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def list_recent_guidances_rows(
        self,
        since_date: str,
    ) -> list[dict[str, Any]]:
        """Return aggregated ingest-date rows for guidances ingested since since_date."""
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_LIST_RECENT_GUIDANCES_SQL, {"since_date": since_date})
                rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def upsert_chunks(
        self,
        rows: list[dict[str, Any]],
    ) -> None:
        """Upsert a batch of embedded-chunk rows into corpus.chunks.

        Opens a fresh direct psycopg connection (not the pool) — mirrors the
        original write_to_postgres which used psycopg.connect directly for
        ingest batch isolation.  ensure_schema() is called idempotently on
        each call so the pipeline is self-bootstrapping.
        """
        if not rows:
            return

        with psycopg.connect(settings.pg_dsn) as conn:
            self.ensure_schema(conn=conn)
            register_vector(conn)
            with conn.cursor() as cur:
                cur.executemany(_UPSERT_SQL, rows)
            # psycopg Connection context manager commits on clean exit

    def ensure_schema(
        self,
        conn: psycopg.Connection[Any] | None = None,
    ) -> None:
        """Create the corpus schema and chunks table if they do not exist.

        If *conn* is supplied (by upsert_chunks, which already has a
        connection open) it is reused so the DDL and the data share one
        transaction.  If *conn* is None a fresh connection is opened — this
        is the ingest CLI bootstrap path (--truncate, main()).
        """
        dim = settings.embedding_dim
        ddl = f"""
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE SCHEMA IF NOT EXISTS corpus;
        CREATE SCHEMA IF NOT EXISTS app;
        CREATE TABLE IF NOT EXISTS corpus.chunks (
            id              bigserial    PRIMARY KEY,
            guidance_id     text         NOT NULL,
            guidance_title  text         NOT NULL,
            section         text,
            chunk_index     int          NOT NULL,
            text            text         NOT NULL,
            char_start      int          NOT NULL,
            char_end        int          NOT NULL,
            token_count     int          NOT NULL,
            embedding       vector({dim}) NOT NULL,
            metadata        jsonb        DEFAULT '{{}}'::jsonb,
            created_at      timestamptz  NOT NULL DEFAULT now(),
            UNIQUE (guidance_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS chunks_guidance_id_idx
            ON corpus.chunks (guidance_id);
        CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
            ON corpus.chunks USING hnsw (embedding vector_cosine_ops);
        """
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(ddl)
        else:
            with psycopg.connect(settings.pg_dsn) as fresh_conn:
                with fresh_conn.cursor() as cur:
                    cur.execute(ddl)
