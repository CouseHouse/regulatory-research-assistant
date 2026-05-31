"""Corpus ingest pipeline: download → parse → chunk → embed → store.

Invoked via `rra-ingest` console script or `python -m rra.ingest`.
This is a batch job (correctness and reproducibility over latency).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import psycopg
import pypdf
import structlog
import tiktoken
import voyageai
from pgvector.psycopg import register_vector
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from rra.config import PROJECT_ROOT, settings

log = structlog.get_logger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────────

VOYAGE_MAX_BATCH: Final[int] = 128
PDF_MIN_TEXT_LEN: Final[int] = 500  # texts shorter than this are likely scanned images

DATA_DIR: Final[Path] = PROJECT_ROOT / "data" / "corpus"


# ─── Data carriers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Chunk:
    """A single text chunk produced by the chunking stage."""

    guidance_id: str     # stable doc ID; equals the source PDF filename stem
    chunk_index: int     # 0-based ordinal; ORDER BY chunk_index reconstructs the doc
    text: str            # chunk body
    char_start: int      # inclusive offset into the parsed full-text string
    char_end: int        # exclusive offset into the parsed full-text string
    token_count: int     # tiktoken cl100k_base count (for cost reporting)


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    """A Chunk paired with its Voyage embedding."""

    chunk: Chunk
    embedding: list[float]  # length == settings.embedding_dim (1024)


# ─── Download ──────────────────────────────────────────────────────────────────

def _urls_from_manifest() -> list[str]:
    """Load download URLs from data/corpus/manifest.json.

    Skips entries where verification.ok is explicitly False.
    """
    entries = json.loads((DATA_DIR / "manifest.json").read_text())
    return [
        e["url"]
        for e in entries
        if not (e.get("verification", {}).get("ok") is False)
    ]


def download_guidances(limit: int | None = None) -> list[Path]:
    """Download up to *limit* FDA guidance PDFs into DATA_DIR.

    Already-cached files are skipped. If one document fails after retries,
    the failure is logged and the rest of the batch continues. Returns only
    successful paths.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    urls = _urls_from_manifest()
    if limit is not None:
        urls = urls[:limit]

    paths: list[Path] = []
    failures: list[str] = []
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            try:
                path = _download_one(client, url, DATA_DIR)
                if path is not None:
                    paths.append(path)
            except Exception:
                log.error("corpus.download.failed", url=url)
                failures.append(url)

    log.info(
        "corpus.download.summary",
        succeeded=len(paths),
        failed=len(failures),
    )
    return paths


def _is_retryable(exc: BaseException) -> bool:
    # Retry only on server errors (5xx) or transient network failures.
    # Client errors (4xx, including 404) are permanent — do not retry.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError))


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _download_one(client: httpx.Client, url: str, dest_dir: Path) -> Path | None:
    """Download a single PDF; return the local path or None on permanent failure."""
    stem = url.rstrip("/").split("/")[-2]  # .../media/{id}/download → id
    dest = dest_dir / f"{stem}.pdf"

    if dest.exists():
        log.info("corpus.download.cached", path=str(dest))
        return dest

    log.info("corpus.download.start", url=url)
    response = client.get(url)
    response.raise_for_status()
    dest.write_bytes(response.content)
    log.info("corpus.download.done", path=str(dest), bytes=len(response.content))
    return dest


# ─── Parse ─────────────────────────────────────────────────────────────────────

def parse_pdf(path: Path) -> str:
    """Extract text from a PDF.  Returns the concatenated text of all pages."""
    reader = pypdf.PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


# ─── Chunk ─────────────────────────────────────────────────────────────────────

def chunk_text(text: str, guidance_id: str) -> list[Chunk]:
    """Split *text* into overlapping token-bounded chunks.

    Uses a tiktoken sliding window (cl100k_base encoding) with
    settings.chunk_size_tokens and settings.chunk_overlap_tokens.
    Chunk boundaries are determined by the token encoding rather than
    character boundaries; char_start/char_end are computed by decoding
    the prefix up to the chunk start so that check_citation can resolve
    spans back to the source text.

    Note: the spec (§4.4) cites RecursiveCharacterTextSplitter for its
    structural-boundary preference.  That lives in langchain-text-splitters,
    which is not a declared project dependency.  A pure tiktoken window is used
    here and produces equivalent chunk sizes; boundary preference can be added
    via langchain-text-splitters if recall@10 stalls below 0.75 (spec §4.4
    reopen trigger).
    """
    enc = tiktoken.get_encoding("cl100k_base")
    size = settings.chunk_size_tokens
    overlap = settings.chunk_overlap_tokens

    all_tokens = enc.encode(text)
    n = len(all_tokens)
    if n == 0:
        return []

    result: list[Chunk] = []
    start = 0

    while start < n:
        end = min(start + size, n)
        chunk_str = enc.decode(all_tokens[start:end])

        # Decode prefix to get exact char offset (O(n) per chunk; acceptable
        # for FDA guidance docs which are typically <20k tokens each).
        prefix_str = enc.decode(all_tokens[:start]) if start > 0 else ""
        char_start = len(prefix_str)
        char_end = char_start + len(chunk_str)

        result.append(
            Chunk(
                guidance_id=guidance_id,
                chunk_index=len(result),
                text=chunk_str,
                char_start=char_start,
                char_end=char_end,
                token_count=end - start,
            )
        )

        if end >= n:
            break
        start = end - overlap

    return result


