"""Eval runner. Iterates the golden set, calls the agent, applies scorers,
emits a markdown report, and exits non-zero if any GATE scorer fails.

Usage:
    python -m rra.evals.run
    python -m rra.evals.run --difficulty easy        # subset
    python -m rra.evals.run --tag v0.1               # label this run
    python -m rra.evals.run --no-gate                # warn only, never fail
    python -m rra.evals.run --fixture ci --no-llm-judges  # CI gate only

Output:
    evals/results/<timestamp>.md       — full report
    evals/results/latest.md            — symlink to most recent

CI integration:
    The runner exits 1 if any gate scorer fails the threshold, if any case
    errors, or if fewer responses than cases were produced (broken harness
    must not report green — eliminates the all([])-is-True footgun).
    .github/workflows/evals.yml runs this on every PR (--fixture ci --no-llm-judges).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .dataset import GoldenCase, load_golden
from .scorers import (
    AgentResponse,
    CitationValidityScorer,
    KeyFactCoverageScorer,
    PositionQualityScorer,
    ScoreResult,
    Scorer,
)

RESULTS_DIR = Path(__file__).resolve().parents[3] / "evals" / "results"
CI_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "ci_key_fixture.jsonl"
CI_VALID_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "evals" / "fixtures" / "ci_valid_fixture.jsonl"

# Baseline label embedded in every report (ADR 0012 P1/P2).
_BASELINE_LABEL = (
    "**Baseline label:** key-existence only (ADR 0010 Day 6 — chunk address "
    "resolution, not quote faithfulness). Do not compare Day 6 numbers to Day 7+ "
    "without re-reading ADR 0010 and ADR 0012 P2."
)


@dataclass
class CaseRun:
    case: GoldenCase
    response: AgentResponse | None
    scores: list[ScoreResult]
    error: str | None = None


# ─── Agent invocation ───────────────────────────────────────────────────────

def run_agent(case: GoldenCase) -> AgentResponse:
    """Call the real LangGraph graph and return an AgentResponse.

    Reads GraphState keys: draft (answer text), passages (retrieved docs),
    trace_id (Langfuse trace). Citations are parsed from the raw draft text
    via _parse_citation_pairs — NOT from any post-resolution key in GraphState
    (there is none). This is the pre-resolution tap: catches hallucinated keys.
    """
    from rra.graph import run_graph
    from rra.api import _parse_citation_pairs

    result = run_graph({
        "query": case.query,
        "product_context": case.product_context,
        "session_id": str(uuid.uuid4()),
    })

    draft: str = result.get("draft", "")
    raw_pairs = _parse_citation_pairs(draft)
    citations = [{"guidance_id": g, "chunk_index": i} for g, i in raw_pairs]
    passages = [p.model_dump() for p in result.get("passages", [])]

    return AgentResponse(
        answer_text=draft,
        citations=citations,
        retrieved_passages=passages,
        raw_trace_id=result.get("trace_id"),
    )


def _make_ci_response(case: GoldenCase) -> AgentResponse:
    """Build AgentResponse directly from fixture citations without invoking the graph.

    Used for --fixture ci: the case's ci_citations field carries pre-built
    (guidance_id, chunk_index) pairs. No API calls, no embeddings.
    """
    citations = [{"guidance_id": g, "chunk_index": i} for g, i in case.ci_citations]
    return AgentResponse(
        answer_text="[CI fixture — no graph invocation]",
        citations=citations,
        retrieved_passages=[],
        raw_trace_id=None,
    )


# ─── Corpus resolver for the deterministic scorer ───────────────────────────

def make_resolver():
    """Returns resolves(guidance_id, chunk_index) -> bool.

    Calls check_citation in key-existence mode (quoted_text=None) — verifies
    that the (guidance_id, chunk_index) pair resolves to a real corpus.chunks
    row. True iff the row exists. DB failures propagate as ToolError (not
    caught here; let them surface so CI fails loudly on infra problems).
    """
    from rra.mcp_server.tools import check_citation

    def resolves(guidance_id: str, chunk_index: int) -> bool:
        result = check_citation("eval", guidance_id, chunk_index, quoted_text=None)
        return result.verified

    return resolves


# ─── Runner ─────────────────────────────────────────────────────────────────

def run_eval(
    scorers: list[Scorer],
    cases: list[GoldenCase],
    tag: str = "",
    enforce_gates: bool = True,
    use_ci_fixture: bool = False,
) -> tuple[list[CaseRun], bool]:
    """Returns (runs, all_gates_passed).

    Gate hardening (ADR 0012 D2):
    - Any error → gate fails (broken harness must not report green)
    - Fewer responses than cases → gate fails
    - No gate scorer produced a non-None result → gate fails (all([])-is-True killed)
    - Otherwise: gate fails iff any gate scorer has passed=False on a non-N/A result
    """
    runs: list[CaseRun] = []
    for case in cases:
        try:
            response = _make_ci_response(case) if use_ci_fixture else run_agent(case)
        except Exception as e:
            runs.append(CaseRun(case=case, response=None, scores=[], error=str(e)))
            continue

        scores = [s.score(case, response) for s in scorers]
        runs.append(CaseRun(case=case, response=response, scores=scores))

    if not enforce_gates:
        return runs, True

    error_count = sum(1 for r in runs if r.error is not None)
    scored_count = sum(1 for r in runs if r.response is not None)

    if error_count > 0:
        return runs, False
    if scored_count < len(cases):
        return runs, False

    gate_scorer_names = {s.name for s in scorers if s.gate}
    gate_results = [
        s
        for r in runs
        if r.response is not None
        for s in r.scores
        if s.scorer in gate_scorer_names and s.score is not None
    ]

    if not gate_results:
        # No gate scorer produced a scoreable result — harness is broken or all N/A.
        return runs, False

    all_passed = all(s.passed for s in gate_results)
    return runs, all_passed


# ─── Reporting ──────────────────────────────────────────────────────────────

def write_report(runs: list[CaseRun], scorers: list[Scorer], tag: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}{('-' + tag) if tag else ''}.md"
    path = RESULTS_DIR / name

    error_count = sum(1 for r in runs if r.error is not None)
    scored = [r for r in runs if r.response is not None]
    zero_citation_count = sum(
        1 for r in scored if r.response is not None and not r.response.citations
    )

    lines = [
        f"# Eval run — {ts}",
        f"Tag: `{tag or '(none)'}`",
        "",
        _BASELINE_LABEL,
        "",
        f"**Cases:** {len(runs)}  "
        f"**Scored:** {len(scored)}  "
        f"**Errors:** {error_count}",
        f"**Zero-citation answers:** {zero_citation_count} of {len(runs)} "
        f"(excluded from citation_validity mean per ADR 0012 D1).",
        "",
        "## Aggregate scores",
        "",
        "| Scorer | Mean | Pass rate | Gate | Threshold |",
        "|---|---|---|---|---|",
    ]

    for scorer in scorers:
        vals = [
            s.score
            for r in scored
            for s in r.scores
            if s.scorer == scorer.name and s.score is not None
        ]
        passes = sum(
            1 for r in scored for s in r.scores
            if s.scorer == scorer.name and s.passed and s.score is not None
        )
        denom = len(vals)
        mean_str = f"{sum(vals) / denom:.3f}" if denom else "N/A"
        pass_rate_str = f"{passes / denom:.1%}" if denom else "N/A"
        lines.append(
            f"| {scorer.name} | {mean_str} | {pass_rate_str} | "
            f"{'**HARD**' if scorer.gate else 'warn'} | {scorer.threshold} |"
        )

    lines.extend(["", "## Per-case detail", ""])
    for r in runs:
        lines.append(f"### `{r.case.id}` ({r.case.difficulty})")
        lines.append(f"> {r.case.query}")
        if r.error:
            lines.append(f"\n**ERROR:** {r.error}\n")
            continue
        for s in r.scores:
            mark = "✅" if s.passed else ("⬜" if s.score is None else "❌")
            score_str = "N/A" if s.score is None else f"{s.score:.3f}"
            lines.append(f"- {mark} **{s.scorer}**: {score_str}")
        lines.append("")

    path.write_text("\n".join(lines))

    latest = RESULTS_DIR / "latest.md"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(path.name)

    return path


# ─── CLI ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    parser.add_argument("--tag", default="")
    parser.add_argument("--no-gate", action="store_true")
    parser.add_argument(
        "--fixture",
        choices=["golden", "ci", "ci-valid"],
        default="golden",
        help=(
            "golden = full 30-case set; "
            "ci = key fixture with valid + bogus cases (gate-bite demo); "
            "ci-valid = valid cases only (merge gate, always exits 0 on healthy work)"
        ),
    )
    parser.add_argument(
        "--no-llm-judges",
        action="store_true",
        help="Skip KeyFactCoverageScorer and PositionQualityScorer. Use for CI (no API key needed).",
    )
    args = parser.parse_args()

    use_ci_fixture = args.fixture in ("ci", "ci-valid")
    fixture_path = (
        CI_VALID_FIXTURE_PATH if args.fixture == "ci-valid"
        else CI_FIXTURE_PATH if args.fixture == "ci"
        else None
    )

    if fixture_path is not None:
        cases = load_golden(fixture_path)
    else:
        cases = load_golden()

    if args.difficulty:
        cases = [c for c in cases if c.difficulty == args.difficulty]

    from rra.config import settings
    from .judge import judge_call

    resolver = make_resolver()
    scorers: list[Scorer] = [CitationValidityScorer(resolver)]

    if not args.no_llm_judges:
        scorers.append(KeyFactCoverageScorer(judge_call, model=settings.key_fact_judge_model))
        scorers.append(PositionQualityScorer(judge_call, model=settings.position_judge_model))

    runs, gates_passed = run_eval(
        scorers,
        cases,
        tag=args.tag,
        enforce_gates=not args.no_gate,
        use_ci_fixture=use_ci_fixture,
    )
    report_path = write_report(runs, scorers, args.tag)
    print(f"Report: {report_path}")

    return 0 if gates_passed else 1


if __name__ == "__main__":
    sys.exit(main())
