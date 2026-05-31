"""Shared Postgres connection pool (ADR 0004).

Process-lifetime singleton pool. All query-path code borrows connections
via get_conn(). The ingest batch job (rra/ingest.py) opens its own
connection because it runs as a separate one-shot process.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from rra.config import settings

_pool: ConnectionPool[psycopg.Connection[Any]] | None = None


def get_pool() -> ConnectionPool[psycopg.Connection[Any]]:
    """Return the process-lifetime pool, creating it on first call."""
    global _pool
    pool = _pool
    if pool is None:
        pool = ConnectionPool(
            settings.pg_dsn,
            min_size=1,
            max_size=10,
        )
        _pool = pool
    return pool


@contextmanager
def get_conn() -> Generator[psycopg.Connection[Any], None, None]:
    """Borrow a connection from the pool; return it on context exit.

    register_vector() is called on each borrow rather than in a pool
    configure callback because the callback runs in a background thread
    and races the first request. Calling it here is safe (idempotent)
    and guarantees the pgvector adapter is present before any query runs.
    """
    with get_pool().connection() as conn:
        register_vector(conn)
        yield conn
