"""Tests for the eval report's quote-faithfulness guardrail (ADR 0013 / plan §8).

The report MUST surface the no-quote count prominently — without it, an analyst
that degrades to emitting no quotes would show a falsely-rising faithfulness mean
as the denominator shrinks (the ADR 0012 D1 hiding-place failure). These tests
lock that line and the τ-distribution section. No DB; report written to tmp_path.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

import rra.evals.run as run  # noqa: E402
from rra.evals.dataset import GoldenCase  # noqa: E402
from rra.evals.run import CaseRun, _faithfulness_summary, write_report  # noqa: E402
from rra.evals.scorers import AgentResponse, CitationValidityScorer, ScoreResult  # noqa: E402


def _case(i: int) -> GoldenCase:
    return GoldenCase(
        id=f"g-{i}", difficulty="easy", query="q", product_context="",
        expected_facts=(), expected_guidance_ids=(),
    )


def _cv(detail: dict, score, passed: bool) -> ScoreResult:
    return ScoreResult("citation_validity", score, passed, detail)


def _faithful_run() -> CaseRun:
    detail = {
        "valid": 2, "assessed": 3, "no_quote": 1, "key_existence": False,
        "faithfulness": [
            {"similarity_score": None, "verified": True},   # substring hit
            {"similarity_score": 0.91, "verified": True},   # coverage hit
            {"similarity_score": 0.42, "verified": False},  # coverage miss
        ],
        "invalid": [],
    }
    resp = AgentResponse("a", [{"guidance_id": "x", "chunk_index": 1, "quoted_text": "q"}], [])
    return CaseRun(_case(1), resp, [_cv(detail, 2 / 3, False)])


def _all_no_quote_run() -> CaseRun:
    detail = {
        "valid": 0, "assessed": 0, "no_quote": 2, "key_existence": False,
        "faithfulness": [], "invalid": [], "reason": "all no-quote",
    }
    resp = AgentResponse("a", [{"guidance_id": "y", "chunk_index": 1, "quoted_text": ""}], [])
    return CaseRun(_case(2), resp, [_cv(detail, None, False)])


def test_faithfulness_summary_aggregates_and_categorizes() -> None:
    summary = _faithfulness_summary([_faithful_run(), _all_no_quote_run()])
    assert summary["no_quote"] == 3                # 1 + 2 across runs
    assert summary["substring_verified"] == 1
    assert summary["coverage_sims"] == [0.91, 0.42]
    assert summary["coverage_verified"] == 1
    assert summary["key_not_found"] == 0


def test_report_surfaces_no_quote_count_prominently(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path)
    scorer = CitationValidityScorer(lambda *a: (True, None))
    path = write_report([_faithful_run(), _all_no_quote_run()], [scorer], tag="t")
    report = path.read_text()

    # The prominent guardrail line (ADR 0012 D1 analog): 3 no-quote citations.
    assert "Citations with NO analyst quote:** 3" in report
    # The τ-distribution section exists and reports assessed/verified counts.
    assert "Quote-faithfulness (ADR 0013" in report
    assert "Quotes assessed:** 3" in report
    # The coverage miss (0.42) lands in the lowest band; the hit (0.91) in the top.
    assert "| [0.00, 0.50) | 1 |" in report
    assert "| [0.85, 1.00] | 1 |" in report


def test_report_key_existence_run_shows_no_faithfulness_signal(monkeypatch, tmp_path) -> None:
    """A CI-style key-existence run has no faithfulness signal — the report says so
    rather than printing an empty distribution."""
    monkeypatch.setattr(run, "RESULTS_DIR", tmp_path)
    detail = {"valid": 1, "assessed": 1, "no_quote": 0, "key_existence": True,
              "faithfulness": [], "invalid": []}
    resp = AgentResponse("a", [{"guidance_id": "z", "chunk_index": 1, "quoted_text": None}], [])
    runrec = CaseRun(_case(3), resp, [_cv(detail, 1.0, True)])
    scorer = CitationValidityScorer(lambda *a: (True, None))
    report = write_report([runrec], [scorer], tag="ci").read_text()

    assert "Citations with NO analyst quote:** 0" in report
    assert "No quote-faithfulness signal in this run" in report
