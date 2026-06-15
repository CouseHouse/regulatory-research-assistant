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
from pydantic import BaseModel, Field

from rra.config import settings
from rra.ports.vectorstore import get_vector_store
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
    try:
        rows: list[dict[str, Any]] = get_vector_store().fetch_guidance_chunks(guidance_id)
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


# Maps typographic quote/apostrophe characters to ASCII equivalents.
# Narrow: only the six quote/apostrophe variants. Preserves §, en-dash (–),
# em-dash (—), ×, and all other meaningful regulatory-document characters.
_CURLY_MAP = str.maketrans({
    0x2018: "'",  # LEFT SINGLE QUOTATION MARK  → '
    0x2019: "'",  # RIGHT SINGLE QUOTATION MARK → '
    0x201C: '"',  # LEFT DOUBLE QUOTATION MARK  → "
    0x201D: '"',  # RIGHT DOUBLE QUOTATION MARK → "
    0x2032: "'",  # PRIME                       → '
    0x2033: '"',  # DOUBLE PRIME                → "
})

# Strips pypdf-embedded draft-guidance line numbers applied before \s+ collapse.
# In FDA draft guidances, pypdf injects sequential 2–4 digit line numbers between
# sentence fragments (e.g. "medical 105 \ndevices" → "medical\ndevices").
#
# Lookbehind logic:
#   (?<!CFR) / (?<!USC) / (?<!art): guard "21 CFR 820\n", "10 USC 7902\n",
#     "Part 820\n" ("art" = last 3 chars of "Part").
#   (?<=[\w,;:\.\)]): require a word/punct char immediately before the spaces
#     so a bare number at start of line (handled by _LINENUM_LINE_RE) is
#     caught by the second pattern instead.
#
# Preserved examples:  "21 CFR 820.30" (dot + digits), "510(k)" (paren),
#   "§ 820.30" (§ not in lookbehind set), "TLS 1.3" (single digit),
#   "90 days" (not before \n).
# Stripped examples:   "medical 105 \ndevices", "controls. 831\nthat".
_LINENUM_INLINE_RE = re.compile(
    r"(?<!CFR)(?<!USC)(?<!art)(?<=[\w,;:\.\)])\s+\d{2,4}\s*\n"
)
# Isolated lines that contain only a 2–4 digit number (e.g. blank-line runs
# between sections in numbered guidance PDFs). No lookbehind needed here
# because the line is standalone.
_LINENUM_LINE_RE = re.compile(r"(?m)^\s*\d{2,4}\s*\n")

# ── matcher-preprocessing v2 (docs/plan/matcher-preprocessing-v2.md) ──────────
# The v1 rules above only fire on a trailing newline. pypdf also drops
# line-numbers MID-LINE, glues them onto adjacent tokens, and splits words across
# the line break. The v2 rules below recover those, grounded in the real
# 0.70–0.85 near-miss band (the 9 cleanly-fixable citations).
#
# Dropping the newline anchor removes the strongest "this digit is a line-number,
# not content" signal, so each rule carries heavy guards:
#   - reg-word backward guards (CFR/USC/Part/Form/FDA/Section) keep "21 CFR 209",
#     "Part 11", "Form FDA 3500A", "Section 510"…
#   - a unit/measure forward denylist keeps "within 30 days", "90 days", "10 mg"…
#   - the mid-line / hyphen rules cap the run at 2–3 digits, so 4-digit content
#     survives untouched: years ("1995"/"2024"), standards ("ISO 9001"), FDA
#     forms ("1572").
#   - the word-rejoin rules are case-gated, keeping "COVID-19 vaccine", "N95",
#     "Type-A submission".
# Documented residual blind spots (NOT chased): a 2–3-digit count before an
# unlisted noun, an alphanumeric identifier ("p53"), or a real cap-hyphen-cap
# pair before a lowercase word ("X-Y coordinate"). These are held safe in
# practice by SYMMETRIC normalization — the same transform runs on both quote and
# chunk, so a previously-passing match cannot break from a transform alone — and
# are verified by the zero-regression smoke (smoke_rechunk --table chunks).

# Unit/measure tokens that mark a preceding number as CONTENT, not a line-number.
_UNIT_WORDS = (
    r"days?|months?|weeks?|years?|hours?|minutes?|seconds?"
    r"|mg|mcg|ng|kg|g|mL|L|dL|mm|cm|nm|Hz|kHz|percent"
    r"|patients?|subjects?|participants?|devices?|samples?|cases?|sites?"
)

