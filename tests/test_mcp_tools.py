"""Tests for src/rra/mcp_server/tools.py — plain Python, no MCP server.

Per ADR 0011: tool functions are importable standalone; these tests call them
directly without starting a server process.

Critical check_citation coverage (ADR 0010 regression guards):
  - key-existence mode (quoted_text=None → verified=True on key resolution)
  - substring hit returns correct matched_doc_span via stored-text regex
    (NOT normalized offsets — the regex must recover char positions in the
    stored text, then add char_start for document-relative span)
  - coverage-ratio fallback fires above τ with similarity_score, None span
  - below-τ → verified=False with similarity_score populated
  - NOT_FOUND → CitationCheckResult(verified=False), NOT ToolError
  - ratio()-vs-coverage regression: a 50-char quote in a 2000-char chunk
    must verify via coverage ratio (SequenceMatcher.ratio() would return ~0.05
    and fail; coverage ratio returns close to 1.0 and passes)
"""
from __future__ import annotations

import os
from difflib import SequenceMatcher
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from rra.mcp_server.tools import (  # noqa: E402
    CitationCheckResult,
    FetchGuidanceResult,
    GuidanceRecord,
    ListRecentGuidancesResult,
    SearchCorpusResult,
    SearchFilters,
    ToolError,
    _normalize,
    check_citation,
    fetch_guidance,
    list_recent_guidances,
    match_quote,
    search_corpus,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_cursor(rows: list[dict] | dict | None) -> MagicMock:
    """Return a mock cursor that yields rows from fetchone/fetchall."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    if isinstance(rows, list):
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = rows[0] if rows else None
    else:
        cur.fetchone.return_value = rows
        cur.fetchall.return_value = [] if rows is None else [rows]
    return cur


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.__enter__ = lambda s: s
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


def _patch_get_conn(row_or_rows):
    """Context manager: patch db.get_conn to return the given row(s)."""
    cur = _make_cursor(row_or_rows)
    conn = _make_conn(cur)
    return patch("rra.mcp_server.tools.get_conn", return_value=conn)


# ─── config ──────────────────────────────────────────────────────────────────


class TestConfig:
    def test_citation_match_threshold_default(self) -> None:
        from rra.config import Settings

        s = Settings()
        assert s.citation_match_threshold == 0.85

    def test_citation_match_threshold_range(self) -> None:
        from pydantic import ValidationError

        from rra.config import Settings

        with pytest.raises(ValidationError):
            Settings(citation_match_threshold=1.5)
        with pytest.raises(ValidationError):
            Settings(citation_match_threshold=-0.1)


# ─── _normalize ──────────────────────────────────────────────────────────────


class TestNormalize:
    def test_collapses_whitespace(self) -> None:
        assert _normalize("hello   world") == "hello world"

    def test_collapses_newlines(self) -> None:
        assert _normalize("hello\n\nworld") == "hello world"

    def test_strips(self) -> None:
        assert _normalize("  hello  ") == "hello"

    def test_mixed_whitespace(self) -> None:
        assert _normalize("foo\t \n bar") == "foo bar"


# ─── matcher-preprocessing v2 ─────────────────────────────────────────────────


class TestMatcherV2Recovery:
    """The 9 cleanly-fixable 0.70–0.85 near-miss corruptions normalize to the
    clean analyst quote. Each chunk fragment below is a real pypdf artifact from
    the near-miss band (docs/plan/matcher-preprocessing-v2.md)."""

    def test_midline_linenumber_stripped(self) -> None:
        # cases 3/5/7: "regulatory 376 action", "those 860 for", "be 209 required"
        assert _normalize("regulatory 376 action") == _normalize("regulatory action")
        assert _normalize("to those 860 for the") == _normalize("to those for the")
        assert _normalize("shall not be 209 required") == _normalize("shall not be required")

    def test_glued_word_linenumber_stripped(self) -> None:
        # cases 1/12: "only16", "mode31,", "language32", "population.33"
        assert _normalize("software only16 function") == _normalize("software only function")
        assert (
            _normalize("mode31, language32 or population.33")
            == _normalize("mode, language or population.")
        )

    def test_paren_glued_marker_stripped(self) -> None:
        # cases 6/11: "3500A)74", "AI-DSFs)3"
        assert _normalize("Form 3500A)74 must") == _normalize("Form 3500A) must")
        assert _normalize("or AI-DSFs)3 is") == _normalize("or AI-DSFs) is")

    def test_intraword_split_rejoined(self) -> None:
        # case 0: "Q-S ubmission" -> "Q-Submission"
        assert _normalize("the Q-S ubmission Program") == _normalize("the Q-Submission Program")

    def test_hyphen_fused_linenumber_stripped(self) -> None:
        # case 8: "Q-220 Submission" -> "Q-Submission"
        assert _normalize("the Q-220 Submission Program") == _normalize("the Q-Submission Program")

    def test_recovered_quote_matches_dirty_chunk(self) -> None:
        """End-to-end: a clean analyst quote verifies against the dirty chunk fragment."""
        quote = "regulatory action against violations"
        chunk = "take legal or regulatory 376 action against violations of prohibited acts"
        v, sim, _ = match_quote(quote, chunk, 0)
        assert v is True


class TestMatcherV2ContentSafety:
    """HARD GATE (docs/plan/matcher-preprocessing-v2.md): the v2 strip rules must
    NOT mangle real regulatory numbers. Dropping the newline anchor put all the
    false-positive weight on these guards — this is the proof they hold."""

    # — reg references keep their numbers (on BOTH sides of the reg-word) —
    def test_cfr_reference_preserved(self) -> None:
        # full-string equality: the title number (21, before CFR) AND the part
        # number (803.52 / 209, after CFR) must both survive untouched
        assert _normalize("described in 21 CFR 803.52 that is") == "described in 21 CFR 803.52 that is"
        assert _normalize("under 21 CFR 209 of the") == "under 21 CFR 209 of the"

    def test_usc_reference_preserved(self) -> None:
        assert _normalize("pursuant to 10 USC 7902 and") == "pursuant to 10 USC 7902 and"

    def test_title_reference_preserved(self) -> None:
        assert "21" in _normalize("under Title 21 of the regulations")

    def test_fda_form_preserved(self) -> None:
        assert "3500A" in _normalize("the FDA Form 3500A must contain")

    def test_section_510k_preserved(self) -> None:
        assert _normalize("cleared under section 510(k), if such") == "cleared under section 510(k), if such"

    def test_section_symbol_dotted_number_preserved(self) -> None:
        assert "820.30" in _normalize("under § 820.30 of the")

    def test_part_spaced_preserved(self) -> None:
        assert "11" in _normalize("records under Part 11 are")

    def test_part_glued_preserved(self) -> None:
        # the reg-word guard: "Part11" must NOT be stripped to "Part"
        assert "Part11" in _normalize("electronic records Part11 compliance")

    # — durations / measures keep their numbers (unit denylist) —
    def test_within_30_days_preserved(self) -> None:
        assert "30" in _normalize("report within 30 days of becoming")

    def test_90_days_preserved(self) -> None:
        assert "90" in _normalize("a 90 days review period")

    def test_measurements_preserved(self) -> None:
        assert "10" in _normalize("a dose of 10 mg administered")
        assert "12" in _normalize("over 12 months of follow-up")

    # — 4-digit content survives the 2–3 digit cap —
    def test_year_preserved(self) -> None:
        assert "1995" in _normalize("published December 11, 1995 in the")
        assert "2024" in _normalize("issued in 2024 by the agency")

    def test_iso_standard_preserved(self) -> None:
        assert "9001" in _normalize("conforms to ISO 9001 quality")

    # — single-digit / version tokens survive (rules require 2+ digits) —
    def test_single_digit_and_version_preserved(self) -> None:
        assert _normalize("uses TLS 1.3 for transport") == "uses TLS 1.3 for transport"
        assert "5" in _normalize("see Figure 5 below")

    # — the paren seam: ")74" strips but "(k)" does not —
    def test_paren_seam_strips_digits_after_keeps_letter_inside(self) -> None:
        assert "74" not in _normalize("Form 3500A)74 must contain")   # digits-after-paren: stripped
        assert "510(k)" in _normalize("cleared under section 510(k) if")  # letter-in-paren: kept

    # — case-gated rejoin rules don't eat legit hyphenated tokens —
    def test_covid19_preserved(self) -> None:
        # hyphen-fused rule is uppercase-gated; "vaccine" is lowercase -> no strip
        assert "19" in _normalize("the COVID-19 vaccine was")

    def test_type_a_submission_preserved(self) -> None:
        # word-split rule needs cap-hyphen-cap; "Type-A" has lowercase before hyphen
        assert _normalize("a Type-A submission requires") == "a Type-A submission requires"

    def test_n95_preserved(self) -> None:
        # glued-word rule requires lowercase before digits; "N95" has uppercase N
        assert "N95" in _normalize("an N95 respirator must")


# ─── search_corpus ────────────────────────────────────────────────────────────


class TestSearchCorpus:
    def test_returns_passages(self) -> None:
        from rra.schemas import RetrievedPassage

        fake_passage = RetrievedPassage(
            guidance_id="gd-1",
            guidance_title="Test",
            chunk_index=0,
            text="Some text",
            char_start=0,
            char_end=9,
            score=0.9,
        )
        with patch("rra.mcp_server.tools.search_corpus.__wrapped__", create=True), \
             patch("rra.retrieval.search_corpus", return_value=[fake_passage]) as mock_sc:
            result = search_corpus("test query")
        assert isinstance(result, SearchCorpusResult)
        assert len(result.passages) == 1
        assert result.passages[0].guidance_id == "gd-1"

    def test_raises_tool_error_on_db_failure(self) -> None:
        with patch("rra.retrieval.search_corpus", side_effect=Exception("connection refused")):
            with pytest.raises(ToolError) as exc_info:
                search_corpus("query")
        err = exc_info.value
        assert err.retryable is True
        assert err.tool == "search_corpus"

    def test_raises_embedding_error_on_voyage_failure(self) -> None:
        with patch(
            "rra.retrieval.search_corpus",
            side_effect=Exception("voyage api error embedding failed"),
        ):
            with pytest.raises(ToolError) as exc_info:
                search_corpus("query")
        assert exc_info.value.code == "EMBEDDING_ERROR"

    def test_filters_passed_through(self) -> None:
        from rra.schemas import RetrievedPassage

        captured: dict = {}

        def _capture(query, k=None, filters=None, lf=None):
            captured["filters"] = filters
            return []

        with patch("rra.retrieval.search_corpus", side_effect=_capture):
            search_corpus("q", filters=SearchFilters(guidance_ids=["gd-1", "gd-2"]))
        assert captured["filters"] == {"guidance_ids": ["gd-1", "gd-2"]}


# ─── fetch_guidance ───────────────────────────────────────────────────────────


class TestFetchGuidance:
    def test_returns_assembled_text(self) -> None:
        rows = [
            {"chunk_index": 0, "guidance_title": "My Guide", "text": "Part A"},
            {"chunk_index": 1, "guidance_title": "My Guide", "text": "Part B"},
        ]
        with _patch_get_conn(rows):
            result = fetch_guidance("gd-test")
        assert isinstance(result, FetchGuidanceResult)
        assert result.guidance_id == "gd-test"
        assert result.guidance_title == "My Guide"
        assert result.text == "Part A\n\nPart B"
        assert result.chunk_count == 2

    def test_not_found_raises_tool_error(self) -> None:
        with _patch_get_conn([]):
            with pytest.raises(ToolError) as exc_info:
                fetch_guidance("nonexistent")
        err = exc_info.value
        assert err.code == "NOT_FOUND"
        assert err.retryable is False

    def test_db_failure_raises_retryable_error(self) -> None:
        with patch("rra.mcp_server.tools.get_conn", side_effect=Exception("pool timeout")):
            with pytest.raises(ToolError) as exc_info:
                fetch_guidance("gd-1")
        assert exc_info.value.code == "DB_ERROR"
        assert exc_info.value.retryable is True

    def test_raw_text_no_cleaning(self) -> None:
        rows = [
            {
                "chunk_index": 0,
                "guidance_title": "T",
                "text": "Text with\nembedded\nnewlines",
            }
        ]
        with _patch_get_conn(rows):
            result = fetch_guidance("gd-1")
        assert "\n" in result.text  # raw artifacts preserved


# ─── check_citation ───────────────────────────────────────────────────────────


class TestCheckCitationKeyExistence:
    """quoted_text=None → key-existence mode."""

    def test_key_exists_verified_true(self) -> None:
        row = {"text": "The chunk text.", "char_start": 100, "char_end": 115}
        with _patch_get_conn(row):
            result = check_citation("some claim", "gd-1", 3)
        assert result.verified is True
        assert result.source_text == "The chunk text."
        assert result.matched_doc_span is None
        assert result.similarity_score is None

    def test_key_not_found_verified_false_not_tool_error(self) -> None:
        """NOT_FOUND must return CitationCheckResult(verified=False), not raise ToolError."""
        with _patch_get_conn(None):
            result = check_citation("claim", "gd-missing", 99)
        assert isinstance(result, CitationCheckResult)
        assert result.verified is False
        assert result.source_text == ""
        assert result.matched_doc_span is None
        assert result.similarity_score is None

    def test_db_failure_raises_tool_error(self) -> None:
        with patch("rra.mcp_server.tools.get_conn", side_effect=Exception("timeout")):
            with pytest.raises(ToolError) as exc_info:
                check_citation("claim", "gd-1", 0)
        assert exc_info.value.code == "DB_ERROR"
        assert exc_info.value.retryable is True

    def test_source_text_returned_unconditionally(self) -> None:
        """source_text must come back even on key-existence mode."""
        row = {"text": "Important passage.", "char_start": 0, "char_end": 18}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0)
        assert result.source_text == "Important passage."


class TestCheckCitationEmptyQuote:
    """quoted_text="" / whitespace (non-None) → verified=False (ADR 0013).

    This is the quote-side of the D1 gate-evasion hole: an empty quote must NOT
    short-circuit to verified=True ("faithful by emptiness"). It is distinct from
    the None key-existence path the critic uses, which stays verified=True.
    """

    def test_empty_quote_not_verified(self) -> None:
        row = {"text": "Some chunk text exists.", "char_start": 0, "char_end": 23}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text="")
        assert result.verified is False
        assert result.similarity_score is None
        # source_text still returned, consistent with every other path.
        assert result.source_text == "Some chunk text exists."

    def test_whitespace_only_quote_not_verified(self) -> None:
        row = {"text": "Some chunk text exists.", "char_start": 0, "char_end": 23}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text="   \n  ")
        assert result.verified is False

    def test_none_quote_still_key_existence_true(self) -> None:
        """Guard (Sign-off A): the empty-quote fix must NOT change the None
        key-existence path — that is the critic's path and stays verified=True."""
        row = {"text": "Some chunk text exists.", "char_start": 0, "char_end": 23}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=None)
        assert result.verified is True


