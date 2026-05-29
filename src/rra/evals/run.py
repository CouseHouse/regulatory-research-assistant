"""Eval runner. Iterates the golden set, calls the agent, applies scorers,
emits a markdown report, and exits non-zero if any GATE scorer fails.

Usage:
    python -m rra.evals.run
    python -m rra.evals.run --difficulty easy        # subset
    python -m rra.evals.run --tag v0.1               # label this run
    python -m rra.evals.run --no-gate                # warn only, never fail

Output:
    evals/results/<timestamp>.md       — full report
    evals/results/latest.md            — symlink to most recent

CI integration:
    The runner exits 1 if any gate scorer fails the threshold.
    `.github/workflows/evals.yml` (TODO) runs this on every PR.
"""

from __future__ import annotations

import argparse
import json
import sys
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


@dataclass
class CaseRun:
    case: GoldenCase
    response: AgentResponse | None
    scores: list[ScoreResult]
    error: str | None = None


# ─── Agent invocation ───────────────────────────────────────────────────────

def run_agent(case: GoldenCase) -> AgentResponse:
    """Call the agent under test. Day 6: import and call the real graph.
    Day 1 stub: return a placeholder so the runner shape is testable."""

    # TODO(day 6):
    #   from rra.graph import build_graph
    #   graph = build_graph()
    #   result = graph.invoke({
    #       "query": case.query,
    #       "product_context": case.product_context,
    #   })
    #   return AgentResponse(
    #       answer_text=result["final_answer"],
    #       citations=result["citations"],
    #       retrieved_passages=result["passages"],
    #       raw_trace_id=result.get("langfuse_trace_id"),
    #   )
    raise NotImplementedError("Wire up graph invocation on day 6")


# ─── Corpus lookup for the deterministic scorer ─────────────────────────────

def make_corpus_lookup():
    """Returns a function (guidance_id) -> full text | None.
    Day 1 stub: in-memory dict. Day 2+: query Postgres."""

    # TODO(day 2): swap to Postgres query
    #   def lookup(gid):
    #       with get_conn() as c:
    #           rows = c.execute("SELECT text FROM corpus.chunks WHERE guidance_id=%s ORDER BY chunk_index", [gid]).fetchall()
    #       return "".join(r[0] for r in rows) if rows else None
    raise NotImplementedError("Wire up Postgres lookup on day 2")


# ─── Runner ─────────────────────────────────────────────────────────────────

def run_eval(
    scorers: list[Scorer],
    cases: list[GoldenCase],
    tag: str = "",
    enforce_gates: bool = True,
) -> tuple[list[CaseRun], bool]:
    """Returns (runs, all_gates_passed)."""
    runs: list[CaseRun] = []
    for case in cases:
        try:
            response = run_agent(case)
        except Exception as e:
            runs.append(CaseRun(case=case, response=None, scores=[], error=str(e)))
            continue

        scores = [s.score(case, response) for s in scorers]
        runs.append(CaseRun(case=case, response=response, scores=scores))

    all_passed = all(
        s.passed
        for r in runs
        if r.response is not None
        for s, scorer in zip(r.scores, scorers)
        if scorer.gate
    )
    return runs, all_passed if enforce_gates else True


# ─── Reporting ──────────────────────────────────────────────────────────────

def write_report(runs: list[CaseRun], scorers: list[Scorer], tag: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{ts}{('-' + tag) if tag else ''}.md"
    path = RESULTS_DIR / name

    lines = [
        f"# Eval run — {ts}",
        f"Tag: `{tag or '(none)'}`",
        f"Cases: {len(runs)}  Errors: {sum(1 for r in runs if r.error)}",
        "",
        "## Aggregate scores",
        "",
        "| Scorer | Mean | Pass rate | Gate | Threshold |",
        "|---|---|---|---|---|",
    ]
    for scorer in scorers:
        scored = [r for r in runs if r.response is not None]
        vals = [
            next((s.score for s in r.scores if s.scorer == scorer.name), None)
            for r in scored
        ]
        vals = [v for v in vals if v is not None]
        passes = sum(
            1 for r in scored for s in r.scores if s.scorer == scorer.name and s.passed
        )
        mean = sum(vals) / len(vals) if vals else 0.0
        pass_rate = passes / len(scored) if scored else 0.0
        lines.append(
            f"| {scorer.name} | {mean:.3f} | {pass_rate:.1%} | "
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
            mark = "✅" if s.passed else "❌"
            lines.append(f"- {mark} **{s.scorer}**: {s.score:.3f}")
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
    args = parser.parse_args()

    cases = load_golden()
    if args.difficulty:
        cases = [c for c in cases if c.difficulty == args.difficulty]

    # TODO(day 6): construct judge client + corpus lookup
    # judge_client = AnthropicJudge(...)
    # corpus_lookup = make_corpus_lookup()
    # scorers: list[Scorer] = [
    #     CitationValidityScorer(corpus_lookup),
    #     KeyFactCoverageScorer(judge_client, model=os.environ["JUDGE_MODEL"]),
    #     PositionQualityScorer(judge_client, model=os.environ["ANALYST_MODEL"]),
    # ]
    scorers: list[Scorer] = []  # placeholder until day 6

    runs, gates_passed = run_eval(
        scorers, cases, tag=args.tag, enforce_gates=not args.no_gate
    )
    report_path = write_report(runs, scorers, args.tag)
    print(f"Report: {report_path}")

    return 0 if gates_passed else 1


if __name__ == "__main__":
    sys.exit(main())