# Rule 3 — intra-word PDF line-break split inside a cap-hyphen-cap compound:
#   "Q-S ubmission" → "Q-Submission".  (cap-hyphen-cap, space, 3+ lowercase)
_WORDSPLIT_RE = re.compile(r"(?<=[A-Z]-[A-Z])\s+(?=[a-z]{3,})")

# Rule 1b — line-number fused after a hyphen, before a Capitalized continuation:
#   "Q-220 Submission" → "Q-Submission".  Upper+lower lookahead keeps
#   "COVID-19 vaccine"; the 2–3 digit cap keeps "ISO-9001 Standard".
_HYPHEN_LINENUM_RE = re.compile(r"(?<=-)\d{2,3}\s+(?=[A-Z][a-z])")

# Rule 1 — mid-line line-number: a 2–3 digit run BETWEEN two same-line word
# tokens (no newline anchor):  "regulatory 376 action" → "regulatory action".
# Reg numbers are content on BOTH sides of the reg-word: "CFR 820" (number after)
# is caught by the backward guards; "21 CFR" / "Title 21" (number before) by the
# forward guard. Without the forward guard the leading title number is stripped.
_LINENUM_MIDLINE_RE = re.compile(
    r"(?<!CFR)(?<!USC)(?<!art)(?<!Form)(?<!FDA)(?<!ection)(?<!itle)"  # number AFTER reg-word
    r"(?<=[\w,;:.)])\s+\d{2,3}\s+"                                    # ' 376 '
    r"(?!(?:" + _UNIT_WORDS + r")\b)"                                 # not ' 30 days'
    r"(?!(?:CFR|USC|Part|Section)\b)"                                 # number BEFORE reg-word
    r"(?=[A-Za-z])"                                                   # before a word
)

# Rule 2a — digit run fused directly after a closing paren (footnote/line marker):
#   "3500A)74" → "3500A)",  "AI-DSFs)3" → "AI-DSFs)".  Leaves "510(k)" untouched.
_PAREN_MARKER_RE = re.compile(r"(?<=\))\d{1,3}(?=$|\s|[.,;:])")

# Rule 2b — 2–3 digit run fused to the end of a lowercase word, before a boundary:
#   "only16" → "only",  "mode31," → "mode,".  Reg-word guard keeps "Part11";
#   the lowercase requirement keeps "N95", "B12".
_GLUED_WORD_RE = re.compile(
    r"(?<!Part)(?<!Form)(?<!FDA)(?<!CFR)(?<!USC)(?<!ection)(?<!Title)(?<!Annex)"
    r"(?<=[a-z])\d{2,3}(?=$|\s|[.,;:)])"
)
# Rule 2b' — same, fused after a word-final period: "population.33" → "population."
#   A LETTER before the period is required, so "§ 820.30" / "v1.33" survive.
_GLUED_DOTWORD_RE = re.compile(r"(?<=[a-z]\.)\d{2,3}(?=$|\s|[.,;:)])")


def _normalize(s: str) -> str:
    """Whitespace-normalize; fold typographic quotes; strip PDF line-number noise.

    Applied to BOTH quote and chunk before any substring or LCS comparison. Because
    the same deterministic transform runs on both sides, a previously-passing match
    cannot break from a transform alone — the basis of the zero-regression gate.

    Order (v1 = Day-7 newline-anchored; v2 = matcher-preprocessing-v2):
      1. Curly quote/apostrophe to ASCII (U+2018/19/1C/1D/32/33).
      2. v2 word-rejoins FIRST (intra-word split, hyphen-fused line-number), so the
         line-number passes see reconstructed words.
      3. v1 inline line-numbers (newline-anchored), then v2 mid-line, paren-fused
         and word-fused line-numbers.
      4. v1 isolated number-only lines.
      5. Final whitespace collapse.
    Non-quote Unicode (§, en/em dash) is preserved — regulatory content, not noise.
    """
    s = s.translate(_CURLY_MAP)
    s = _WORDSPLIT_RE.sub("", s)          # "Q-S ubmission"       -> "Q-Submission"
    s = _HYPHEN_LINENUM_RE.sub("", s)     # "Q-220 Submission"    -> "Q-Submission"
    s = _LINENUM_INLINE_RE.sub("\n", s)   # v1: " 105\n"           -> "\n"
    s = _LINENUM_MIDLINE_RE.sub(" ", s)   # "regulatory 376 act"  -> "regulatory act"
    s = _PAREN_MARKER_RE.sub("", s)       # "3500A)74"            -> "3500A)"
    s = _GLUED_DOTWORD_RE.sub("", s)      # "population.33"       -> "population."
    s = _GLUED_WORD_RE.sub("", s)         # "only16"              -> "only"
    s = _LINENUM_LINE_RE.sub("", s)       # v1: isolated number-only lines
    return re.sub(r"\s+", " ", s).strip()


