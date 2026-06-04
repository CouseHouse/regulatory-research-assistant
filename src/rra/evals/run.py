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
import contextlib
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

def run_agent(case: GoldenCase, *, trace_to_langfuse: bool = False) -> AgentResponse:
    """Call the real LangGraph graph and return an AgentResponse.

    Reads GraphState keys: draft (answer text), passages (retrieved docs).
    Citations AND their analyst-emitted supporting quotes are parsed from the raw
    draft via the SHARED parse_answer (ADR 0013) — the same parser the API
    resolver uses, so the measurement and the product surface cannot drift (plan
    §7-#5). This is the pre-resolution tap: it reads the analyst's own output,
    catching hallucinated keys.

    Each citation carries quoted_text — the analyst's verbatim span, or "" when
    the analyst emitted no quote. The parser's None is mapped to "" here so a
    golden no-quote ("") is distinguishable from the CI fixture's None (which
    means key-existence mode); the scorer treats the two differently. answer_text
    is the user-facing prose (clean_prose, <q> envelopes stripped) so the LLM
    judges grade exactly what the API returns, not raw markup.

    Langfuse parenting (trace_to_langfuse=True, set by --allow-population): the
    graph runs INSIDE one per-case "eval-case" span so the agents' own spans
    (planner/researcher/analyst/critic/retrieval) nest under a single trace
    instead of each flushing as an orphan root (the confirmed bug). The session
    is propagated onto the parent AND every child via propagate_attributes, and
    the captured trace_id rides out on raw_trace_id for the Phase-4 score writes
    and Phase-5 dataset-run linkage. With the flag off (default) or keys absent,
    the graph runs exactly as before with raw_trace_id=None — no Langfuse side
    effect (mirrors the gate philosophy: no Langfuse writes without the opt-in).
    """
    from rra.graph import run_graph
    from rra.citations import parse_answer
    from rra.tracing import get_langfuse

    session_id = str(uuid.uuid4())
    lf = get_langfuse() if trace_to_langfuse else None

    span = None
    trace_id: str | None = None
    with contextlib.ExitStack() as stack:
        if lf is not None:
            from langfuse import propagate_attributes

            # propagate_attributes FIRST so the eval-case span — and every agent
            # span opened inside run_graph — starts within the session context.
            stack.enter_context(propagate_attributes(session_id=session_id))
            span = stack.enter_context(
                lf.start_as_current_observation(
                    name="eval-case",
                    as_type="span",
                    input={"query": case.query, "product_context": case.product_context},
                    metadata={"case_id": case.id, "difficulty": case.difficulty},
                )
            )
            trace_id = lf.get_current_trace_id()

        result = run_graph({
            "query": case.query,
            "product_context": case.product_context,
            "session_id": session_id,
        })

        draft: str = result.get("draft", "")
        clean_prose, triples = parse_answer(draft)

        if span is not None:
            span.update(
                output={
                    "answer_preview": clean_prose[:200],
                    "citation_count": len(triples),
                }
            )

    citations = [
        {"guidance_id": g, "chunk_index": i, "quoted_text": q or ""}
        for g, i, q in triples
    ]
    passages = [p.model_dump() for p in result.get("passages", [])]

    return AgentResponse(
        answer_text=clean_prose,
        citations=citations,
        retrieved_passages=passages,
        raw_trace_id=trace_id,
    )


def _make_ci_response(case: GoldenCase) -> AgentResponse:
    """Build AgentResponse directly from fixture citations without invoking the graph.

    Used for --fixture ci: the case's ci_citations field carries pre-built
    (guidance_id, chunk_index) pairs. No API calls, no embeddings.

    quoted_text=None on every citation keeps the CI gate in KEY-EXISTENCE mode
    (ADR 0012 D2): deterministic, fixture-keyed, zero-API. The fixture has no
    quotes, so faithfulness is never (and must never be) computed here.
    """
    citations = [
        {"guidance_id": g, "chunk_index": i, "quoted_text": None}
        for g, i in case.ci_citations
    ]
    return AgentResponse(
        answer_text="[CI fixture — no graph invocation]",
        citations=citations,
        retrieved_passages=[],
        raw_trace_id=None,
    )


# ─── Corpus verifier for the deterministic scorer ───────────────────────────

