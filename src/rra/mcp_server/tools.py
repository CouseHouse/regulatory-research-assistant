"""MCP tool functions as plain importable Python (ADR 0011).

All four tool functions live here. src/rra/mcp_server/server.py registers them
as MCP handlers for external clients (Claude Desktop). Agents import and call
them in-process — no subprocess, no transport.

check_citation implements the ADR-0010 three-step matching contract:
  1. whitespace-normalize both sides
  2. normalized substring check + whitespace-flexible regex for stored-text span
  3. SequenceMatcher COVERAGE ratio (longest_match.size / len(norm_quoted))
     — NOT SequenceMatcher.ratio(), whose denominator is dominated by chunk
       length and returns ~0.05 even on a perfect short-quote match.
"""
from __future__ import annotations

import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Literal

import structlog
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from rra.config import settings
from rra.db import get_conn
from rra.schemas import RetrievedPassage

log = structlog.get_logger(__name__)


# ─── Shared error model ───────────────────────────────────────────────────────


class ToolError(Exception):
    """Raised by tool functions on infrastructure failures.

    retryable=True  → transient (DB timeout, Voyage API down); critic treats
                       as inconclusive — does NOT use as evidence for revise.
    retryable=False → permanent (key absent, malformed input); critic treats
                       same as verified=False.
    """

    def __init__(
        self,
        code: Literal["NOT_FOUND", "INVALID_INPUT", "DB_ERROR", "EMBEDDING_ERROR", "UNKNOWN"],
        message: str,
        tool: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.tool = tool
        self.retryable = retryable


# ─── search_corpus ────────────────────────────────────────────────────────────


class SearchFilters(BaseModel):
    guidance_ids: list[str] | None = None


class SearchCorpusInput(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=50)
    filters: SearchFilters | None = None


class SearchCorpusResult(BaseModel):
    passages: list[RetrievedPassage]


def search_corpus(
    query: str,
    k: int = 5,
    filters: SearchFilters | None = None,
) -> SearchCorpusResult:
    """Retrieve top-k passages from the corpus via vector search + rerank.

    Thin wrapper over rra.retrieval.search_corpus with Pydantic validation
    and a consistent ToolError surface. Returns SearchCorpusResult so callers
    can unwrap .passages or pass the full result.
    """
    from rra.retrieval import search_corpus as _search_corpus  # avoid circular at module load

    filter_dict: dict[str, Any] | None = None
    if filters is not None and filters.guidance_ids:
        filter_dict = {"guidance_ids": filters.guidance_ids}

    try:
        passages = _search_corpus(query=query, k=k, filters=filter_dict)
    except Exception as exc:
        # Distinguish embedding (Voyage) failures from DB failures by message heuristics.
        # Both are transient and retryable.
        msg = str(exc).lower()
        if "voyage" in msg or "embed" in msg:
            raise ToolError(
                code="EMBEDDING_ERROR",
                message=str(exc),
                tool="search_corpus",
                retryable=True,
            ) from exc
        raise ToolError(
            code="DB_ERROR",
            message=str(exc),
            tool="search_corpus",
            retryable=True,
        ) from exc

    return SearchCorpusResult(passages=passages)


# ─── fetch_guidance ───────────────────────────────────────────────────────────


class FetchGuidanceInput(BaseModel):
    guidance_id: str


class FetchGuidanceResult(BaseModel):
    guidance_id: str
    guidance_title: str
    text: str
    chunk_count: int


def fetch_guidance(guidance_id: str) -> FetchGuidanceResult:
    """Retrieve full text of an FDA guidance document, assembled from stored chunks.

    Text is returned RAW — no cleaning applied. PDF artifacts (embedded newlines,
    boilerplate headers) are present by design; cleaning is deferred to Day 7 ingest.
    Returning raw text preserves the dirty baseline that Day 6 evals must measure.
    """
    sql = """
        SELECT chunk_index, guidance_title, text
        FROM corpus.chunks
        WHERE guidance_id = %(guidance_id)s
        ORDER BY chunk_index
    """
    try:
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, {"guidance_id": guidance_id})
                rows: list[dict[str, Any]] = cur.fetchall()
    except Exception as exc:
        raise ToolError(
            code="DB_ERROR",
            message=str(exc),
            tool="fetch_guidance",
            retryable=True,
        ) from exc

    if not rows:
        raise ToolError(
            code="NOT_FOUND",
            message=f"No chunks found for guidance_id={guidance_id!r}",
            tool="fetch_guidance",
            retryable=False,
        )

    title: str = rows[0]["guidance_title"]
    full_text = "\n\n".join(row["text"] for row in rows)

    return FetchGuidanceResult(
        guidance_id=guidance_id,
        guidance_title=title,
        text=full_text,
        chunk_count=len(rows),
    )


# ─── check_citation ───────────────────────────────────────────────────────────


class CheckCitationInput(BaseModel):
    claim: str
    guidance_id: str
    chunk_index: int
    quoted_text: str | None = None


class CitationCheckResult(BaseModel):
    verified: bool
    source_text: str
    matched_doc_span: list[int] | None = None
    similarity_score: float | None = None


def _normalize(s: str) -> str:
    """Collapse all whitespace runs to a single space and strip."""
    return re.sub(r"\s+", " ", s).strip()


