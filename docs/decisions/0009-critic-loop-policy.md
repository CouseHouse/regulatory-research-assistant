# 0009 — Critic-loop policy: hard cap 2, edit-in-place, escalate-on-corpus-gap

**Status:** Active
**Date:** 2026-06-01
**Owner:** Butters

## Context

The critic-revision loop (spec §3.1) needs a concrete termination policy. Three choices are contested: how many revisions before forced exit, whether the analyst re-synthesizes from scratch or edits in place, and what "escalate" means vs. "revise." Without explicit policy, the loop could run indefinitely, discard good drafts, or blur the distinction between a fixable citation error and a corpus coverage gap.

## Decision

The critic emits one of three verdicts; each maps to a specific action:

- **`approve`** — All citations valid and grounded. Exit graph immediately.
- **`revise`** — One or more citations are wrong or overclaimed, but the draft is fixable. Route back to analyst with structured notes. Analyst edits in place (does not re-synthesize). Hard cap: after 2 revisions, exit with `cap_hit = True` and return the current draft.
- **`escalate`** — The question cannot be grounded in the available corpus evidence; revision will not help. Exit graph immediately on any pass, including the first. Return draft with `warning` set.

`max_critic_revisions = 2` is configurable via `settings.max_critic_revisions`; default 2 (spec §3).

## Alternatives considered

- **Re-synthesize on revise** — Rejected. Edit-in-place is cheaper (analyst does not re-read all passages from scratch), less likely to regress correct sections, and more measurable in evals (did the specific note get addressed?). Full re-synthesis is only warranted when the draft structure is fundamentally wrong — which is what `escalate` handles.

- **Unbounded revision loop** — Rejected. Spec §7.1 identifies loop divergence as a known risk: a confused or adversarial critic emitting "revise" indefinitely. A hard cap is the only reliable mitigation. Cap-hit rate is measured in evals (spec §7.1).

- **Discard draft on cap-out, return empty answer** — Rejected. At cap-out, the draft is likely mostly correct — the cap typically fires on a single stubborn disputed citation, not a wholesale failure. Discarding it wastes the analyst's work and returns a worse answer than the in-progress draft. Return the draft with a `warning` flag so the caller can surface the caveat.

- **`escalate` only after at least one revision attempt** — Rejected. Escalation signals a corpus coverage gap, not a citation-quality problem. Forcing a revision attempt first wastes a Sonnet call on a question the corpus cannot answer. The critic can recognize a coverage gap on the first pass as reliably as on the second; the distinction from "revise" is the nature of the failure, not how many attempts have been made.

- **`escalate` discards the draft** — Rejected for the same reason as cap-out discard: the analyst may have produced a partial answer for the portions of the question that *are* covered. Return the draft with `warning` and let the caller decide.

## Consequences

**Enables:**
- Bounded worst-case cost: at most 3 analyst calls + 3 critic calls per query (initial + 2 revisions + final critic).
- A corpus-gap signal (`escalate`) that is distinct from a citation-quality signal (`revise`), giving callers and evals different remediation paths.
- Cap-hit rate as a measurable eval metric without extra instrumentation.
- Retention of the best-available draft in all exit conditions.

**Constrains:**
- Two revisions may be insufficient for queries that genuinely need iterative refinement. The cap is a policy choice, not a technical limit.
- Edit-in-place requires the analyst to receive the previous draft in its context window on revision passes, adding tokens vs. a stateless re-synthesis.

**Reopen if:**
- Evals show critic adds <2% to `citation_validity` over the single-agent baseline (spec §3.1 trigger) — then the entire revision loop may be theater, not value. Consider removing the critic or reducing to 1 revision.
- Cap-hit rate in evals exceeds 10% — suggests 2 revisions is too low for the query distribution. Raise `max_critic_revisions` to 3.
- A query class emerges where escalation fires incorrectly on a first pass (corpus has evidence, but critic mislabels it as a gap) — add a one-revision buffer before escalation is permitted for that class.

## Related

- spec.md §3.1 (critic-revision loop and reopen trigger), §7.1 (critic loop divergence risk)
- ADR 0001 (LangGraph) — the graph that enforces the routing logic
- ADR 0008 (state shape) — `verdict`, `revision_count`, `cap_hit`, `critic_notes` fields this policy writes
- ADR 0006 (citation span addressing) — what the critic is verifying
- `docs/day4-design.md` section C — full termination-condition analysis
