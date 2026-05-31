"""Unit tests for rra.ingest.

External services (Voyage, Postgres, HTTP) are mocked — no live API calls.
"""
from __future__ import annotations

# Set required credentials before any rra imports trigger settings instantiation.
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from rra.ingest import (
    VOYAGE_MAX_BATCH,
    Chunk,
    DATA_DIR,
    DownloadedDoc,
    EmbeddedChunk,
    PDF_MIN_TEXT_LEN,
    _download_one,
    _embed_batch,
    _entries_from_manifest,
    chunk_text,
    download_guidances,
    embed_chunks,
    parse_pdf,
    write_to_postgres,
)

# ─── Shared fixtures ───────────────────────────────────────────────────────────

GUIDANCE_ID = "test-guidance-001"
GUIDANCE_TITLE = "Test FDA Guidance Document"

# Enough text to produce multiple chunks (~600 words → ~800 tokens → 2 chunks at 512/50)
LONG_TEXT = (
    "This FDA guidance document provides recommendations for software validation. "
    "Section 1: Introduction. The purpose of this document is to provide guidance "
    "on the general principles of software validation as they apply to the "
    "development, validation, and maintenance of software used in the production "
    "of FDA-regulated products. Software validation is part of the overall design "
    "validation for a finished device. Software validation activities occur in "
    "the context of a defined software development life cycle (SDLC) and problem "
    "resolution process. Section 2: Scope. This document applies to software that "
    "is used as a component or part of a medical device. Section 3: Background. "
    "FDA's regulation of software has been evolving for many years. The agency has "
    "developed guidance documents on software validation over the years. "
) * 12  # ~9 600 characters, comfortably > 512 tokens


@pytest.fixture
def sample_chunk() -> Chunk:
    return Chunk(
        guidance_id=GUIDANCE_ID,
        guidance_title=GUIDANCE_TITLE,
        chunk_index=0,
        text="FDA guidance text sample.",
        char_start=0,
        char_end=25,
        token_count=5,
    )


@pytest.fixture
def sample_embedded_chunk(sample_chunk: Chunk) -> EmbeddedChunk:
    return EmbeddedChunk(chunk=sample_chunk, embedding=[0.1] * 1024)


# ─── parse_pdf ─────────────────────────────────────────────────────────────────

class _MockPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _MockReader:
    def __init__(self, pages: list[_MockPage]) -> None:
        self._pages = pages

    @property
    def pages(self) -> list[_MockPage]:
        return self._pages


def test_parse_pdf_concatenates_pages(tmp_path: Path) -> None:
    """parse_pdf joins page texts with newlines."""
    dummy = tmp_path / "test.pdf"
    dummy.write_bytes(b"")  # path must exist for PdfReader mock

    mock_reader = _MockReader([_MockPage("Page one."), _MockPage("Page two.")])
    with patch("pypdf.PdfReader", return_value=mock_reader):
        result = parse_pdf(dummy)

    assert result == "Page one.\nPage two."


def test_parse_pdf_handles_empty_pages(tmp_path: Path) -> None:
    """extract_text returning None/empty string should not raise."""
    dummy = tmp_path / "empty.pdf"
    dummy.write_bytes(b"")

    class _EmptyPage:
        def extract_text(self) -> str | None:
            return None

    mock_reader = _MockReader([_EmptyPage()])  # type: ignore[list-item]
    with patch("pypdf.PdfReader", return_value=mock_reader):
        result = parse_pdf(dummy)

    assert result == ""


def test_parse_pdf_scanned_detection(tmp_path: Path) -> None:
    """Scanned PDFs (< PDF_MIN_TEXT_LEN chars) are detectable by the caller."""
    dummy = tmp_path / "scanned.pdf"
    dummy.write_bytes(b"")

    short_text = "x" * (PDF_MIN_TEXT_LEN - 1)
    mock_reader = _MockReader([_MockPage(short_text)])
    with patch("pypdf.PdfReader", return_value=mock_reader):
        result = parse_pdf(dummy)

    assert len(result) < PDF_MIN_TEXT_LEN


# ─── chunk_text ────────────────────────────────────────────────────────────────

def test_chunk_text_empty_returns_empty() -> None:
    assert chunk_text("", GUIDANCE_ID, GUIDANCE_TITLE) == []


def test_chunk_text_short_text_single_chunk() -> None:
    """Text shorter than chunk_size produces exactly one chunk."""
    text = "Short FDA guidance paragraph."
    chunks = chunk_text(text, GUIDANCE_ID, GUIDANCE_TITLE)

    assert len(chunks) == 1
    assert chunks[0].guidance_id == GUIDANCE_ID
    assert chunks[0].guidance_title == GUIDANCE_TITLE
    assert chunks[0].chunk_index == 0
    assert chunks[0].text == text
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].token_count > 0


def test_chunk_text_long_text_multiple_chunks() -> None:
    """Text longer than chunk_size produces multiple chunks."""
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert len(chunks) > 1