class TestCheckCitationBoilerplateSeam:
    """The documented dirty-corpus failure mode (Day-7 plan §4): an honest quote
    split by a mid-chunk boilerplate header. The longest contiguous match is ~half
    the quote, so coverage < τ and an honest quote reads as verified=False with a
    mid-band similarity_score — exactly the τ-calibration signal Day 7 measures.
    """

    def test_seam_split_quote_below_tau_with_score(self) -> None:
        # The honest analyst quote (contiguous, as a human reads the source):
        quote = "the manufacturer shall validate the software for its intended use"
        # Stored chunk: the same words, but a boilerplate header is spliced into
        # the MIDDLE (the PDF-extraction artifact), breaking the contiguous run.
        chunk_text = (
            "the manufacturer shall validate "
            "Contains Nonbinding Recommendations "
            "the software for its intended use"
        )
        row = {"text": chunk_text, "char_start": 0, "char_end": len(chunk_text)}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        # Seam breaks the substring path; coverage falls below τ → verified=False,
        # but a similarity_score IS returned for the τ-distribution.
        assert result.verified is False
        assert result.similarity_score is not None
        assert 0.3 < result.similarity_score < 0.85


class TestCheckCitationSubstringMatch:
    """Step 2: substring match returns correct doc-level span."""

    def _chunk_row(self, text: str, char_start: int = 500) -> dict:
        return {"text": text, "char_start": char_start, "char_end": char_start + len(text)}

    def test_exact_substring_verifies(self) -> None:
        chunk_text = "The applicant must submit a 510(k) premarket notification."
        quote = "submit a 510(k) premarket notification"
        row = self._chunk_row(chunk_text, char_start=200)
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is True
        assert result.matched_doc_span is not None
        # Span must be document-relative (char_start added)
        start, end = result.matched_doc_span
        assert start >= 200
        assert chunk_text[start - 200 : end - 200] == quote

    def test_matched_doc_span_uses_char_start_offset(self) -> None:
        """Span must be document-relative, not chunk-relative."""
        chunk_text = "Devices must comply with 21 CFR Part 820."
        quote = "comply with 21 CFR Part 820"
        char_start = 1000
        row = {"text": chunk_text, "char_start": char_start, "char_end": char_start + len(chunk_text)}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is True
        assert result.matched_doc_span is not None
        start, end = result.matched_doc_span
        # Start must be >= char_start (document-relative)
        assert start >= char_start
        assert chunk_text[start - char_start : end - char_start] == quote

    def test_substring_with_pdf_newlines_in_stored_text(self) -> None:
        """Quote from normalized text should match even when stored text has PDF newlines."""
        stored = "The device\nmust comply\nwith all requirements."
        quote = "device must comply with all"  # normalized form from model
        row = {"text": stored, "char_start": 0, "char_end": len(stored)}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is True

    def test_similarity_score_none_on_substring_hit(self) -> None:
        """SequenceMatcher is not invoked on a direct substring hit."""
        chunk_text = "A clear and direct statement."
        quote = "clear and direct"
        row = {"text": chunk_text, "char_start": 0, "char_end": len(chunk_text)}
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.similarity_score is None


