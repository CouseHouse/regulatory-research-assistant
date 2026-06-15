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

import re

import httpx
import psycopg
import pypdf
import structlog
import tiktoken
from psycopg.types.json import Jsonb
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from rra.config import PROJECT_ROOT, settings
from rra.ports.embeddings import get_embeddings
from rra.ports.vectorstore import get_vector_store
from rra.rate_limit import RateLimiter

log = structlog.get_logger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────────

# Note: VOYAGE_MAX_BATCH has moved to rra.adapters.voyage_embeddings — it is a
# provider limit and belongs with the adapter.  Ingest now delegates batching
# to the embeddings port, which handles batching internally.
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


# ─── Clean ─────────────────────────────────────────────────────────────────────

# FDA running header spliced mid-sentence at page breaks by pypdf's page join.
_NONBINDING_RE = re.compile(
    r"Contains?\s+Nonbinding\s+Recommendations?\.?",
    re.IGNORECASE,
)
# Companion header present in draft docs.
_DRAFT_HEADER_RE = re.compile(
    r"Draft\s*[–—-]\s*Not\s+for\s+Implementation\.?",
    re.IGNORECASE,
)
# Page-number footers / running headers on their own line.
_PAGE_RE = re.compile(
    r"\n[ \t]*(?:Page\s+\d+\s+of\s+\d+|\d+\s+of\s+\d+)[ \t]*(?=\n|$)",
    re.IGNORECASE,
)
# Hyphenated word split across a line break (e.g. "manufac-\nturer").
_HYPHEN_NL_RE = re.compile(r"-\n(?=[a-z])")
# Mid-sentence single newline: lower/punct before \n, word-char after.
# Preserves \n\n (paragraph breaks) and newlines after sentence-terminal . ! ?
_MID_SENT_NL_RE = re.compile(r"(?<=[a-z,;:(])\n(?=[a-zA-Z0-9(])")


def clean_text(raw: str) -> str:
    """Strip FDA boilerplate and repair PDF-embedded newlines pre-chunk.

    Applied once before chunk_text_structural (ADR 0014, plan §0.3):
    char_start/char_end offsets in the output are self-consistent with the
    text this function returns because chunking happens downstream.
    """
    text = _NONBINDING_RE.sub(" ", raw)
    text = _DRAFT_HEADER_RE.sub(" ", text)
    text = _PAGE_RE.sub("", text)
    text = _HYPHEN_NL_RE.sub("", text)
    text = _MID_SENT_NL_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ─── Chunk ─────────────────────────────────────────────────────────────────────

