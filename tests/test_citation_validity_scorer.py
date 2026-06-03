"""Tests for CitationValidityScorer's two-mode behaviour (ADR 0013).

The scorer picks its mode per citation from quoted_text:
  - None      → key-existence (CI fixture; the unchanged HARD gate, ADR 0012 D2)
  - non-empty → quote faithfulness (golden; ADR 0010 engine)
  - ""         → no quote (golden; counted, EXCLUDED from the mean — the ADR 0012
                D1 analog that stops a shrinking denominator inflating the mean)

A fake verifier stands in for check_citation so these are pure unit tests (no DB).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from rra.evals.dataset import GoldenCase  # noqa: E402
from rra.evals.scorers import AgentResponse, CitationValidityScorer  # noqa: E402


def _case() -> GoldenCase:
    return GoldenCase(
        id="t", difficulty="easy", query="q", product_context="",
        expected_facts=(), expected_guidance_ids=(),
    )


def _resp(citations: list[dict]) -> AgentResponse:
    return AgentResponse(answer_text="a", citations=citations, retrieved_passages=[])


def _fake_verify(g: str, i: int, q: str | None):
    """None → key exists; 'faithful' → substring hit (sim None); 'cov' → coverage
    0.91; anything else → miss at 0.40."""
    if q is None:
        return (True, None)
    if q == "faithful":
        return (True, None)
    if q == "cov":
        return (True, 0.91)
    return (False, 0.40)


# ─── Key-existence mode (CI fixture) — the unchanged gate ────────────────────

def test_key_existence_mode_scores_like_day6() -> None:
    scorer = CitationValidityScorer(_fake_verify)
    r = scorer.score(_case(), _resp([
        {"guidance_id": "a", "chunk_index": 1, "quoted_text": None},
        {"guidance_id": "b", "chunk_index": 2, "quoted_text": None},
    ]))
    assert r.score == 1.0
    assert r.detail["key_existence"] is True
    assert r.detail["no_quote"] == 0


# ─── Faithfulness mode (golden) ──────────────────────────────────────────────

def test_faithfulness_mode_fraction_and_similarity_capture() -> None:
    scorer = CitationValidityScorer(_fake_verify)
    r = scorer.score(_case(), _resp([
        {"guidance_id": "a", "chunk_index": 1, "quoted_text": "faithful"},  # substring
        {"guidance_id": "b", "chunk_index": 2, "quoted_text": "cov"},       # coverage hit
        {"guidance_id": "c", "chunk_index": 3, "quoted_text": "miss"},      # miss
    ]))
    assert abs(r.score - 2 / 3) < 1e-9
    assert r.detail["key_existence"] is False
    # similarity_score recorded per citation for the τ-distribution.
    sims = [f["similarity_score"] for f in r.detail["faithfulness"]]
    assert sims == [None, 0.91, 0.40]


# ─── No-quote guardrail (the D1 analog) ──────────────────────────────────────

def test_no_quote_citation_excluded_from_mean_and_counted() -> None:
    scorer = CitationValidityScorer(_fake_verify)
    r = scorer.score(_case(), _resp([
        {"guidance_id": "a", "chunk_index": 1, "quoted_text": "faithful"},  # assessed
        {"guidance_id": "b", "chunk_index": 2, "quoted_text": ""},          # no quote
    ]))
    # The no-quote citation is NOT in the denominator (else the mean would be 0.5);
    # it is counted separately so write_report can surface it.
    assert r.score == 1.0
    assert r.detail["assessed"] == 1
    assert r.detail["no_quote"] == 1


def test_all_no_quote_is_na_not_zero() -> None:
    """All-no-quote must be N/A (None), never 0.0 — like a zero-citation answer
    (ADR 0012 D1). The no-quote count is what keeps this honest."""
    scorer = CitationValidityScorer(_fake_verify)
    r = scorer.score(_case(), _resp([
        {"guidance_id": "a", "chunk_index": 1, "quoted_text": ""},
        {"guidance_id": "b", "chunk_index": 2, "quoted_text": "   "},
    ]))
    assert r.score is None
    assert r.detail["no_quote"] == 2
    assert r.detail["assessed"] == 0


def test_whitespace_only_quote_counts_as_no_quote() -> None:
    """A whitespace-only quote must never be scored faithful-by-emptiness; it is a
    no-quote, so verify is not even consulted for it."""
    calls = []

    def tracking_verify(g, i, q):
        calls.append(q)
        return (True, None)

    scorer = CitationValidityScorer(tracking_verify)
    r = scorer.score(_case(), _resp([
        {"guidance_id": "a", "chunk_index": 1, "quoted_text": "   "},
    ]))
    assert r.score is None
    assert r.detail["no_quote"] == 1
    assert calls == []  # verifier never called for a no-quote citation


def test_zero_citations_is_na() -> None:
    scorer = CitationValidityScorer(_fake_verify)
    r = scorer.score(_case(), _resp([]))
    assert r.score is None
