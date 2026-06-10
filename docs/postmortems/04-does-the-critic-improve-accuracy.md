# Is the critic theater? Measuring whether a second agent actually improves accuracy

## The question

The system runs an analyst → critic loop: the analyst drafts an answer with citations, a
second Sonnet agent verifies every citation against its source chunk and sends the draft
back with notes until it approves or a hard cap of 2 revisions fires (ADR 0009). A second
agent that re-reads the first one's work *feels* like it should raise quality. But "feels
like" is how you end up paying for theater. The real question was narrow and falsifiable:
**does the critic make the answer measurably more accurate, or is it ceremony?**

ADR 0009 had already written its own kill-switch into the "reopen" triggers: if the critic
raises `citation_validity` by **less than 2 percentage points** over the single-agent
baseline, "the entire revision loop may be theater — consider removing the critic." So this
wasn't a vibe check. There was a pre-registered number the critic had to clear to justify
its existence.

## Why it wasn't obvious either way

The case *for* the critic: an analyst optimizing for a fluent answer will overclaim — attach
a citation to a sentence the source only half-supports — and a dedicated verifier whose only
job is "does the quote actually appear in the chunk" should catch exactly that.

The case *against*: the analyst and critic are the same model family looking at the same
passages. A critic that shares the analyst's blind spots rubber-stamps the same errors, and
you've doubled cost and latency for a second opinion that isn't independent. Plenty of
"add a reviewer agent" designs are this. The only way to know which one we'd built was to
turn the critic off and measure.

## The experiment

Two arms over the same 30-case golden set, judges on, everything else identical:

- **Arm 1 — critic OFF:** `CRITIC_FORCE_VERDICT=approve`. The critic node still runs but every
  verdict is forced to `approve`, so the loop never revises — a true single-agent baseline
  with the rest of the graph unchanged.
- **Arm 2 — critic ON:** production config, live verdicts, revisions and cap as designed.

Isolating the critic this way (rather than ripping the node out) keeps the graph, retrieval,
and prompts byte-identical between arms, so any delta is the revision loop and nothing else.

## What the numbers said

The critic doesn't just move the accuracy metric — it's the difference between an answer set
that ships and one that doesn't.

All scores are on a 0–100% scale except `position_quality`, which is rated out of 5.
"Change" is in **percentage points** — the raw gap between the two columns, not a relative
percentage.

| Scorer | Critic OFF | Critic ON | Change |
|---|---|---|---|
| **citation_validity** (must hit 95% to ship) | **84.2%** (6.7% of cases pass) | **97.2%** (73.3% pass) | **+13.0 pts** (≈15% higher, relative) |
| key_fact_coverage (target 80%) | 78.3% | 80.8% | +2.5 pts |
| position_quality (target 4.0 / 5) | 4.56 / 5 | 4.80 / 5 | +0.24 |
| quote-faithfulness (τ = 0.85) | 385 / 455 quotes verified (84.6%) | 449 / 461 verified (97.4%) | +12.8 pts |

The headline is `citation_validity`: it climbed **13 percentage points — from 84.2% to
97.2%** (about 15% higher than the baseline in relative terms). That's more than 6× the
2-point bar ADR 0009 set for "not theater." And the release gate sits at **95%** — so the
single-agent baseline (84.2%) is **below the line and would not ship**, while the critic arm
(97.2%) clears it. The critic isn't polishing an already-good answer; it's what takes
citation accuracy from failing to passing. Quote-faithfulness tells the same story from the
source-text side: the bare analyst got about **1 quote in 6 wrong** (84.6% verified); the
critic cut that to roughly **1 in 40** (97.4%).

The two judge-scored metrics moved far less (`key_fact_coverage` +2.5 points,
`position_quality` +0.24 on the 5-point scale), which is exactly what you'd expect: the
critic's job is citation correctness, not
coverage or ordering. It improved the thing it was built to improve and left the rest roughly
where it found them. That specificity is itself evidence the lift is real and not noise — a
spurious effect wouldn't land precisely on the targeted metric.

## The wrinkle — the cap is biting on half the cases

The critic earns its place, but the same run flags that it's straining against its budget.
**14 of 30 cases (47%) hit `cap_hit=True`** — the critic was still demanding revisions when
the 2-revision cap forced an exit (39 `revise` vs 46 `approve` verdicts across all passes).
So 97.2% is the accuracy the critic reaches *with the cap cutting it off early on half the
cases*, not the ceiling of what it could reach if allowed to keep going.

This trips ADR 0009's *other* reopen trigger — cap-hit rate over 10% says "2 revisions may be
too low for this query distribution." We're at 47%. The open question this leaves is whether
raising the cap to 3 lifts accuracy further or just burns tokens on a handful of cases the
critic and analyst will never agree on (a stubborn disputed citation can loop forever). That's
a cheap targeted follow-up, not a guess to make here.

## Verdict and what I'd do differently

**Verdict: keep the critic.** On the one metric it exists to move it delivers a 13-point lift
(84.2% → 97.2%) — more than 6× the 2-point theater threshold the project set for itself — and
is the sole reason the answer set passes its 95% accuracy gate. It costs roughly 4× the analyst-only graph spend per run, but
the question was never cost — it was whether the accuracy is real, and it is.

Two things I'd change. First, I'd **run this delta earlier and cheaper.** The forced-approve
trick is a one-flag baseline; there was no reason to wait for a full paid two-arm run to learn
the critic wasn't theater — a smaller smoke on a handful of known-overclaim cases would have
given the same directional answer for cents. Second, I'd **treat the 47% cap-hit as the real
open item**, not a footnote: the critic clearing its accuracy bar and the critic running out
of revisions on half its cases are the same finding viewed twice, and the second half is where
the next accuracy gain (or the proof there isn't one) is hiding.
