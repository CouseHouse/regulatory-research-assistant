# Day 6 — Evals (the most important day)

## Goal

30 golden questions written, harness fully wired, first real baseline number. This is the day the JD's "evals as a deliverable" requirement gets satisfied.

**Do not write the golden questions while implementing the agent.** Pick them cold, before you know what your system handles well. Otherwise you bias toward questions your system already passes.

## Deliverables

- `evals/golden.jsonl` with 30 real questions (10 easy / 15 medium / 5 hard) — REPLACE the three placeholder examples
- `src/rra/evals/scorers.py`: fill in NotImplementedError stubs:
  - `CitationValidityScorer.score()` — deterministic, against Postgres
  - `KeyFactCoverageScorer.score()` — Haiku LLM-as-judge
  - `PositionQualityScorer.score()` — Sonnet judge **with retrieved passages in context** (the anti-reward-hacking design)
- `src/rra/evals/run.py`: fill in `run_agent()` and `make_corpus_lookup()` against the real graph and DB
- `.github/workflows/evals.yml`: CI workflow that runs the harness on PR and gates on citation_validity

## Designing 30 good questions

Pick 8-10 distinct FDA guidances first. Then per guidance, write 3 questions:
- **Easy:** Single-guidance lookup with a known answer ("What does guidance X recommend about Y?")
- **Medium:** Synthesis across that guidance and 1-3 others
- **Hard:** Edge case where the right answer is partial or "the guidance does not clearly address this"

The 5 hard questions are the most valuable. They test refusal-to-hallucinate, which is what regulated-vertical customers actually care about. Don't skip them or pick easy ones.

For each question, annotate:
- `expected_facts`: 2-4 statements the answer SHOULD make
- `expected_guidance_ids`: which docs SHOULD be cited
- `notes`: why this case matters (helps you remember when revisiting in 2 weeks)

## Decisions to make

1. LLM-as-judge JSON parsing strategy: tolerate slight format drift (regex extract) or strict parse with retry? Strict-with-one-retry is the right pattern.
2. CI gate threshold: 0.95 citation validity is the spec target. Day-6 baseline likely below it — fail the build, then fix on day 7. Don't lower the gate to pass.
3. Where to surface eval results: just a markdown report (current design) is fine for v1; Langfuse "datasets" feature is a future enhancement.

## Stop conditions

- `uv run python -m rra.evals.run` runs all 30 questions without crashing
- `evals/results/latest.md` shows real numbers (not the placeholders from day 1)
- CI workflow committed and exercised (open a draft PR, watch it fail intentionally)
- Per-difficulty-band breakdown shows scores aren't uniformly low or uniformly high — if they are, your questions are too easy/hard

## Expected baseline (rough)

- Citation validity: 0.80-0.95 (depends on prompt strength; below 0.95 is normal day-6 result)
- Key fact coverage: 0.60-0.80
- Position quality: 3.5-4.2 / 5.0

If you're suspiciously high (everything > 0.9), check your questions — they're probably too easy.
If you're suspiciously low (everything < 0.5), check your judge prompts — they're probably miscalibrated.

## Don't do yet

- Fix the weaknesses (day 7)
- Cloud deploy (day 8)
- Polish — the eval report can look ugly today

## Definition of done

Dev-log entry shows: baseline scores per scorer, per-difficulty-band breakdown, the 1-2 weakest categories with specific example failures, and which weakness is targeted for day 7's fix.