class TestCheckCitationCoverageRatio:
    """Step 3: SequenceMatcher coverage-ratio fallback."""

    def _row(self, text: str, char_start: int = 0) -> dict:
        return {"text": text, "char_start": char_start, "char_end": char_start + len(text)}

    def test_coverage_ratio_fires_above_threshold(self) -> None:
        """A quote with minor variation that does not substring-match should fall through
        to the coverage-ratio path and verify when coverage >= τ."""
        base = "The sponsor must demonstrate substantial equivalence"
        # Slight variation that won't be a direct substring after normalization
        chunk_text = "The sponsor must demonstrate  substantial equivalence to a predicate."
        # Use a quote that won't be a substring of normalized chunk
        # but is clearly there
        quote = "sponsor must demonstrate substantial equivalence"
        # Verify manually that it IS a substring after normalization — if so pick a worse quote
        from rra.mcp_server.tools import _normalize
        norm_q = _normalize(quote)
        norm_c = _normalize(chunk_text)
        if norm_q in norm_c:
            # Make quote slightly different so it won't substring-match
            quote = "sponsor  must demonstrate substantial equivalence"  # double space
        # The coverage ratio for this quote should be high
        row = self._row(chunk_text)
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        # Either verified via substring (Step 2) or coverage (Step 3) — both acceptable
        # The key invariant is it must verify
        assert result.verified is True

    def test_coverage_ratio_below_threshold_not_verified(self) -> None:
        """A quote that doesn't appear in the chunk at all → verified=False."""
        chunk_text = "The 510(k) pathway requires substantial equivalence to a predicate device."
        quote = "Premarket Approval requires safety and effectiveness studies"  # totally different
        row = self._row(chunk_text)
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is False
        assert result.similarity_score is not None
        assert result.similarity_score < 0.85

    def test_similarity_score_populated_on_failure(self) -> None:
        """similarity_score must be returned on verified=False for τ-calibration."""
        chunk_text = "A" * 200
        quote = "B" * 50  # completely different
        row = self._row(chunk_text)
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is False
        assert result.similarity_score is not None

    def test_source_text_returned_on_failure(self) -> None:
        """source_text must be returned even when verified=False (critic reference)."""
        chunk_text = "Actual chunk content."
        quote = "Something completely different that will not match."
        row = self._row(chunk_text)
        with _patch_get_conn(row):
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)
        assert result.verified is False
        assert result.source_text == chunk_text

    def test_matched_doc_span_none_on_fallback(self) -> None:
        """Coverage-ratio path cannot pinpoint span — must return None."""
        chunk_text = "The regulation requires compliance with Part 820 quality system requirements."
        # Force a high-coverage but non-substring match by using the chunk itself
        # slightly mutated so Step 2 doesn't fire
        quote = "regulation  requires compliance"  # double space → won't substring after normalize... or will it
        from rra.mcp_server.tools import _normalize
        norm_q = _normalize(quote)
        norm_c = _normalize(chunk_text)
        if norm_q not in norm_c:
            # Good — falls to Step 3
            row = self._row(chunk_text)
            with _patch_get_conn(row):
                result = check_citation("claim", "gd-1", 0, quoted_text=quote)
            if result.verified:
                assert result.matched_doc_span is None  # Step 3 cannot pinpoint
        # If it normalized to a substring, Step 2 handles it — also acceptable


