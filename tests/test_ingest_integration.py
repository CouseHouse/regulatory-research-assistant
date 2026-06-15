"""Integration tests for the ingest pipeline against a live Postgres instance.

These tests are marked @pytest.mark.integration and are skipped unless docker
compose is up. They test the parts unit tests cannot: that columns actually land
in the DB with the right types, and that the ON CONFLICT upsert is truly
idempotent.

Run with:
    uv run pytest -m integration
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

import pytest
import psycopg

from rra.config import settings
from rra.ingest import Chunk, EmbeddedChunk, chunk_text, write_to_postgres

# ─── Infrastructure ────────────────────────────────────────────────────────────

GUIDANCE_ID = "inttest-fixture-001"
GUIDANCE_TITLE = "Integration Test Guidance — Fixture Only"

FIXTURE_TEXT = (
    "This is a synthetic FDA guidance document used only by the integration "
    "test suite. Section 1: Purpose. Validates that the ingest pipeline writes "
    "all required columns to corpus.chunks and that re-running is idempotent. "
    "Section 2: Scope. Not a real guidance document. Do not cite. "
) * 6  # ~400 tokens — enough for one chunk, short enough to be fast


def _postgres_reachable() -> bool:
    try:
        with psycopg.connect(settings.pg_dsn, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_postgres() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres unavailable — run `docker compose up -d` first")


@pytest.fixture(autouse=True)
def clean_fixture_rows() -> None:
    """Delete integration-test rows before and after each test."""
    _purge()
    yield
    _purge()


def _purge() -> None:
    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM corpus.chunks WHERE guidance_id = %s", (GUIDANCE_ID,)
            )


def _fake_embedded(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    return [EmbeddedChunk(chunk=c, embedding=[0.01] * 1024) for c in chunks]


# ─── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_write_populates_all_columns() -> None:
    """All Chunk fields appear in corpus.chunks with correct values and types."""
    chunks = chunk_text(FIXTURE_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert chunks, "fixture text produced no chunks"

    write_to_postgres(_fake_embedded(chunks))

    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT guidance_id, guidance_title, chunk_index,
                       text, char_start, char_end, token_count,
                       embedding IS NOT NULL AS has_embedding
                FROM corpus.chunks
                WHERE guidance_id = %s
                ORDER BY chunk_index
                """,
                (GUIDANCE_ID,),
            )
            rows = cur.fetchall()

    assert len(rows) == len(chunks)
    for i, row in enumerate(rows):
        gid, gtitle, cidx, text, cs, ce, tc, has_emb = row
        assert gid == GUIDANCE_ID
        assert gtitle == GUIDANCE_TITLE
        assert cidx == i
        assert text == chunks[i].text
        assert cs == chunks[i].char_start
        assert ce == chunks[i].char_end
        assert tc == chunks[i].token_count
        assert tc > 0
        assert has_emb


@pytest.mark.integration
def test_write_is_idempotent() -> None:
    """Running write_to_postgres twice for the same doc produces no duplicate rows."""
    chunks = chunk_text(FIXTURE_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    embedded = _fake_embedded(chunks)

    write_to_postgres(embedded)
    write_to_postgres(embedded)  # second identical run

    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM corpus.chunks WHERE guidance_id = %s",
                (GUIDANCE_ID,),
            )
            count: int = cur.fetchone()[0]  # type: ignore[index]

    assert count == len(chunks), (
        f"Expected {len(chunks)} rows after two identical writes, got {count}"
    )
