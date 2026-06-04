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
║  To populate (AFTER the critic-flip + critic-delta land): set              ║
║  POPULATION_GATED = False in this file, in the same commit as the          ║
║  critic-flip's eval-maturation work, then run:                             ║
║      uv run python -m rra.evals.run --langfuse-sync                         ║
╚══════════════════════════════════════════════════════════════════════════╝

Client reuse: every call here uses the SHARED process-lifetime client from
`rra.tracing.get_langfuse()` (the same singleton api.py uses for request
traces). We never construct a second Langfuse client — see ADR 0015 / tracing.py.

Trace model (post-hoc eval record): during an eval run the graph is invoked
WITHOUT a trace_id (run_agent builds its own initial state), so no request
trace exists to attach scores to. `sync_eval_to_langfuse` therefore opens one
`eval-case` span per case — mirroring the api.py idiom
(`start_as_current_observation(as_type="span")` + `get_current_trace_id()`) —
and attaches that case's scores to the span's trace. If a response ever carries
a `raw_trace_id` (e.g. a future eval that threads a trace into the graph), it is
cross-referenced in the span metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .dataset import GoldenCase
from .scorers import ScoreResult

if TYPE_CHECKING:
    from .run import CaseRun


# ─── Gate ────────────────────────────────────────────────────────────────────
# Flip to False ONLY in the eval-maturation commit that lands the critic-flip.
# See the module banner above for the full rationale. Until then, the wired-in
# path is a hard no-op so a stray --langfuse-sync cannot publish stale scores.
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


def should_populate(client: Any, *, gated: bool = POPULATION_GATED) -> tuple[bool, str]:
    """Decide whether the wired-in path may write to Langfuse.

    Returns (allowed, reason). Both the disabled and gated outcomes are normal
    no-op paths, not errors — callers print the reason and carry on.
    """
    if client is None:
        return False, "Langfuse disabled (no public/secret key in settings)"
    if gated:
        return (
            False,
            "population GATED until the critic-flip (POPULATION_GATED=True in "
            "langfuse_eval.py) — pre-flip key-existence scores would go stale",
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
    """Push the dataset, then open one eval-case span per run and attach scores.

    Assumes the gate has already been cleared by the caller (`maybe_sync_langfuse`
    / `should_populate`). Flushes once at the end so the background sender drains
    before the process exits. Returns a summary dict for the CLI to print.
    """
    pushed = push_golden_dataset(client, [r.case for r in runs], dataset_name=dataset_name)

    emitted = 0
    for run in runs:
        with client.start_as_current_observation(
            name="eval-case",
            as_type="span",
            input={
                "query": run.case.query,
                "product_context": run.case.product_context,
            },
            metadata={
                "case_id": run.case.id,
                "difficulty": run.case.difficulty,
                "dataset": dataset_name,
                # Cross-ref the graph's own trace if a future eval ever threads one.
                "graph_trace_id": getattr(run.response, "raw_trace_id", None),
            },
        ) as span:
            trace_id = client.get_current_trace_id()
            if run.error:
                span.update(output={"error": run.error})
            else:
                answer = run.response.answer_text if run.response is not None else ""
                citation_count = len(run.response.citations) if run.response is not None else 0
                span.update(
                    output={
                        "answer_preview": answer[:200],
                        "citation_count": citation_count,
                    }
                )
            emitted += emit_scores(client, trace_id, run.scores)

    client.flush()
    return {
        "dataset": dataset_name,
        "items": pushed,
        "cases": len(runs),
        "scores": emitted,
    }


def maybe_sync_langfuse(
    runs: list[CaseRun],
    *,
    enabled: bool,
    gated: bool = POPULATION_GATED,
) -> dict[str, Any]:
    """Glue called from run.py: fetch the SHARED tracing client, apply the gate,
    and sync only if both `enabled` (the --langfuse-sync flag) and the gate allow.

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
    allowed, reason = should_populate(client, gated=gated)
    if not allowed:
        return {"synced": False, "reason": reason}

    try:
        summary = sync_eval_to_langfuse(client, runs)
    except Exception as exc:  # noqa: BLE001 — Langfuse must never fail the eval
        return {"synced": False, "reason": f"langfuse sync failed (non-fatal): {exc}"}
    return {"synced": True, **summary}