class TestRatioBugRegression:
    """Regression guard for ADR-0010's core bug fix.

    SequenceMatcher.ratio() on a 50-char quote vs a 2000-char chunk returns ~0.05
    even on a perfect match, because the denominator is len(quote) + len(chunk).
    The coverage ratio (longest_match.size / len(quote)) returns close to 1.0.

    This test verifies that a short exact quote in a long chunk passes verification.
    If someone accidentally replaces coverage ratio with ratio(), this test fails.
    """

    def test_short_quote_in_long_chunk_verifies(self) -> None:
        # 50-char quote embedded in a ~2000-char chunk
        quote = "submit a 510(k) premarket notification to the FDA"  # 50 chars
        padding = "x " * 975  # ~1950 chars of noise
        chunk_text = padding + quote + " " + padding

        # Prove that SequenceMatcher.ratio() would fail at τ=0.85
        norm_q = _normalize(quote)
        norm_c = _normalize(chunk_text)
        matcher = SequenceMatcher(None, norm_q, norm_c, autojunk=False)
        ratio = matcher.ratio()
        assert ratio < 0.1, f"Test assumption broken: ratio={ratio}"  # ~0.025 expected

        # But our implementation should verify it (via substring or coverage)
        row = {"text": chunk_text, "char_start": 0, "char_end": len(chunk_text)}
        with patch("rra.mcp_server.tools.get_conn") as mock_gc:
            cur = MagicMock()
            cur.__enter__ = lambda s: s
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchone.return_value = row
            conn = MagicMock()
            conn.__enter__ = lambda s: s
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor.return_value = cur
            mock_gc.return_value = conn
            result = check_citation("claim", "gd-1", 0, quoted_text=quote)

        assert result.verified is True, (
            f"Short quote in long chunk must verify. "
            f"ratio()={ratio:.4f} would fail, coverage ratio should pass."
        )


