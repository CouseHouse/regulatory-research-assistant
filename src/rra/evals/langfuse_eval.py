"""Langfuse eval integration — push the golden set as a dataset and emit each
scorer's result as a score linked to a trace.

Implements future-work §14 / PENDING-DECISIONS Decision 2 (SETTLED 2026-06-03:
Langfuse scores/datasets are pulled into the Day-8 eval-maturation phase, run
*last*, after the critic-delta).

╔══════════════════════════════════════════════════════════════════════════╗
║  POPULATION IS GATED — DO NOT FLIP `POPULATION_GATED` UNTIL THE CRITIC-FLIP ║
╠══════════════════════════════════════════════════════════════════════════╣
║  This module is BUILD-ONLY for now. The primitives below                    ║
║  (push_golden_dataset / emit_scores) are fully implemented and unit-tested  ║
║  against a mocked client, but the wired-in path (`maybe_sync_langfuse`,     ║
║  called from run.py behind --langfuse-sync) REFUSES to populate while       ║
║  POPULATION_GATED is True.                                                  ║
║                                                                            ║
║  Why: today `citation_validity` runs in KEY-EXISTENCE mode (ADR 0010 Day-6 ║
║  baseline / ADR 0012). The eval-maturation day flips the critic to pass    ║
║  the analyst's parsed quote (next-session-plan.md "critic-flip", the small  ║
║  critic.py edit), after which `citation_validity` measures QUOTE            ║
║  FAITHFULNESS instead. Scores pushed to Langfuse now would carry           ║
║  key-existence semantics under the same score name and go stale — and       ║
║  misleading — the moment the critic flips. The required order is            ║
║  matcher-v2 → τ-confirm → critic-flip → critic-delta → Langfuse.            ║
║                                                                            ║
║  To populate (AFTER the critic-flip + critic-delta land): do NOT edit       ║
║  POPULATION_GATED. Pass the per-run --allow-population flag (keys must      ║
║  be present). It opens the gate for ONE invocation only, so there is        ║
║  no committed constant to forget to revert:                                 ║
║      uv run python -m rra.evals.run --langfuse-sync --allow-population      ║
╚══════════════════════════════════════════════════════════════════════════╝

Client reuse: every call here uses the SHARED process-lifetime client from
`rra.tracing.get_langfuse()` (the same singleton api.py uses for request
traces). We never construct a second Langfuse client — see ADR 0015 / tracing.py.

Trace model (run-time parent trace): run_agent (run.py) opens one `eval-case`
span PER CASE and runs the graph INSIDE it (gated by --allow-population), so the
agents' spans nest under a single per-case trace instead of orphaning, and that
trace's id rides out on the response's `raw_trace_id`. `sync_eval_to_langfuse`
attaches each case's scores to its `raw_trace_id` (idempotent by
`{trace_id}-{scorer}`) rather than opening its own span — a post-hoc span would
put the scores on an empty orphan, not on the trace whose children are the agent
spans. A case with no `raw_trace_id` (errored, or produced without the flag) is
skipped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .dataset import GoldenCase
from .scorers import ScoreResult

if TYPE_CHECKING:
    from .run import CaseRun


# ─── Gate ────────────────────────────────────────────────────────────────────
# PERMANENT default — do NOT flip this constant. The gate is opened per-run by
# the --allow-population CLI flag (run.py), which ALSO requires keys present.
# Keeping the safe state as the committed default means there is no constant to
# forget to revert (mirrors the CRITIC_FORCE_VERDICT footgun discipline); the
# wired-in path stays a hard no-op unless --allow-population is passed.
POPULATION_GATED = True

# Dataset name in Langfuse. Stable so re-pushes upsert the same dataset/items
# rather than forking a new copy each run.
GOLDEN_DATASET_NAME = "rra-golden-eval"

DATASET_DESCRIPTION = (
    "RRA golden eval set (evals/golden.jsonl). Inputs: query + product_context. "
    "Expected outputs: expected_facts + expected_guidance_ids. Scored by "
    "citation_validity (gate), key_fact_coverage, position_quality. "
    "NOTE: citation_validity semantics flip key-existence → quote-faithfulness at "
    "the critic-flip (ADR 0010/0012/0013); runs straddling the flip are not "
    "comparable."
)


def should_populate(
    client: Any,
    *,
    allow_population: bool = False,
    gated: bool = POPULATION_GATED,
) -> tuple[bool, str]:
    """Decide whether the wired-in path may write to Langfuse.

    Population requires BOTH (a) Langfuse keys present (client is not None) AND
    (b) the gate open. The gate opens only when the caller passes
    allow_population=True — the per-run --allow-population flag. The flag NEVER
    flips POPULATION_GATED; it opens the gate for one invocation, so the safe
    state stays the committed default with no constant to forget to revert.

    Returns (allowed, reason). Both the disabled and gated outcomes are normal
    no-op paths, not errors — callers print the reason and carry on.
    """
    effective_gate = gated and not allow_population
    if client is None:
        return False, "Langfuse disabled (no public/secret key in settings)"
    if effective_gate:
        return (
            False,
            "population GATED (POPULATION_GATED=True) — pass --allow-population to "
            "open the gate for this run (and ensure Langfuse keys are set)",
        )
    return True, "ok"


# ─── Dataset push (a) ────────────────────────────────────────────────────────


def push_golden_dataset(
    client: Any,
    cases: list[GoldenCase],
    *,
    dataset_name: str = GOLDEN_DATASET_NAME,
) -> int:
    """Upsert the golden set as a Langfuse dataset + one item per case.

    Idempotent: `create_dataset` upserts by name, and each item is keyed by
    `case.id` so re-pushing overwrites in place instead of duplicating
    (langfuse v4 `create_dataset_item` upserts when `id` already exists).

    Returns the number of items pushed.
    """
    client.create_dataset(
        name=dataset_name,
        description=DATASET_DESCRIPTION,
        metadata={
            "source": "evals/golden.jsonl",
            "scorers": ["citation_validity", "key_fact_coverage", "position_quality"],
        },
    )

    pushed = 0
    for case in cases:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=case.id,  # stable id → idempotent upsert
            input={"query": case.query, "product_context": case.product_context},
            expected_output={
                "expected_facts": list(case.expected_facts),
                "expected_guidance_ids": list(case.expected_guidance_ids),
            },
            metadata={"difficulty": case.difficulty, "notes": case.notes},
        )
        pushed += 1
    return pushed


# ─── Score emission (b) ──────────────────────────────────────────────────────


def score_value_and_type(result: ScoreResult) -> tuple[float | str, str]:
    """Map a ScoreResult to a Langfuse (value, data_type) pair.

    A numeric score is NUMERIC. The N/A sentinel (score is None — a
    zero-citation or all-no-quote answer, ADR 0012 D1) is emitted as a
    CATEGORICAL "n/a" rather than dropped, so the N/A stays visible on the
    trace instead of silently vanishing from the denominator.
    """
    if result.score is None:
        return "n/a", "CATEGORICAL"
    return float(result.score), "NUMERIC"


def _score_comment(result: ScoreResult) -> str:
    reason = result.detail.get("reason") if isinstance(result.detail, dict) else None
    if reason:
        return str(reason)
    return f"passed={result.passed}"


def emit_scores(client: Any, trace_id: str | None, scores: list[ScoreResult]) -> int:
    """Emit one Langfuse score per scorer, each linked to `trace_id`.

    Idempotent: every score carries score_id=f"{trace_id}-{result.scorer}", so
    re-running the same eval case UPDATES the score in place instead of appending
    a duplicate to the Scores table (Langfuse "Preventing Duplicate Scores").
    Each scorer name is unique per case, so the key never collides within a run.
    A trace-linked score also appears in the global Scores section automatically —
    there is no separate "write to Scores section" call.

    Returns the number of scores emitted. `passed` and the scorer name ride in
    metadata so a pass/fail view is reconstructable without re-deriving the
    threshold.
    """
    emitted = 0
    for result in scores:
        value, data_type = score_value_and_type(result)
        client.create_score(
            name=result.scorer,
            value=value,
            data_type=data_type,
            trace_id=trace_id,
            score_id=f"{trace_id}-{result.scorer}",
            comment=_score_comment(result),
            metadata={"passed": result.passed, "scorer": result.scorer},
        )
        emitted += 1
    return emitted


# ─── Orchestration ───────────────────────────────────────────────────────────


def sync_eval_to_langfuse(
    client: Any,
    runs: list[CaseRun],
    *,
    dataset_name: str = GOLDEN_DATASET_NAME,
) -> dict[str, Any]:
    """Push the dataset, then attach each case's scores to the per-case trace that
    run_agent opened at run time (`raw_trace_id`).

    Scores land on the SAME trace whose children are the agent spans — NOT on a
    post-hoc orphan span. Assumes the gate has already been cleared by the caller
    (`maybe_sync_langfuse` / `should_populate`) and that the runs were produced
    with --allow-population (so run_agent captured `raw_trace_id`). A case with no
    `raw_trace_id` (errored, or produced without the flag) has no trace to attach
    to and is skipped. Flushes once at the end. Returns a summary dict.
    """
    pushed = push_golden_dataset(client, [r.case for r in runs], dataset_name=dataset_name)

    emitted = 0
    skipped = 0
    for run in runs:
        trace_id = getattr(run.response, "raw_trace_id", None)
        if trace_id is None:
            skipped += 1
            continue
        emitted += emit_scores(client, trace_id, run.scores)

    client.flush()
    return {
        "dataset": dataset_name,
        "items": pushed,
        "cases": len(runs),
        "scores": emitted,
        "skipped_no_trace": skipped,
    }


def maybe_sync_langfuse(
    runs: list[CaseRun],
    *,
    enabled: bool,
    allow_population: bool = False,
    gated: bool = POPULATION_GATED,
) -> dict[str, Any]:
    """Glue called from run.py: fetch the SHARED tracing client, apply the gate,
    and sync only if a sync was requested (`enabled`), the gate is open
    (`allow_population` — the --allow-population flag), and keys are present.

    NEVER raises. Langfuse is auxiliary observability — a tracing outage during
    --langfuse-sync must not turn a green eval red (and must not mask the gate
    result that main() returns right after this call). The disabled/gated/
    not-requested paths return {"synced": False, "reason": ...}; a failure inside
    the populated path is caught and returned the same way, non-fatally.
    """
    if not enabled:
        return {"synced": False, "reason": "not requested (--langfuse-sync off)"}

    from rra.tracing import get_langfuse  # SHARED singleton — never a 2nd client

    client = get_langfuse()
    allowed, reason = should_populate(
        client, allow_population=allow_population, gated=gated
    )
    if not allowed:
        return {"synced": False, "reason": reason}

    try:
        summary = sync_eval_to_langfuse(client, runs)
    except Exception as exc:  # noqa: BLE001 — Langfuse must never fail the eval
        return {"synced": False, "reason": f"langfuse sync failed (non-fatal): {exc}"}
    return {"synced": True, **summary}