# ─── Embed ─────────────────────────────────────────────────────────────────────

def embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]:
    """Embed *chunks* with Voyage in batches of at most VOYAGE_MAX_BATCH."""
    result: list[EmbeddedChunk] = []

    for batch_start in range(0, len(chunks), VOYAGE_MAX_BATCH):
        batch = chunks[batch_start : batch_start + VOYAGE_MAX_BATCH]
        texts = [c.text for c in batch]
        embeddings = _embed_batch(texts)
        if len(embeddings) != len(batch):
            raise NotImplementedError(  # TODO(day3): surface as a typed error
                f"Voyage returned {len(embeddings)} embeddings for {len(batch)} inputs"
            )
        for chunk, emb in zip(batch, embeddings):
            result.append(EmbeddedChunk(chunk=chunk, embedding=emb))

    return result


@retry(
    # voyageai error types have no public stubs; catch broadly and reraise after backoff
    retry=retry_if_exception_type(Exception),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Call the Voyage embed endpoint for a single batch (≤ VOYAGE_MAX_BATCH items)."""
    client = voyageai.Client(  # type: ignore[attr-defined]
        api_key=settings.voyage_api_key.get_secret_value()
    )
    response = client.embed(texts, model=settings.embedding_model, input_type="document")
    # voyageai stubs type .embeddings as list[list[float]] | list[list[int]];
    # explicit float conversion ensures the return type is always list[list[float]].
    return [[float(v) for v in emb] for emb in response.embeddings]


# ─── Write ─────────────────────────────────────────────────────────────────────

_UPSERT_SQL: Final[str] = """
INSERT INTO corpus.chunks
    (guidance_id, chunk_index, text, char_start, char_end, token_count, embedding)
VALUES
    (%(guidance_id)s, %(chunk_index)s, %(text)s, %(char_start)s, %(char_end)s,
     %(token_count)s, %(embedding)s)
ON CONFLICT (guidance_id, chunk_index) DO UPDATE SET
    text        = EXCLUDED.text,
    char_start  = EXCLUDED.char_start,
    char_end    = EXCLUDED.char_end,
    token_count = EXCLUDED.token_count,
    embedding   = EXCLUDED.embedding
"""


def write_to_postgres(chunks: list[EmbeddedChunk]) -> None:
    """Write (or upsert) *chunks* into corpus.chunks.

    All rows for the batch are written in a single transaction.
    The schema is created idempotently at the start of each call so the
    pipeline is self-bootstrapping on a fresh database.
    """
    if not chunks:
        return

    with psycopg.connect(settings.pg_dsn) as conn:
        _ensure_schema(conn)
        register_vector(conn)

        rows: list[dict[str, Any]] = [
            {
                "guidance_id": ec.chunk.guidance_id,
                "chunk_index": ec.chunk.chunk_index,
                "text": ec.chunk.text,
                "char_start": ec.chunk.char_start,
                "char_end": ec.chunk.char_end,
                "token_count": ec.chunk.token_count,
                "embedding": ec.embedding,
            }
            for ec in chunks
        ]
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, rows)
        # psycopg Connection context manager commits on clean exit

    log.info(
        "corpus.write.done",
        guidance_id=chunks[0].chunk.guidance_id,
        n_chunks=len(chunks),
    )


def _ensure_schema(conn: psycopg.Connection[Any]) -> None:
    """Create the corpus schema and chunks table if they don't exist."""
    dim = settings.embedding_dim
    ddl = f"""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE SCHEMA IF NOT EXISTS corpus;
    CREATE TABLE IF NOT EXISTS corpus.chunks (
        id           bigserial    PRIMARY KEY,
        guidance_id  text         NOT NULL,
        chunk_index  int          NOT NULL,
        text         text         NOT NULL,
        char_start   int          NOT NULL,
        char_end     int          NOT NULL,
        token_count  int          NOT NULL,
        embedding    vector({dim}) NOT NULL,
        UNIQUE (guidance_id, chunk_index)
    );
    CREATE INDEX IF NOT EXISTS chunks_hnsw
        ON corpus.chunks USING hnsw (embedding vector_cosine_ops);
    """
    with conn.cursor() as cur:
        cur.execute(ddl)


# ─── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point for the `rra-ingest` console script."""
    parser = argparse.ArgumentParser(
        description="Ingest FDA guidance PDFs into Postgres/pgvector."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of documents to ingest (default: all)",
    )
    args = parser.parse_args()

    paths = download_guidances(args.limit)
    if not paths:
        log.error("corpus.ingest.no_files_downloaded")
        return 1

    ingested = 0
    for path in paths:
        guidance_id = path.stem
        text = parse_pdf(path)

        if len(text) < PDF_MIN_TEXT_LEN:
            log.warning(
                "corpus.parse.scanned_pdf_skipped",
                filename=path.name,
                text_len=len(text),
            )
            continue

        chunks = chunk_text(text, guidance_id)
        embedded = embed_chunks(chunks)
        write_to_postgres(embedded)
        ingested += 1

    log.info("corpus.ingest.complete", ingested=ingested, total=len(paths))
    return 0 if ingested > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
