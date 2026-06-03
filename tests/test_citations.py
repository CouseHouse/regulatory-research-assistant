"""Tests for rra.citations.parse_answer — the SHARED inline-citation parser.

This parser feeds BOTH the API resolver and the eval runner (ADR 0013), so its
contract is load-bearing. The headline invariant is DECOUPLING: a missing or
malformed supporting quote must never drop a citation address (Day-7 plan §7-#5).
"""
from __future__ import annotations

from rra.citations import parse_answer


# ─── Address + quote: the happy path ────────────────────────────────────────

def test_citation_with_quote_parsed() -> None:
    prose, triples = parse_answer(
        "SaMD needs validation [72674:3]<q>a risk-based approach</q>. Done."
    )
    assert triples == [("72674", 3, "a risk-based approach")]
    # <q>…</q> is stripped from the user-facing prose; [guid:idx] is kept.
    assert prose == "SaMD needs validation [72674:3]. Done."


def test_guidance_id_with_hyphen_and_underscore() -> None:
    _, triples = parse_answer("Claim [doc-A_1:12]<q>z</q>.")
    assert triples == [("doc-A_1", 12, "z")]


def test_multiple_citations_each_bound_to_own_quote() -> None:
    """Non-greedy capture keeps each quote with its own bracket — a greedy match
    would merge the two quotes and the prose between them."""
    _, triples = parse_answer("A [a:1]<q>q-one</q> and B [b:2]<q>q-two</q>.")
    assert triples == [("a", 1, "q-one"), ("b", 2, "q-two")]


def test_quote_spanning_newline_is_captured() -> None:
    """re.DOTALL lets a quote span a PDF-embedded newline."""
    _, triples = parse_answer("X [a:1]<q>line one\nline two</q>.")
    assert triples == [("a", 1, "line one\nline two")]


# ─── Decoupling: a quote problem must NEVER drop the citation ────────────────

def test_no_quote_citation_survives() -> None:
    """A bare [guid:idx] with no <q> yields quoted_text=None but keeps the address."""
    prose, triples = parse_answer("Claim here [72674:3]. More.")
    assert triples == [("72674", 3, None)]
    assert prose == "Claim here [72674:3]. More."


def test_malformed_unterminated_quote_keeps_citation_and_leaks_no_marker() -> None:
    """An opening <q> with no closing </q> must not drop the citation, and must
    not leak a raw marker into the prose."""
    prose, triples = parse_answer("X [a:1]<q>unterminated quote. Next sentence.")
    assert triples == [("a", 1, None)]
    assert "<q>" not in prose


def test_nested_closing_delimiter_truncates_quote_but_keeps_citation() -> None:
    """A </q> occurring inside quoted content truncates THAT quote to its prefix
    (benign) — the citation address is untouched and no raw marker leaks."""
    prose, triples = parse_answer("X [a:1]<q>see 21 CFR </q> rest</q>.")
    assert triples[0][:2] == ("a", 1)
    assert triples[0][2] == "see 21 CFR"          # prefix only
    assert "<q>" not in prose and "</q>" not in prose


# ─── Empty / whitespace quotes are the honest "no quote" signal (None) ───────

def test_empty_quote_is_none() -> None:
    _, triples = parse_answer("X [a:1]<q></q>.")
    assert triples == [("a", 1, None)]


def test_whitespace_only_quote_is_none() -> None:
    _, triples = parse_answer("X [a:1]<q>   </q>.")
    assert triples == [("a", 1, None)]


def test_quote_envelope_whitespace_trimmed_internal_kept() -> None:
    _, triples = parse_answer("X [a:1]<q>  multi  word span  </q>.")
    assert triples == [("a", 1, "multi  word span")]


# ─── Prose cleaning edge cases ───────────────────────────────────────────────

def test_no_citations_returns_prose_unchanged() -> None:
    prose, triples = parse_answer("Plain prose with no citations at all.")
    assert prose == "Plain prose with no citations at all."
    assert triples == []


def test_whitespace_before_quote_is_consumed() -> None:
    """The leading whitespace before <q> is consumed so no double-space remains."""
    prose, _ = parse_answer("claim [a:1] <q>q</q> next")
    assert prose == "claim [a:1] next"