def check_citation(
    claim: str,
    guidance_id: str,
    chunk_index: int,
    quoted_text: str | None = None,
) -> CitationCheckResult:
    """Verify a citation address and optionally check quote faithfulness.

    claim is trace context only — NOT used in verification. Verification is
    deterministic. Whether the passage supports the claim is the critic's job.

    quoted_text=None → key-existence mode: verified=True if the chunk exists.
    quoted_text supplied → ADR-0010 three-step matching.

    NOT_FOUND returns CitationCheckResult(verified=False), never ToolError.
    Only DB connection failures raise ToolError.
    """
    _sql = """
        SELECT text, char_start, char_end
        FROM corpus.chunks
        WHERE guidance_id = %(guidance_id)s AND chunk_index = %(chunk_index)s
    """
    try:
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _sql,
                    {"guidance_id": guidance_id, "chunk_index": chunk_index},
                )
                row: dict[str, Any] | None = cur.fetchone()
    except Exception as exc:
        raise ToolError(
            code="DB_ERROR",
            message=str(exc),
            tool="check_citation",
            retryable=True,
        ) from exc

    # NOT_FOUND → clean verified=False, not a ToolError (ADR 0010).
    if row is None:
        return CitationCheckResult(
            verified=False,
            source_text="",
            matched_doc_span=None,
            similarity_score=None,
        )

    chunk_text: str = row["text"]
    char_start: int = row["char_start"]

    # Key-existence mode — no matching attempted.
    if quoted_text is None:
        return CitationCheckResult(
            verified=True,
            source_text=chunk_text,
            matched_doc_span=None,
            similarity_score=None,
        )

    # ── Three-step matching (ADR 0010) ────────────────────────────────────────

    # Step 1: Normalize both sides.
    norm_quoted = _normalize(quoted_text)
    norm_chunk = _normalize(chunk_text)

    if not norm_quoted:
        # Empty quote after normalization — treat as key-existence.
        return CitationCheckResult(
            verified=True,
            source_text=chunk_text,
            matched_doc_span=None,
            similarity_score=None,
        )

    # Step 2: Normalized substring check with whitespace-flexible regex for span recovery.
    if norm_quoted in norm_chunk:
        # Recover offsets in the STORED (non-normalized) text.
        # Normalized positions do not map to stored positions because normalization
        # collapses whitespace sequences and shifts subsequent offsets.
        escaped_words = re.escape(norm_quoted).split(r"\ ")
        pattern = re.compile(r"\s+".join(escaped_words))
        m = pattern.search(chunk_text)
        if m:
            doc_start = char_start + m.start()
            doc_end = char_start + m.end()
            return CitationCheckResult(
                verified=True,
                source_text=chunk_text,
                matched_doc_span=[doc_start, doc_end],
                similarity_score=None,
            )
        # Substring found in normalized space but regex could not relocate it in
        # stored text (edge case: normalization changed word boundaries).
        return CitationCheckResult(
            verified=True,
            source_text=chunk_text,
            matched_doc_span=None,
            similarity_score=None,
        )

    # Step 3: SequenceMatcher coverage-ratio fallback.
    # Metric: longest_match.size / len(norm_quoted) — NOT ratio().
    # ratio() denominator is dominated by chunk length; returns ~0.05 even on a
    # perfect short-quote match. Coverage ratio asks the right question: what
    # fraction of the quote appears contiguously in the chunk?
    matcher = SequenceMatcher(None, norm_quoted, norm_chunk, autojunk=False)
    longest = matcher.find_longest_match(0, len(norm_quoted), 0, len(norm_chunk))
    coverage = longest.size / len(norm_quoted)

    tau = settings.citation_match_threshold
    if coverage >= tau:
        return CitationCheckResult(
            verified=True,
            source_text=chunk_text,
            matched_doc_span=None,  # cannot pinpoint from longest-match alone
            similarity_score=coverage,
        )

    # No match — return source_text and score for critic reference and τ-calibration.
    return CitationCheckResult(
        verified=False,
        source_text=chunk_text,
        matched_doc_span=None,
        similarity_score=coverage,
    )


# ─── list_recent_guidances ────────────────────────────────────────────────────


class GuidanceRecord(BaseModel):
    guidance_id: str
    guidance_title: str
    ingest_date: datetime


class ListRecentGuidancesInput(BaseModel):
    since_date: str


class ListRecentGuidancesResult(BaseModel):
    guidances: list[GuidanceRecord]


def list_recent_guidances(since_date: str) -> ListRecentGuidancesResult:
    """List guidance documents first ingested since a given ISO date (YYYY-MM-DD).

    Uses MIN(created_at) as an ingest-date proxy; FDA publication date is not
    captured in corpus metadata and may differ.
    """
    from datetime import date

    try:
        date.fromisoformat(since_date)
    except ValueError as exc:
        raise ToolError(
            code="INVALID_INPUT",
            message=f"since_date must be YYYY-MM-DD, got {since_date!r}",
            tool="list_recent_guidances",
            retryable=False,
        ) from exc

    sql = """
        SELECT guidance_id, guidance_title, MIN(created_at) AS ingest_date
        FROM corpus.chunks
        GROUP BY guidance_id, guidance_title
        HAVING MIN(created_at) >= %(since_date)s
        ORDER BY ingest_date DESC
    """
    try:
        with get_conn() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, {"since_date": since_date})
                rows = cur.fetchall()
    except Exception as exc:
        raise ToolError(
            code="DB_ERROR",
            message=str(exc),
            tool="list_recent_guidances",
            retryable=True,
        ) from exc

    return ListRecentGuidancesResult(
        guidances=[
            GuidanceRecord(
                guidance_id=row["guidance_id"],
                guidance_title=row["guidance_title"],
                ingest_date=row["ingest_date"],
            )
            for row in rows
        ]
    )