# Paragraph separator (two or more newlines).
_PARA_SPLIT = re.compile(r"\n\n+")
# Sentence boundary: terminal punctuation + whitespace + sentence-starting char.
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9("])')


def _structural_segments(text: str, enc: tiktoken.Encoding) -> list[tuple[int, int]]:
    """Return non-overlapping (char_start, char_end) segments from cleaned text.

    Splits on paragraph boundaries first; oversized paragraphs fall back to
    sentence boundaries. Covers the full character range of `text`.
    """
    size = settings.chunk_size_tokens

    para_spans: list[tuple[int, int]] = []
    prev = 0
    for m in _PARA_SPLIT.finditer(text):
        if m.start() > prev:
            para_spans.append((prev, m.start()))
        prev = m.end()
    if prev < len(text):
        para_spans.append((prev, len(text)))

    segments: list[tuple[int, int]] = []
    for cs, ce in para_spans:
        para = text[cs:ce]
        if not para.strip():
            continue
        if len(enc.encode(para)) <= size:
            segments.append((cs, ce))
        else:
            sent_prev = 0
            for sm in _SENT_SPLIT.finditer(para):
                if sm.start() > sent_prev:
                    segments.append((cs + sent_prev, cs + sm.start()))
                sent_prev = sm.end()
            if sent_prev < len(para):
                segments.append((cs + sent_prev, ce))

    return segments


def chunk_text_structural(
    text: str,
    guidance_id: str,
    guidance_title: str,
    cluster: str | None = None,
) -> list[Chunk]:
    """Structural chunker: paragraph → sentence packing under a token budget.

    Replaces the fixed tiktoken window (ADR 0014). Splits on paragraph (\\n\\n)
    then sentence boundaries; packs segments greedily to
    settings.chunk_size_tokens with settings.chunk_overlap_tokens of soft
    overlap at segment boundaries.

    char_start / char_end are character offsets into the `text` argument (the
    cleaned doc text); they remain self-consistent with the stored chunk text
    by construction — chunking happens after cleaning (plan §0.3).
    """
    enc = tiktoken.get_encoding("cl100k_base")
    size = settings.chunk_size_tokens
    overlap = settings.chunk_overlap_tokens

    if not text.strip():
        return []

    segs = _structural_segments(text, enc)
    if not segs:
        return []

    seg_toks: list[int] = [len(enc.encode(text[cs:ce])) for cs, ce in segs]

    result: list[Chunk] = []
    i = 0
    n = len(segs)

    while i < n:
        # Greedily pack segments starting at segs[i].
        total = 0
        j = i
        while j < n:
            if total + seg_toks[j] > size and j > i:
                break
            total += seg_toks[j]
            j += 1

        # Chunk spans segs[i..j); char range covers the full text slice
        # including inter-segment whitespace between packed segments.
        cs = segs[i][0]
        ce = segs[j - 1][1]
        chunk_str = text[cs:ce]

        result.append(
            Chunk(
                guidance_id=guidance_id,
                guidance_title=guidance_title,
                chunk_index=len(result),
                text=chunk_str,
                char_start=cs,
                char_end=ce,
                token_count=len(enc.encode(chunk_str)),
                cluster=cluster,
            )
        )

        if j >= n:
            break

        # Soft overlap: re-include tail segments totalling ≈ overlap tokens.
        overlap_acc = 0
        k = j
        while k > i + 1 and overlap_acc + seg_toks[k - 1] <= overlap:
            overlap_acc += seg_toks[k - 1]
            k -= 1
        i = k

    return result


def chunk_text(
    text: str,
    guidance_id: str,
    guidance_title: str,
    cluster: str | None = None,
) -> list[Chunk]:
    """Fixed-size tiktoken sliding window (retained for reference / tests).

    Superseded in the live pipeline by chunk_text_structural (ADR 0014).
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
    """Embed *chunks* via the embeddings port.

    Batching, retry logic, and the VOYAGE_MAX_BATCH limit have moved into the
    VoyageEmbeddingsAdapter (rra.adapters.voyage_embeddings).  The public
    signature of embed_chunks is UNCHANGED so callers and tests are unaffected.
    """
    if not chunks:
        return []

    texts = [c.text for c in chunks]
    embeddings = get_embeddings().embed_documents(texts)

    if len(embeddings) != len(chunks):
        raise RuntimeError(
            f"Embeddings adapter returned {len(embeddings)} embeddings for {len(chunks)} inputs"
        )

    return [
        EmbeddedChunk(chunk=chunk, embedding=emb)
        for chunk, emb in zip(chunks, embeddings)
    ]


# ─── Write ─────────────────────────────────────────────────────────────────────

_SCRATCH_TABLE: Final[str] = "corpus.chunks_rechunk"

_SCRATCH_DDL: Final[str] = f"""
CREATE SCHEMA IF NOT EXISTS corpus;
CREATE TABLE IF NOT EXISTS {_SCRATCH_TABLE} (
    id              bigserial    PRIMARY KEY,
    guidance_id     text         NOT NULL,
    guidance_title  text         NOT NULL,
    chunk_index     int          NOT NULL,
    text            text         NOT NULL,
    char_start      int          NOT NULL,
    char_end        int          NOT NULL,
    token_count     int          NOT NULL,
    metadata        jsonb        DEFAULT '{{}}'::jsonb,
    UNIQUE (guidance_id, chunk_index)
);
"""

_SCRATCH_UPSERT: Final[str] = f"""
INSERT INTO {_SCRATCH_TABLE}
    (guidance_id, guidance_title, chunk_index, text, char_start, char_end, token_count, metadata)
VALUES
    (%(guidance_id)s, %(guidance_title)s, %(chunk_index)s, %(text)s,
     %(char_start)s, %(char_end)s, %(token_count)s, %(metadata)s)
ON CONFLICT (guidance_id, chunk_index) DO UPDATE SET
    guidance_title = EXCLUDED.guidance_title,
    text           = EXCLUDED.text,
    char_start     = EXCLUDED.char_start,
    char_end       = EXCLUDED.char_end,
    token_count    = EXCLUDED.token_count,
    metadata       = EXCLUDED.metadata
"""


def write_to_scratch(chunks: list[Chunk]) -> None:
    """Write clean+rechunked text to corpus.chunks_rechunk — no embeddings.

    Idempotent: safe to re-run as cleaning/chunking parameters are tuned.
    Live corpus.chunks is untouched throughout (ADR 0014 text-only validation
    path). Schema is created on first call.
    """
    if not chunks:
        return

    with psycopg.connect(settings.pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_SCRATCH_DDL)

        rows: list[dict[str, Any]] = [
            {
                "guidance_id": c.guidance_id,
                "guidance_title": c.guidance_title,
                "chunk_index": c.chunk_index,
                "text": c.text,
                "char_start": c.char_start,
                "char_end": c.char_end,
                "token_count": c.token_count,
                "metadata": Jsonb({"cluster": c.cluster}),
            }
            for c in chunks
        ]
        with conn.cursor() as cur:
            cur.executemany(_SCRATCH_UPSERT, rows)

    log.info(
        "corpus.scratch.done",
        guidance_id=chunks[0].guidance_id,
        n_chunks=len(chunks),
    )


def write_to_postgres(chunks: list[EmbeddedChunk]) -> None:
    """Write (or upsert) *chunks* into corpus.chunks via the vector store port.

    All rows for the batch are written in a single transaction.
    The schema is created idempotently at the start of each call so the
    pipeline is self-bootstrapping on a fresh database.
    """
    if not chunks:
        return

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
    get_vector_store().upsert_chunks(rows)

    log.info(
        "corpus.write.done",
        guidance_id=chunks[0].chunk.guidance_id,
        n_chunks=len(chunks),
    )


# ─── Entry point ───────────────────────────────────────────────────────────────

def rechunk_to_scratch(limit: int | None = None) -> int:
    """Clean + structurally rechunk all PDFs into corpus.chunks_rechunk.

    Text + offsets only — no embeddings. Safe to re-run; idempotent via ON
    CONFLICT DO UPDATE. Live corpus.chunks is untouched (ADR 0014).
    """
    limiter = RateLimiter(
        rate_per_second=settings.download_rate_per_second,
        burst=settings.download_burst,
    )
    docs = download_guidances(limit, limiter)
    if not docs:
        log.error("corpus.rechunk_scratch.no_files")
        return 1

    ingested = 0
    failures = 0
    for doc in docs:
        try:
            raw = parse_pdf(doc.path)
            if len(raw) < PDF_MIN_TEXT_LEN:
                log.warning(
                    "corpus.rechunk_scratch.scanned_pdf_skipped",
                    filename=doc.path.name,
                )
                continue
            text = clean_text(raw)
            chunks = chunk_text_structural(
                text, doc.guidance_id, doc.guidance_title, doc.cluster
            )
            write_to_scratch(chunks)
            ingested += 1
        except Exception:
            log.error(
                "corpus.rechunk_scratch.doc_failed",
                path=str(doc.path),
                guidance_id=doc.guidance_id,
            )
            failures += 1

    log.info(
        "corpus.rechunk_scratch.complete",
        ingested=ingested,
        failed=failures,
        total=len(docs),
    )
    return 0 if ingested > 0 else 1


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
    parser.add_argument(
        "--rechunk-scratch",
        action="store_true",
        help=(
            "Build corpus.chunks_rechunk (text + offsets, no embeddings) via "
            "clean_text + chunk_text_structural. Does NOT touch corpus.chunks. "
            "Run before smoke_rechunk.py to validate the rechunk strategy."
        ),
    )
    args = parser.parse_args()

    if args.rechunk_scratch:
        return rechunk_to_scratch(args.limit)

    # Bootstrap schema — and optionally truncate — before any download or
    # embedding work. This surfaces schema problems immediately rather than
    # after expensive Voyage API calls have already been made.
    get_vector_store().ensure_schema()
    if args.truncate:
        with psycopg.connect(settings.pg_dsn) as conn:
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

            chunks = chunk_text_structural(
                clean_text(text), doc.guidance_id, doc.guidance_title, doc.cluster
            )
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
