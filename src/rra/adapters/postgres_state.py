"""Postgres-backed memory/state adapter for the local profile.

Moves the body of _get_checkpointer() from rra/graph.py verbatim into an
adapter so that graph.py can access the checkpointer through the StatePort.

The dedicated autocommit connection is intentional:
  - CREATE INDEX CONCURRENTLY (run by setup()) requires autocommit mode —
    Postgres forbids it inside a transaction block, which is psycopg3's
    default.
  - prepare_threshold=0 is the LangGraph-recommended psycopg3 compatibility
    setting.
  - This connection is SEPARATE from the request-path pool (ADR 0004) so that
    autocommit=True cannot bleed into query connections.
"""
from __future__ import annotations

from typing import Any

import psycopg
import structlog
from langgraph.checkpoint.postgres import PostgresSaver

from rra.config import settings

log = structlog.get_logger(__name__)

_checkpointer: PostgresSaver | None = None


class PostgresStateAdapter:
    """StatePort implementation backed by LangGraph's PostgresSaver."""

    def checkpointer(self) -> Any:
        """Return the process-lifetime PostgresSaver, creating it on first call.

        Body moved verbatim from graph._get_checkpointer().  The module-level
        ``_checkpointer`` singleton is preserved so that multiple calls to
        ``PostgresStateAdapter().checkpointer()`` return the same object
        (matching the previous process-lifetime singleton behaviour).
        """
        global _checkpointer
        if _checkpointer is None:
            conn = psycopg.connect(
                settings.pg_dsn,
                autocommit=True,
                prepare_threshold=0,
                row_factory=psycopg.rows.dict_row,
            )
            cp = PostgresSaver(conn)
            cp.setup()
            log.info("graph.checkpointer_ready")
            _checkpointer = cp
        return _checkpointer
