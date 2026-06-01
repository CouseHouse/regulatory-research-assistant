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
import voyageai.error as _voyageai_error
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from rra.config import PROJECT_ROOT, settings
from rra.rate_limit import RateLimiter

log = structlog.get_logger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────────

VOYAGE_MAX_BATCH: Final[int] = 128
PDF_MIN_TEXT_LEN: Final[int] = 500  # texts shorter than this are likely scanned images

DATA_DIR: Final[Path] = PROJECT_ROOT / "data" / "corpus"


# ─── Data carriers ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Chunk:
    """A single text chunk produced by the chunking stage."""

    guidance_id: str        # stable doc ID; equals the manifest `id` field
    guidance_title: str     # human-readable title from the manifest
    chunk_index: int        # 0-based ordinal; ORDER BY chunk_index reconstructs the doc
    text: str               # chunk body
    char_start: int         # inclusive offset into the parsed full-text string
    char_end: int           # exclusive offset into the parsed full-text string
    token_count: int        # tiktoken cl100k_base count (for cost reporting)
    cluster: str | None = None  # manifest cluster label; stored in metadata JSONB


@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    """A Chunk paired with its Voyage embedding."""

    chunk: Chunk
    embedding: list[float]  # length == settings.embedding_dim (1024)


@dataclass(frozen=True, slots=True)
class DownloadedDoc:
    """Result of a successful _download_one call."""

    path: Path
    guidance_id: str
    guidance_title: str
    cluster: str | None = None  # propagated from manifest; None for legacy manifests


# ─── Download ──────────────────────────────────────────────────────────────────

def _entries_from_manifest() -> list[dict[str, Any]]:
    """Load entries from data/corpus/manifest.json.

    Skips entries where verification.ok is explicitly False.
    """
    entries: list[dict[str, Any]] = json.loads(
        (DATA_DIR / "manifest.json").read_text()
    )
    return [
        e
        for e in entries
        if not (e.get("verification", {}).get("ok") is False)
    ]


def download_guidances(
    limit: int | None = None,
    limiter: RateLimiter | None = None,
) -> list[DownloadedDoc]:
    """Download up to *limit* FDA guidance PDFs into DATA_DIR.

    Already-cached files are skipped. If one document fails after retries,
    the failure is logged and the rest of the batch continues. Returns only
    successful docs.

    *limiter* throttles every download attempt (not manifest loading or
    embedding calls). Defaults to a fresh limiter at the rate configured
    in settings.
    """
    _limiter: RateLimiter = limiter if limiter is not None else RateLimiter(
        rate_per_second=settings.download_rate_per_second,
        burst=settings.download_burst,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entries = _entries_from_manifest()
    if limit is not None:
        entries = entries[:limit]

    docs: list[DownloadedDoc] = []
    failures: list[str] = []
    with httpx.Client(
        timeout=60.0,
        follow_redirects=True,
        headers={
            "User-Agent": (
                "rra-ingest/0.3 (portfolio project; "
                "github.com/CouseHouse/regulatory-research-assistant)"
            )
        },
    ) as client:
        for entry in entries:
            url: str = entry["url"]
            guidance_id: str = entry["id"]
            guidance_title: str = entry["title"]
            try:
                _limiter.acquire()
                path = _download_one(client, url, guidance_id, DATA_DIR)
                if path is not None:
                    docs.append(
                        DownloadedDoc(
                            path=path,
                            guidance_id=guidance_id,
                            guidance_title=guidance_title,
                            cluster=entry.get("cluster"),
                        )
                    )
            except Exception:
                log.error("corpus.download.failed", url=url, guidance_id=guidance_id)
                failures.append(url)

    log.info(
        "corpus.download.summary",
        succeeded=len(docs),
        failed=len(failures),
    )
    s = _limiter.stats
    log.info(
        "corpus.download.rate_limit_stats",
        requests=s.requests_made,
        total_wait_seconds=round(s.total_wait_seconds, 2),
        longest_wait_seconds=round(s.longest_wait_seconds, 3),
    )
    return docs


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
def _download_one(
    client: httpx.Client, url: str, guidance_id: str, dest_dir: Path
) -> Path | None:
    """Download a single PDF; return the local path or None on permanent failure."""
    dest = dest_dir / f"{guidance_id}.pdf"

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

def chunk_text(
    text: str,
    guidance_id: str,
    guidance_title: str,
    cluster: str | None = None,
) -> list[Chunk]:
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
                guidance_title=guidance_title,
                chunk_index=len(result),
                text=chunk_str,
                char_start=char_start,
                char_end=char_end,
                token_count=end - start,
                cluster=cluster,
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
            raise RuntimeError(
                f"Voyage returned {len(embeddings)} embeddings for {len(batch)} inputs"
            )
        for chunk, emb in zip(batch, embeddings):
            result.append(EmbeddedChunk(chunk=chunk, embedding=emb))

    return result


def _is_embed_retryable(exc: BaseException) -> bool:
    # Retry on transient Voyage errors: rate limits, server errors, timeouts,
    # connection issues. AuthenticationError and InvalidRequestError are
    # permanent — retrying wastes up to 4 minutes and still fails.
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


@retry(
    retry=retry_if_exception(_is_embed_retryable),
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
                "guidance_title": ec.chunk.guidance_title,
                "chunk_index": ec.chunk.chunk_index,
                "text": ec.chunk.text,
                "char_start": ec.chunk.char_start,
                "char_end": ec.chunk.char_end,
                "token_count": ec.chunk.token_count,
                "embedding": ec.embedding,
                "metadata": Jsonb({"cluster": ec.chunk.cluster}),
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
    """Create the corpus schema and chunks table if they don't exist.

    DDL mirrors init-db/01-init.sql exactly — update both together.
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
    parser.add_argument(
        "--truncate",
        action="store_true",
        help=(
            "Truncate corpus.chunks before ingesting. "
            "Use after schema changes to start with a clean slate."
        ),
    )
    args = parser.parse_args()

    # Bootstrap schema — and optionally truncate — before any download or
    # embedding work. This surfaces schema problems immediately rather than
    # after expensive Voyage API calls have already been made.
    with psycopg.connect(settings.pg_dsn) as conn:
        _ensure_schema(conn)
        if args.truncate:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE corpus.chunks RESTART IDENTITY")
            log.info("corpus.truncated")

    limiter = RateLimiter(
        rate_per_second=settings.download_rate_per_second,
        burst=settings.download_burst,
    )
    docs = download_guidances(args.limit, limiter)
    if not docs:
        log.error("corpus.ingest.no_files_downloaded")
        return 1

    ingested = 0
    ingest_failures = 0
    for doc in docs:
        try:
            text = parse_pdf(doc.path)

            if len(text) < PDF_MIN_TEXT_LEN:
                log.warning(
                    "corpus.parse.scanned_pdf_skipped",
                    filename=doc.path.name,
                    text_len=len(text),
                )
                continue

            chunks = chunk_text(text, doc.guidance_id, doc.guidance_title, doc.cluster)
            embedded = embed_chunks(chunks)
            write_to_postgres(embedded)
            ingested += 1
        except Exception:
            log.error(
                "corpus.ingest.doc_failed",
                path=str(doc.path),
                guidance_id=doc.guidance_id,
            )
            ingest_failures += 1

    log.info(
        "corpus.ingest.complete",
        ingested=ingested,
        failed=ingest_failures,
        total=len(docs),
    )
    return 0 if ingested > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
