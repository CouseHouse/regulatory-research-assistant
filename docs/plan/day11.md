# Day 11 — Postmortems

## Goal

Three "what broke" writeups distilled from the dev log. These are interview gold.

## Deliverables

- `docs/postmortems/01-<slug>.md`
- `docs/postmortems/02-<slug>.md`
- `docs/postmortems/03-<slug>.md`
- README links to all three

## Choosing the three

Scan `docs/dev-log.md` from days 2-10. Look for entries with:
- Before/after measurements
- A clear "I thought X, turned out it was Y" arc
- A fix that's defensible in an interview (not just "I added a try/except")

Strong candidates from likely failure modes:
- Day 7's targeted fix (probably your strongest one)
- Day 2 ingestion surprises (scanned PDFs, embedding batch limits, transaction failures)
- Day 4 critic loop divergence (the spec §7.1 risk)
- Day 6 evaluation reward-hacking (judge agreeing with confident hallucinations until passages added to judge context)
- Day 9 cloud deploy surprises (IAM gaps, pgvector extension, ALB timeout tuning)

Pick the three with the **clearest before/after numbers**. Stories without numbers read like opinions.

## Postmortem shape (~250-400 words each)

```markdown
# [Specific symptom in one sentence]

## Symptom
What was observed. Include the metric/log/screenshot.

## First hypothesis
What I thought was wrong. Why.

## What I tried first
The fix I attempted. The result.

## What turned out to be the actual problem
The real root cause, with evidence.

## The fix
What changed. Code link or diff.

## Before / after
| Metric | Before | After |
|---|---|---|
| X | 0.71 | 0.88 |

## What I'd do differently
With hindsight, what's the lesson.
```

The "first hypothesis that turned out wrong" section is the most important. Senior engineers debug; junior engineers fix. The first-hypothesis arc shows you debug.

## Anti-patterns to avoid

- **Marketing prose.** "I noticed a fascinating opportunity to optimize..." — no. State the symptom flatly.
- **Hindsight bias.** "Of course X was wrong because Y" — say what you actually thought at the time.
- **Vague metrics.** "Things got better" is useless. Pick a number.
- **Pretending the fix worked first try.** It didn't. Say what failed.
- **Skipping the wrong turn.** The failed first attempt is what makes the story credible.

## Stop conditions

- Three postmortems written, 250-400 words each
- Each has a before/after metric
- README has a "What broke and what I learned" section linking to all three
- A friend (or you, after a coffee) could read one in 2 minutes and follow it

## Don't do yet

- Loom recording (day 13)
- Final repo polish (day 12)

## Definition of done

The three files exist, render correctly on GitHub, and you would not be embarrassed if an interviewer found them and asked you to walk through one.