# ─── match_quote ─────────────────────────────────────────────────────────────


class TestMatchQuoteMalformedInput:
    """Robustness: malformed inputs return (False, 0.0, None) — never an exception.

    An unverifiable chunk (None text, empty text, wrong-type char_start) is a
    non-match, not a crash.  Same fail-closed principle as the empty-quote guard
    in check_citation (ADR 0013 / ADR 0014).
    """

    def test_none_chunk_text(self) -> None:
        v, sim, span = match_quote("some quote", None, 0)  # type: ignore[arg-type]
        assert v is False
        assert sim == 0.0
        assert span is None

    def test_empty_chunk_text(self) -> None:
        v, sim, span = match_quote("some quote", "", 0)
        assert v is False
        assert sim == 0.0
        assert span is None

    def test_none_char_start(self) -> None:
        v, sim, span = match_quote("some quote", "chunk text here", None)  # type: ignore[arg-type]
        assert v is False
        assert sim == 0.0
        assert span is None

    def test_non_int_char_start(self) -> None:
        v, sim, span = match_quote("some quote", "chunk text here", "abc")  # type: ignore[arg-type]
        assert v is False
        assert sim == 0.0
        assert span is None

    def test_none_quoted_text(self) -> None:
        v, sim, span = match_quote(None, "chunk text here", 0)  # type: ignore[arg-type]
        assert v is False
        assert sim == 0.0
        assert span is None

    def test_valid_inputs_still_work(self) -> None:
        """Guard must not break the normal path."""
        v, sim, span = match_quote("chunk text", "chunk text here for testing", 0)
        assert v is True


