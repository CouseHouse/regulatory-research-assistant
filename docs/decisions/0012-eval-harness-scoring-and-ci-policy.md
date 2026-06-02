# 0012 — Day 6 eval-harness scoring and CI policy

**Status:** Active
**Date:** 2026-06-02
**Owner:** Kyle Couse

## Context

Day 6 wires the eval harness end-to-end: `run_agent` calls the real LangGraph graph, `make_corpus_lookup` queries Postgres, and the three scorers (`CitationValidityScorer`, `KeyFactCoverageScorer`, `PositionQualityScorer`) run against the 30-case golden set. Four implementation-level decisions need to be locked before code is written, because each has a non-obvious gotcha that would produce misleading eval numbers or a CI setup that doesn't actually prove the gate bites.

The harness operates in key-existence mode for Day 6 (per ADR 0010's activation-timing constraint): `check_citation` verifies that cited `guidance_id:chunk_index` pairs resolve to real corpus rows, not that the quoted text is faithful. That baseline label is essential context for reading Day 6 numbers honestly.

## Decision

Four decisions are recorded here. They govern `CitationValidityScorer` and `make_corpus_lookup` (D1), `.github/workflows/evals.yml` (D2), `PositionQualityScorer` model wiring (D3), and the intentional-fail CI demo case (D4).

### D1 — Zero-citation answers are scored N/A for `citation_validity` and excluded from that scorer's mean

An answer that emits zero citations receives `score=None` (N/A) for `citation_validity` and is **excluded from that scorer's mean** — it is not scored 0.0.

Rationale: a correct hard-refusal answer ("the guidance sets no threshold for this device type") may legitimately cite nothing. Scoring it 0.0 punishes correct behavior and drags the gate down on the five hard-refusal cases in `golden.jsonl`. The threshold (≥ 0.95) is calibrated against answers that do cite; mixing in structural zeros corrupts the denominator.

**Critical guardrail — this exclusion must never become a hiding place.** The runner must emit a prominent count in every report: `"N of 30 answers had zero citations"`. The failure mode of an analyst that degrades to emitting no citations is caught by `key_fact_coverage` (fact recall collapses when answers are vague enough to cite nothing) and by the count itself. Without both of these backstops, a non-citing analyst would show a *falsely rising* `citation_validity` mean as fewer and fewer cases remain in the denominator — the metric would improve as the system got worse.

Current state: `CitationValidityScorer.score()` in `scorers.py:70–72` returns `ScoreResult(self.name, 0.0, False, {"reason": "no citations"})` for the zero-citation case. This must be changed to return a sentinel result that the runner excludes from the mean.

### D2 — CI runs the deterministic gate only, against a lightweight key fixture; full eval is out-of-band

`.github/workflows/evals.yml` runs **only `CitationValidityScorer`** (the sole hard gate, `gate=True`) against a lightweight fixture of `(guidance_id, chunk_index)` key pairs. No embeddings are computed. No judge API calls are made. The full eval — both LLM judges (`KeyFactCoverageScorer`, `PositionQualityScorer`) plus the full 30-case golden set — runs manually or on a nightly schedule, not on every PR.

Rationale: `citation_validity` is deterministic (string-match against corpus keys). Key-existence resolution needs only the key pairs, not the full Postgres vector store. CI therefore runs in seconds at zero API cost and with no secrets beyond a Postgres connection string. Judge models cost tokens, need `ANTHROPIC_API_KEY`, and vary between runs — unsuitable for per-PR gating. This matches the day06.md stop condition "CI gates on `citation_validity`."

### D3 — `PositionQualityScorer` reads `POSITION_JUDGE_MODEL`, not `ANALYST_MODEL`

`PositionQualityScorer` is constructed with `model=os.environ["POSITION_JUDGE_MODEL"]`. The current stub in `run.py:191` uses `os.environ["ANALYST_MODEL"]`; that must be corrected before Day 6 wires the scorer.

Rationale: the analyst model is the system under test. If the judge reads the same env var, then changing the analyst model silently changes the judge — the measurement instrument co-varies with the thing being measured. The judge must be pinned independently so that a model swap triggers an intentional re-evaluation of the judge setting, not a silent drift.

**Caveat (record as a known limitation):** a Sonnet judge evaluating a Sonnet analyst has a mild self-preference bias — the judge will tend to favour responses that sound like the style it would produce. The passages-in-context design mitigates this (the judge's verdict is grounded in retrieved source text, not pure stylistic preference) but does not eliminate it. If `position_quality` scores look inflated relative to human spot-checks, revisit the judge model choice.

### D4 — The intentional-fail CI demo uses a planted bogus case in a CI-only fixture, never in `golden.jsonl`

The day06.md stop condition "open a draft PR, watch it fail" is exercised by a deliberately invalid case — a known-nonexistent `chunk_index` — planted in a CI-only fixture file. This case is **never added to `evals/golden.jsonl`**.

Rationale (stated as a prediction — see Consequences for the full argument): the key-existence baseline is expected to be high (≥ 0.95) because a well-behaved analyst rarely hallucinate chunk indices. The gate may pass naturally on the real golden set, which would mean a "watch it fail" demo is impossible without a planted case. A deliberately wrong citation guarantees `citation_validity < 1.0` for that fixture run, proving the gate bites. Keeping the planted case in a separate CI fixture preserves the integrity of the golden set as ground truth.

## Alternatives considered

**D1 — Score zero-citation answers as 0.0 (the current stub behavior)**
Rejected. The five hard-refusal cases in `golden.jsonl` are deliberately designed to have correct answers with few or no citations. Scoring them 0.0 conflates "bad citation behavior" with "correct refusal behavior" and would make the harness punish the thing it should reward. Raising the threshold to accommodate refusals would mask actual citation failures.

**D1 — Score zero-citation answers as 1.0 ("vacuously valid")**
Rejected. A refusal answer may be correct; that doesn't mean citations are valid. Vacuous scoring removes the citation signal entirely for refusal cases and treats structural absence as equivalent to verified citations.

**D2 — Run all three scorers in CI**
Rejected. `KeyFactCoverageScorer` and `PositionQualityScorer` require live judge API calls. Both are non-deterministic (LLM outputs vary), so a given run could pass or fail on noise rather than a real regression. Per-PR API costs and required secrets make them unsuitable CI gates. Non-deterministic results violate the principle that a CI gate should have a definitive pass/fail on the same code.

**D2 — Run the full harness nightly in CI and gate PRs on the nightly result**
Rejected. This decouples the gate from the change that caused a failure; by the time the nightly runs, multiple PRs may have landed. The value of a CI gate is that it identifies the offending commit immediately.

**D3 — Use `ANALYST_MODEL` for the judge (the current stub)**
Rejected. See D3 rationale above. Co-variation between the system under test and the measurement instrument is a fundamental validity problem.

**D4 — Add a hard-refusal question to `golden.jsonl` that naturally yields a low `citation_validity` score**
Rejected. Hard-refusal cases are *expected* to have N/A `citation_validity` (by D1). They cannot serve as intentional-fail demos because they would be excluded from the mean and would not drive CI failure. The planted bogus chunk_index in a CI-only fixture is the minimal intervention that proves the gate catches hallucinated indices.

## Consequences

**Enables:**
- The `citation_validity` mean is computed over cases where citation behavior is meaningful, without structural corruption from correct refusal answers.
- CI proves the gate is live (planted case) without polluting the ground-truth golden set.
- The judge model is independently pinnable, enabling model-upgrade experiments where the analyst changes but the judge holds constant.
- The zero-citation count in every report makes the N/A exclusion auditable: an analyst that stops citing entirely will be immediately visible in the count even as its `citation_validity` mean climbs.

**Constrains:**
- The runner must implement N/A exclusion logic for `citation_validity` — the `ScoreResult` type or the runner's aggregation path needs a sentinel for "no citations, excluded from mean."
- Two separate fixture files must be maintained: `evals/golden.jsonl` (ground truth, never contains planted failures) and a CI-only key fixture.
- `POSITION_JUDGE_MODEL` must be set in CI and local `.env`; missing it will break `PositionQualityScorer` construction on Day 6 wiring.

**Predictions — read Day 6 numbers against these before drawing conclusions:**

**P1 — A passing Day 6 `citation_validity` gate is not good news.** It is evidence the key-existence ruler is too coarse to see the real citation problem (boilerplate spans, quote faithfulness). Key-existence only catches hallucinated chunk indices; a well-behaved analyst that cites real chunks but quotes them unfaithfully will score near 1.0 on this baseline. The entire motivation for Day 7's matching-engine activation is that a passing Day 6 gate reveals the limit of the measurement, not the quality of the citations. Do not read "0.97, gate green" as "citations are fine." (Consistent with ADR 0010's "measures key-existence only" constraint and the locked baseline label.)

**P2 — The Day 6 → Day 7 comparison is triple-confounded and not apples-to-apples.** Day 7 changes the ruler (activates quote-faithfulness matching), the substrate (re-chunk → new `chunk_index` values invalidate Day 6 addresses), and the corpus (boilerplate cleaning changes source text). None of these changes is isolated. The only honest comparison re-runs the full harness on Day 7 against the same question set using the Day 7 corpus and the activated matching engine; the Day 6 numbers are baseline under a different measurement regime. Reference the locked baseline label in the report and ADR 0010's activation-timing constraint before comparing across days.

**Reopen if:**
- The zero-citation count in reports reveals a systematic non-citing pattern in a non-refusal category — that would indicate the N/A exclusion is masking a real problem, and the D1 policy needs tightening.
- `position_quality` scores are consistently ≥ 4.5 while human spot-checks disagree — revisit D3's judge model choice and the self-preference caveat.
- The planted bogus case in the CI fixture becomes a maintenance burden (e.g., the chunk schema changes) — consider a synthetic in-memory fixture instead.

## Related

- ADR 0010 (`check_citation` matching contract) — defines key-existence mode, activation timing, and the Day 6 baseline label that D1 and P1/P2 depend on
- ADR 0006 (citation span addressing) — `guidance_id:chunk_index` is the address space the CI fixture keys are drawn from
- ADR 0009 (critic-loop policy) — the revise/approve verdicts whose downstream correctness D3's independent judge is designed to measure honestly
- docs/plan/day06.md — stop conditions this ADR governs, including "CI gates on `citation_validity`" and "open a draft PR, watch it fail"
- spec.md §6.2 — `citation_validity` ≥ 0.95 threshold definition