def match_quote(
    quoted_text: str,
    chunk_text: str,
    char_start: int,
) -> tuple[bool, float | None, list[int] | None]:
    """ADR-0010 three-step matching algorithm — pure function, no DB, no config I/O.

    Returns (verified, similarity_score, matched_doc_span).
    Called by check_citation (production path) and by the $0 text-only smoke
    (evals/smoke_rechunk.py — ADR 0014). Sharing one implementation prevents
    drift between the diagnostic and production matching paths.

    Malformed inputs (None, empty chunk_text, non-int char_start) return
    (False, 0.0, None) rather than raising — an unverifiable chunk is a
    non-match, not an exception (same fail-closed principle as the empty-quote
    guard in check_citation).
    """
    # Guard malformed inputs before any _normalize call.
    if (
        not isinstance(quoted_text, str)
        or not isinstance(chunk_text, str)
        or not chunk_text
        or not isinstance(char_start, int)
    ):
        return False, 0.0, None

    norm_quoted = _normalize(quoted_text)
    norm_chunk = _normalize(chunk_text)

    if not norm_quoted:
        return False, None, None

    # Step 2: normalized substring + whitespace-flexible regex for span recovery.
    if norm_quoted in norm_chunk:
        # Split into words BEFORE escaping so re.escape does not consume spaces.
        # re.escape no longer escapes spaces in Python 3.7+, so the old
        # re.escape(s).split(r"\ ") pattern never splits and \s+ is never joined.
        words = norm_quoted.split()
        pattern = re.compile(r"\s+".join(re.escape(w) for w in words))
        m = pattern.search(chunk_text)
        if m:
            return True, None, [char_start + m.start(), char_start + m.end()]
        return True, None, None

    # Step 3: SequenceMatcher coverage-ratio fallback.
    matcher = SequenceMatcher(None, norm_quoted, norm_chunk, autojunk=False)
    longest = matcher.find_longest_match(0, len(norm_quoted), 0, len(norm_chunk))
    coverage = longest.size / len(norm_quoted)

    tau = settings.citation_match_threshold
    return coverage >= tau, coverage, None


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
    quoted_text empty/whitespace (non-None) → verified=False: faithfulness is
        unassessable and must NOT be scored faithful-by-emptiness (ADR 0013).
        This is distinct from the None key-existence path.

    NOT_FOUND returns CitationCheckResult(verified=False), never ToolError.
    Only DB connection failures raise ToolError.
    """
    try:
        row: dict[str, Any] | None = get_vector_store().fetch_chunk(
            guidance_id, chunk_index
        )
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

    if not quoted_text.strip():
        # Empty/whitespace quoted_text (non-None) — faithfulness CANNOT be
        # assessed, so fail closed: verified=False. This is NOT key-existence
        # (that is the quoted_text IS None path above, untouched). Returning True
        # here would be the quote-side of the D1 gate-evasion hole — a citation
        # scored "faithful by emptiness" (Day-7 plan §7-#3 / ADR 0013). Callers
        # treat an empty analyst quote as "no quote" and count it separately;
        # this branch is the defensive backstop if one slips through.
        return CitationCheckResult(
            verified=False,
            source_text=chunk_text,
            matched_doc_span=None,
            similarity_score=None,
        )

    # Three-step matching delegated to pure function (ADR 0014 — shared with
    # the $0 text-only smoke so production and diagnostic paths are identical).
    verified, similarity_score, matched_doc_span = match_quote(
        quoted_text, chunk_text, char_start
    )
    return CitationCheckResult(
        verified=verified,
        source_text=chunk_text,
        matched_doc_span=matched_doc_span,
        similarity_score=similarity_score,
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

    try:
        rows = get_vector_store().list_recent_guidances_rows(since_date)
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