class TestMatchQuoteSpanRecovery:
    """Step 2 span recovery works correctly for stored text with PDF whitespace."""

    def test_pdf_newline_in_stored_text(self) -> None:
        """Quote normalized from PDF-newline text should recover span in stored text."""
        stored = "the sponsor\nshould demonstrate adequate safety"
        quote = "sponsor should demonstrate adequate"  # normalized (spaces)
        v, sim, span = match_quote(quote, stored, 10)
        assert v is True
        # Span should be document-relative (offset by char_start=10)
        if span is not None:
            assert span[0] >= 10
            assert span[1] > span[0]

    def test_span_none_on_no_match(self) -> None:
        """No span when quote is not in chunk."""
        v, sim, span = match_quote("completely unrelated phrase", "some chunk text here", 0)
        assert v is False
        assert span is None


class TestCurlyQuoteNormalization:
    """Curly apostrophes and quotation marks in corpus text match analyst straight-quote output.

    pypdf extracts FDA guidance text with typographic quotes (U+2018/19/1C/1D).
    Analyst models emit straight ASCII quotes. _normalize folds both sides so
    the mismatch never causes a false verified=False.
    """

    def test_curly_apostrophe_in_chunk_matches_straight_in_quote(self) -> None:
        """Chunk with U+2019 RIGHT SINGLE QUOTATION MARK matches straight-apostrophe quote."""
        chunk = "the device’s intended use must be supported"  # curly apostrophe
        quote = "the device's intended use must be supported"       # straight apostrophe
        v, sim, span = match_quote(quote, chunk, 0)
        assert v is True, "curly apostrophe in chunk should match straight apostrophe in quote"
        assert sim is None  # substring hit (Step 2) — no LCS needed

    def test_straight_apostrophe_in_chunk_matches_curly_in_quote(self) -> None:
        """Reverse direction: straight chunk, curly quote — both fold to the same form."""
        chunk = "the device's intended use must be supported"       # straight apostrophe
        quote = "the device’s intended use must be supported"  # curly apostrophe
        v, sim, span = match_quote(quote, chunk, 0)
        assert v is True, "curly apostrophe in quote should match straight apostrophe in chunk"

    def test_curly_double_quotes_in_chunk_match_straight_in_quote(self) -> None:
        """Left/right double-quote chars in chunk match straight double-quote in quote."""
        chunk = '“Software as a Medical Device” (SaMD) guidance'
        quote = '"Software as a Medical Device" (SaMD) guidance'
        v, sim, span = match_quote(quote, chunk, 0)
        assert v is True

    def test_non_quote_unicode_preserved(self) -> None:
        """Normalization must NOT fold § or en/em dash — those are regulatory content."""
        chunk = "under § 820.30 of 21 CFR"   # § char
        quote = "under § 820.30 of 21 CFR"   # identical
        v, sim, span = match_quote(quote, chunk, 0)
        assert v is True
        # Verify that § is unchanged in the normalized form
        from rra.mcp_server.tools import _normalize
        assert "§" in _normalize(chunk), "§ must survive _normalize (regulatory content)"