def test_chunk_text_indices_sequential() -> None:
    """chunk_index values form a 0-based sequence."""
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_chunk_text_overlap_exists() -> None:
    """Consecutive chunks share some text (the overlap window)."""
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert len(chunks) >= 2

    # The end of chunk N and start of chunk N+1 should share content.
    # Verify the char ranges overlap.
    for a, b in zip(chunks, chunks[1:]):
        assert b.char_start < a.char_end, (
            f"No overlap between chunk {a.chunk_index} and {b.chunk_index}"
        )


def test_chunk_text_guidance_id_propagated() -> None:
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert all(c.guidance_id == GUIDANCE_ID for c in chunks)


def test_chunk_text_guidance_title_propagated() -> None:
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert all(c.guidance_title == GUIDANCE_TITLE for c in chunks)


def test_chunk_text_token_counts_positive() -> None:
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    assert all(c.token_count > 0 for c in chunks)


def test_chunk_text_char_offsets_span_source() -> None:
    """char_start and char_end should decode back to the chunk's text."""
    chunks = chunk_text(LONG_TEXT, GUIDANCE_ID, GUIDANCE_TITLE)
    for chunk in chunks:
        extracted = LONG_TEXT[chunk.char_start : chunk.char_end]
        assert extracted == chunk.text, (
            f"Char offset mismatch at chunk {chunk.chunk_index}"
        )


# ─── embed_chunks ──────────────────────────────────────────────────────────────

def _make_chunks(n: int) -> list[Chunk]:
    return [
        Chunk(
            guidance_id=GUIDANCE_ID,
            guidance_title=GUIDANCE_TITLE,
            chunk_index=i,
            text=f"chunk text {i}",
            char_start=i * 20,
            char_end=i * 20 + 12,
            token_count=3,
        )
        for i in range(n)
    ]


def test_embed_chunks_returns_one_per_input(sample_chunk: Chunk) -> None:
    fake_emb = [0.0] * 1024
    with patch("rra.ingest._embed_batch", return_value=[fake_emb]) as mock_batch:
        result = embed_chunks([sample_chunk])

    assert len(result) == 1
    assert result[0].chunk is sample_chunk
    assert result[0].embedding == fake_emb
    mock_batch.assert_called_once_with([sample_chunk.text])


def test_embed_chunks_batches_at_voyage_max() -> None:
    """embed_chunks must never send more than VOYAGE_MAX_BATCH items per call."""
    n = VOYAGE_MAX_BATCH + 10
    chunks = _make_chunks(n)

    def fake_batch(texts: list[str]) -> list[list[float]]:
        assert len(texts) <= VOYAGE_MAX_BATCH
        return [[0.0] * 1024 for _ in texts]

    with patch("rra.ingest._embed_batch", side_effect=fake_batch):
        result = embed_chunks(chunks)

    assert len(result) == n


def test_embed_chunks_order_preserved() -> None:
    """EmbeddedChunk order must match input Chunk order."""
    chunks = _make_chunks(5)
    fake_embs = [[float(i)] * 1024 for i in range(5)]

    with patch("rra.ingest._embed_batch", return_value=fake_embs):
        result = embed_chunks(chunks)

    for i, ec in enumerate(result):
        assert ec.chunk.chunk_index == i
        assert ec.embedding[0] == float(i)


def test_embed_batch_calls_voyage(monkeypatch: pytest.MonkeyPatch) -> None:
    """_embed_batch passes texts and model to the Voyage client."""
    fake_response = MagicMock()
    fake_response.embeddings = [[0.5] * 1024, [0.6] * 1024]

    mock_client = MagicMock()
    mock_client.embed.return_value = fake_response

    with patch("voyageai.Client", return_value=mock_client):
        result = _embed_batch(["text a", "text b"])

    mock_client.embed.assert_called_once()
    call_kwargs: dict[str, Any] = mock_client.embed.call_args.kwargs
    assert call_kwargs.get("input_type") == "document"
    assert result == [[0.5] * 1024, [0.6] * 1024]


# ─── write_to_postgres ─────────────────────────────────────────────────────────

def test_write_to_postgres_empty_is_noop() -> None:
    """write_to_postgres with an empty list must not open a connection."""
    with patch("psycopg.connect") as mock_connect:
        write_to_postgres([])

    mock_connect.assert_not_called()