def make_verifier():
    """Returns verify(guidance_id, chunk_index, quoted_text) -> (verified, similarity_score).

    Wraps check_citation, selecting its mode by quoted_text (ADR 0013):
      - quoted_text=None  → KEY-EXISTENCE (CI gate; ADR 0012 D2): verified iff the
        (guidance_id, chunk_index) row exists. similarity_score is None.
      - quoted_text=<str> → ADR-0010 three-step QUOTE MATCHING (faithfulness):
        returns (verified, similarity_score) — the score feeds τ-calibration.

    DB failures propagate as ToolError (not caught here; let them surface so CI
    fails loudly on infra problems).
    """
    from rra.mcp_server.tools import check_citation

    def verify(
        guidance_id: str, chunk_index: int, quoted_text: str | None
    ) -> tuple[bool, float | None]:
        result = check_citation(
            "eval", guidance_id, chunk_index, quoted_text=quoted_text
        )
        return result.verified, result.similarity_score

    return verify


# ─── Runner ─────────────────────────────────────────────────────────────────

def run_eval(
    scorers: list[Scorer],
    cases: list[GoldenCase],
    tag: str = "",
    enforce_gates: bool = True,
    use_ci_fixture: bool = False,
    trace_to_langfuse: bool = False,
) -> tuple[list[CaseRun], bool]:
    """Returns (runs, all_gates_passed).

    trace_to_langfuse (from --allow-population) is forwarded to run_agent so each
    case's graph runs under one eval-case Langfuse trace (orphan fix); it has no
    effect on the CI-fixture path, which never invokes the graph.

    Gate hardening (ADR 0012 D2):
    - Any error → gate fails (broken harness must not report green)
    - Fewer responses than cases → gate fails
    - No gate scorer produced a non-None result → gate fails (all([])-is-True killed)
    - Otherwise: gate fails iff any gate scorer has passed=False on a non-N/A result
    """
    runs: list[CaseRun] = []
    for case in cases:
        try:
            response = (
                _make_ci_response(case)
                if use_ci_fixture
                else run_agent(case, trace_to_langfuse=trace_to_langfuse)
            )
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