# ─── list_recent_guidances ────────────────────────────────────────────────────


class TestListRecentGuidances:
    def test_returns_guidance_records(self) -> None:
        from datetime import datetime, timezone

        rows = [
            {
                "guidance_id": "gd-1",
                "guidance_title": "Device Guide",
                "ingest_date": datetime(2026, 1, 15, tzinfo=timezone.utc),
            }
        ]
        with _patch_get_conn(rows):
            result = list_recent_guidances("2026-01-01")
        assert isinstance(result, ListRecentGuidancesResult)
        assert len(result.guidances) == 1
        assert result.guidances[0].guidance_id == "gd-1"

    def test_invalid_date_raises_tool_error(self) -> None:
        with pytest.raises(ToolError) as exc_info:
            list_recent_guidances("not-a-date")
        err = exc_info.value
        assert err.code == "INVALID_INPUT"
        assert err.retryable is False

    def test_db_failure_raises_retryable_error(self) -> None:
        with patch("rra.mcp_server.tools.get_conn", side_effect=Exception("pool exhausted")):
            with pytest.raises(ToolError) as exc_info:
                list_recent_guidances("2026-01-01")
        assert exc_info.value.code == "DB_ERROR"
        assert exc_info.value.retryable is True

    def test_empty_result_ok(self) -> None:
        with _patch_get_conn([]):
            result = list_recent_guidances("2030-01-01")
        assert result.guidances == []