def test_write_to_postgres_calls_executemany(
    sample_embedded_chunk: EmbeddedChunk,
) -> None:
    """write_to_postgres calls executemany with one row per EmbeddedChunk."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("rra.ingest.register_vector"),
        patch("rra.ingest._ensure_schema"),
    ):
        write_to_postgres([sample_embedded_chunk])

    mock_cursor.executemany.assert_called_once()
    _sql, rows = mock_cursor.executemany.call_args.args
    assert len(rows) == 1
    row = rows[0]
    assert row["guidance_id"] == sample_embedded_chunk.chunk.guidance_id
    assert row["guidance_title"] == sample_embedded_chunk.chunk.guidance_title
    assert row["embedding"] == sample_embedded_chunk.embedding


def test_write_to_postgres_upsert_fields_present(
    sample_embedded_chunk: EmbeddedChunk,
) -> None:
    """Each row must contain all required DB columns."""
    required_keys = {
        "guidance_id",
        "guidance_title",
        "chunk_index",
        "text",
        "char_start",
        "char_end",
        "token_count",
        "embedding",
        "metadata",
    }

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("rra.ingest.register_vector"),
        patch("rra.ingest._ensure_schema"),
    ):
        write_to_postgres([sample_embedded_chunk])

    _sql, rows = mock_cursor.executemany.call_args.args
    assert required_keys == set(rows[0].keys())


def test_write_to_postgres_cluster_in_metadata() -> None:
    """Chunk.cluster lands as metadata->>'cluster' in the row dict passed to executemany."""
    from psycopg.types.json import Jsonb

    chunk = Chunk(
        guidance_id=GUIDANCE_ID,
        guidance_title=GUIDANCE_TITLE,
        chunk_index=0,
        text="text",
        char_start=0,
        char_end=4,
        token_count=1,
        cluster="software-samd-ai",
    )
    ec = EmbeddedChunk(chunk=chunk, embedding=[0.0] * 1024)

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("rra.ingest.register_vector"),
        patch("rra.ingest._ensure_schema"),
    ):
        write_to_postgres([ec])

    _sql, rows = mock_cursor.executemany.call_args.args
    meta = rows[0]["metadata"]
    assert isinstance(meta, Jsonb)
    assert meta.obj == {"cluster": "software-samd-ai"}


def test_write_to_postgres_no_cluster_metadata_null() -> None:
    """Chunk with no cluster produces metadata with cluster=None (JSON null)."""
    from psycopg.types.json import Jsonb

    chunk = Chunk(
        guidance_id=GUIDANCE_ID,
        guidance_title=GUIDANCE_TITLE,
        chunk_index=0,
        text="text",
        char_start=0,
        char_end=4,
        token_count=1,
    )
    ec = EmbeddedChunk(chunk=chunk, embedding=[0.0] * 1024)

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with (
        patch("psycopg.connect", return_value=mock_conn),
        patch("rra.ingest.register_vector"),
        patch("rra.ingest._ensure_schema"),
    ):
        write_to_postgres([ec])

    _sql, rows = mock_cursor.executemany.call_args.args
    meta = rows[0]["metadata"]
    assert isinstance(meta, Jsonb)
    assert meta.obj == {"cluster": None}


# ─── _entries_from_manifest ────────────────────────────────────────────────────

def test_entries_from_manifest_loads_and_filters(tmp_path: Path) -> None:
    """Entries are read from manifest.json; verification.ok=False entries skipped."""
    manifest = [
        {"id": "1", "title": "A", "url": "https://example.com/1", "verification": {"ok": True}},
        {"id": "2", "title": "B", "url": "https://example.com/2", "verification": {"ok": False}},
        {"id": "3", "title": "C", "url": "https://example.com/3"},
    ]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    with patch("rra.ingest.DATA_DIR", tmp_path):
        entries = _entries_from_manifest()

    assert len(entries) == 2
    ids = [e["id"] for e in entries]
    assert "1" in ids   # verification.ok=True → included
    assert "2" not in ids  # verification.ok=False → skipped
    assert "3" in ids   # no verification field → included


# ─── _download_one retry policy ────────────────────────────────────────────────

def test_download_one_no_retry_on_404(tmp_path: Path) -> None:
    """A 404 response must not trigger any retries — raises after one attempt."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with pytest.raises(httpx.HTTPStatusError):
        _download_one(mock_client, "https://www.fda.gov/media/99999/download", "99999", tmp_path)

    assert mock_client.get.call_count == 1


# ─── download_guidances fault tolerance ───────────────────────────────────────

def test_download_guidances_continues_on_failure(tmp_path: Path) -> None:
    """Batch completes and returns only successful DownloadedDocs when one raises."""
    manifest_entries = [
        {"id": "1", "title": "Doc One", "url": "https://www.fda.gov/media/1/download"},
        {"id": "2", "title": "Doc Two", "url": "https://www.fda.gov/media/2/download"},
        {"id": "3", "title": "Doc Three", "url": "https://www.fda.gov/media/3/download"},
    ]

    def fake_download(client: Any, url: str, guidance_id: str, dest_dir: Path) -> Path:
        if guidance_id == "2":
            raise httpx.ConnectError("connection refused")
        p = dest_dir / f"{guidance_id}.pdf"
        p.write_bytes(b"%PDF fake")
        return p

    with (
        patch("rra.ingest.DATA_DIR", tmp_path),
        patch("rra.ingest._entries_from_manifest", return_value=manifest_entries),
        patch("rra.ingest._download_one", side_effect=fake_download),
    ):
        docs = download_guidances()

    assert len(docs) == 2
    assert all(doc.path.exists() for doc in docs)
    assert {d.guidance_id for d in docs} == {"1", "3"}
    assert {d.guidance_title for d in docs} == {"Doc One", "Doc Three"}