def _faithfulness_summary(runs: list[CaseRun]) -> dict:
    """Aggregate the quote-faithfulness signal from citation_validity details.

    Surfaces the no-quote guardrail count (ADR 0012 D1 analog) and the
    similarity_score distribution that τ-calibration needs (ADR 0013; plan §4-§5).
    A faithfulness record is {"similarity_score": float|None, "verified": bool}:
      - sim is None, verified      → Step-2 substring hit
      - sim is None, not verified  → key not found (hallucinated address)
      - sim is a float             → Step-3 coverage-ratio score (verified iff ≥ τ)
    """
    no_quote = 0
    substring_verified = 0
    key_not_found = 0
    coverage_sims: list[float] = []
    coverage_verified = 0
    for r in runs:
        if r.response is None:
            continue
        for s in r.scores:
            if s.scorer != "citation_validity":
                continue
            no_quote += s.detail.get("no_quote", 0)
            for f in s.detail.get("faithfulness", []):
                sim = f["similarity_score"]
                if sim is None:
                    if f["verified"]:
                        substring_verified += 1
                    else:
                        key_not_found += 1
                else:
                    coverage_sims.append(sim)
                    if f["verified"]:
                        coverage_verified += 1
    return {
        "no_quote": no_quote,
        "substring_verified": substring_verified,
        "key_not_found": key_not_found,
        "coverage_sims": coverage_sims,
        "coverage_verified": coverage_verified,
    }


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
    faith = _faithfulness_summary(runs)

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
        f"**Citations with NO analyst quote:** {faith['no_quote']} "
        f"(excluded from the citation_validity faithfulness mean — ADR 0012 D1 "
        f"analog: a shrinking denominator must not inflate the mean).",
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

    # ── Quote-faithfulness distribution (ADR 0013 — τ-calibration data) ──────
    from rra.config import settings

    tau = settings.citation_match_threshold
    sims = faith["coverage_sims"]
    total_assessed_quotes = (
        faith["substring_verified"] + faith["key_not_found"] + len(sims)
    )

    lines.extend(["", "## Quote-faithfulness (ADR 0013 — τ-calibration data)", ""])
    if total_assessed_quotes == 0 and faith["no_quote"] == 0:
        lines.append(
            "_No quote-faithfulness signal in this run (key-existence fixture — "
            "ADR 0012 D2). Faithfulness is produced only on golden / out-of-band runs._"
        )
    else:
        verified_quotes = faith["substring_verified"] + faith["coverage_verified"]
        pct = (
            f"{verified_quotes / total_assessed_quotes:.1%}"
            if total_assessed_quotes
            else "N/A"
        )
        lines.extend([
            f"**τ (citation_match_threshold):** {tau} — sims ≥ τ count as verified.",
            f"**Quotes assessed:** {total_assessed_quotes}  ·  "
            f"**Verified faithful:** {verified_quotes} ({pct})  ·  "
            f"**No analyst quote (excluded):** {faith['no_quote']}",
            "",
            "Match-path breakdown:",
            f"- Substring hit (Step 2, exact after whitespace-normalize): "
            f"{faith['substring_verified']}",
            f"- Coverage-ratio scored (Step 3): {len(sims)} "
            f"(verified ≥ τ: {faith['coverage_verified']})",
            f"- Key not found (hallucinated address): {faith['key_not_found']}",
            "",
            "similarity_score distribution — Step-3 coverage path:",
            "| Band | Count |",
            "|---|---|",
            f"| [0.00, 0.50) | {sum(1 for x in sims if x < 0.50)} |",
            f"| [0.50, 0.70) | {sum(1 for x in sims if 0.50 <= x < 0.70)} |",
            f"| [0.70, 0.85) | {sum(1 for x in sims if 0.70 <= x < 0.85)} |",
            f"| [0.85, 1.00] | {sum(1 for x in sims if x >= 0.85)} |",
            "",
            "_The 0.50–0.85 bands are the boilerplate-seam suspects flagged in the "
            "Day-7 plan §4 (honest quotes split by a mid-chunk header). Do NOT lower "
            "τ to absorb them — clean the corpus (Priority 3) first, then calibrate._",
        ])
        if sims:
            lines.append(
                f"_Raw coverage sims (sorted): "
                f"{', '.join(f'{x:.3f}' for x in sorted(sims))}_"
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
            extra = ""
            if s.scorer == "citation_validity":
                assessed = s.detail.get("assessed")
                if assessed is not None:
                    extra = f"  _(assessed {assessed}, no-quote {s.detail.get('no_quote', 0)})_"
            lines.append(f"- {mark} **{s.scorer}**: {score_str}{extra}")
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
    parser.add_argument(
        "--langfuse-sync",
        action="store_true",
        help=(
            "Push the golden set as a Langfuse dataset and emit each scorer's "
            "result as a score linked to a per-case trace. GATED: a hard no-op "
            "unless --allow-population is ALSO passed (see rra.evals.langfuse_eval)."
        ),
    )
    parser.add_argument(
        "--allow-population",
        action="store_true",
        help=(
            "Open the population gate for THIS invocation only (also requires "
            "Langfuse keys). POPULATION_GATED stays True as the committed default "
            "— this flag never flips it, so there is no constant to forget to "
            "revert. Implies --langfuse-sync; without it, population is a no-op."
        ),
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

    verifier = make_verifier()
    scorers: list[Scorer] = [CitationValidityScorer(verifier)]

    if not args.no_llm_judges:
        scorers.append(KeyFactCoverageScorer(judge_call, model=settings.key_fact_judge_model))
        scorers.append(PositionQualityScorer(judge_call, model=settings.position_judge_model))

    runs, gates_passed = run_eval(
        scorers,
        cases,
        tag=args.tag,
        enforce_gates=not args.no_gate,
        use_ci_fixture=use_ci_fixture,
        # Open per-case eval-case traces only when populating (keys handled by
        # get_langfuse() returning None when absent) — same opt-in as the gate.
        trace_to_langfuse=args.allow_population,
    )
    report_path = write_report(runs, scorers, args.tag)
    print(f"Report: {report_path}")

    # Optional Langfuse sync (dataset push + per-case scores). GATED: a no-op
    # unless --allow-population is passed (and Langfuse keys are present) — see
    # rra.evals.langfuse_eval. The gate/disabled/not-requested paths return
    # synced=False without touching Langfuse, so this never affects a normal run
    # or the CI gate below.
    from .langfuse_eval import maybe_sync_langfuse

    # --allow-population is the single per-run opt-in: it implies a sync request
    # AND opens the gate (which still requires keys). --langfuse-sync alone stays
    # a gated no-op so an existing invocation can't suddenly start publishing.
    sync_requested = args.langfuse_sync or args.allow_population
    sync_status = maybe_sync_langfuse(
        runs,
        enabled=sync_requested,
        allow_population=args.allow_population,
    )
    if sync_requested:
        print(f"Langfuse sync: {sync_status}")

    return 0 if gates_passed else 1


if __name__ == "__main__":
    sys.exit(main())
