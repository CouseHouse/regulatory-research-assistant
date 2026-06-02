# Day 7 — Fix the worst weakness

## Goal

Address the lowest-scoring metric from day 6 with a real, measurable fix. This work becomes postmortem #1.

## Process (not specific code)

1. **Pick ONE weakness from day 6.** Don't try to fix everything. The smallest single change that moves a metric is the right scope.
2. **Form a hypothesis.** Examples:
   - "Recall is bad on terminology mismatch → researcher should generate query rephrasings"
   - "Citation validity is below 0.95 because the critic accepts paraphrased spans → tighten the critic prompt to require exact substring matches"
   - "Hard-band position quality is low because the analyst overstates confidence → add explicit hedging instructions"
3. **Make ONE change.** Implement the smallest version of the fix.
4. **Re-run evals.** `uv run python -m rra.evals.run`
5. **Did the target metric improve?**
   - YES → did other metrics regress? If no regressions: commit, document in postmortem.
   - YES but regression elsewhere → either roll back, or accept the trade and document why
   - NO → try a different fix. Cap at 3 attempts; if nothing works, the issue may be deeper than a one-day fix and goes in future-work.
6. **Document everything.** This day's dev-log becomes the postmortem on day 11.

## What this looks like as a postmortem

The dev-log entry for today should answer:
- **Symptom:** which metric, what score, what kind of question fails
- **First hypothesis:** what you thought was wrong, why
- **First fix:** what you changed, before/after numbers
- **If that didn't work:** second hypothesis, second fix
- **Final state:** target metric score, any other metric changes
- **What you'd do differently:** if you started fresh

## Deliverables

- ONE code change targeting the weakness (small, focused PR)
- Updated `evals/results/latest.md` showing the improvement
- `docs/dev-log.md` entry detailed enough to become a postmortem on day 11

## Decisions you'll face

If multiple metrics are weak, which to attack first? Priority order:
1. **Citation validity** — if this is below 0.95, that's the hardest gate and the most embarrassing failure. Fix first.
2. **Hard-band failures** — refusal-to-hallucinate is the regulated-vertical signature feature.
3. **Position quality** — quality scores are softer and more subjective.
4. **Key fact coverage** — usually the easiest to move; can wait if other things are worse.

## Stop conditions

- The targeted metric moved by at least 5 percentage points (or 0.3 on the 5-point quality scale)
- Other metrics didn't drop by more than 2 points
- Detailed dev-log entry written
- Eval CI is green again (or its failure is documented and intentional)

## Don't do yet

- Touch multiple metrics simultaneously
- Add features the spec didn't call for (no whack-a-mole)
- Postmortem polish — that's day 11

## Definition of done

Eval re-run shows real improvement on the target. Dev-log entry has before/after numbers and the actual code diff link.
