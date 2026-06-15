# 0023 — Layer-aware defense-in-depth security gate (HF detector + measured coverage)

**Status:** Accepted
**Date:** 2026-06-12
**Owner:** Kyle Couse (drafted by Claude in the ports/adapters/security refactor, Phase 3)

## Context

Phase 3 swaps the `AllowAllGuardrails` wiring adapter for a real local
injection detector (ADR 0022 anticipated this as a pure adapter change). The
first measured run exposed a metric-integrity problem: the red-team corpus
(RT-redteam.md) deliberately contains attacks a phrasing-based classifier
cannot see — regulatory-prose camouflage, fabricated citations, tool-misuse
directives — and the single-model detection rate on it is 0.412. A harness
that exercises only the detector but gates on a system-level "detection rate"
either fails forever on attacks the detector was never the control for, or
invites corpus-gaming to go green. Meanwhile the controls that DO stop those
attacks (XML sanitization, the deterministic citation gate, deny-by-default
tool scoping, secret confinement) already exist and are mechanically testable.

## Decision

The merge-blocking security gate measures **defense-in-depth coverage**, not
single-detector detection: every attack row in
`evals/fixtures/redteam_injection.jsonl` is tagged with the layer(s) expected
to stop it; `python -m rra.evals.security` mechanically exercises each layer
(`detector` = `HFInjectionGuardrails` running
`protectai/deberta-v3-base-prompt-injection-v2` on CPU at threshold 0.2;
`sanitizer`; `citation-gate` through the tool-transport chokepoint;
`tool-scoping`; `secret-confinement`; `output-filter`) and gates on
coverage ≥ 0.80, detector FP ≤ 0.20, and zero harness errors. Cases covered
only by architectural assertion (`inert-no-resume`) and named residuals
(`behavioral` — prose persuasion, validated only by the credit-gated two-arm
eval) are reported separately by id, never silently folded into the headline.

Detector threshold 0.2 is the secure-by-default operating point ("block
unless confidently safe"): at the retrieved-content boundary the cost of a
block is one dropped passage, so uncertainty resolves toward blocking. On
the corpus the margin is wide (benign ≤ 0.011; one hard look-alike, rt-c03,
is misclassified at any threshold and is the accepted FP within the budget).

## Alternatives considered

- **Gate on single-detector detection rate ≥ 0.8 (the original harness)** —
  Rejected: structurally unreachable on this corpus without removing the
  first-layer-miss cases the mandate requires, i.e. it incentivizes weakening
  the corpus. The detector's honest rate (0.471 at threshold 0.2) is still
  reported, marked illustrative.
- **Re-tag hard cases `should_block: false`** — Rejected: corpus-gaming; the
  attacks are real, only the owning control differs.
- **LLM Guard as the detector** (named in ADR 0022 / the master plan) —
  Rejected for the local profile: it wraps the same protectai DeBERTa model
  behind a larger dependency surface; importing the model directly via
  `transformers` keeps the supply-chain surface smaller (RT-8) and the
  scanner config ours. The guardrails port is unchanged either way.
- **An LLM-judge layer for the prose-persuasion residuals (rt-006, rt-014)** —
  Rejected for the gate: it would make the merge gate cost money and violate
  the no-paid-calls constraint; the two-arm eval owns those cases.

## Consequences

**Enables:**
- An honest, free, merge-blocking gate: coverage 0.895 (17/19), FP 0.200,
  detector-only 0.526, with rt-006/rt-014 as named residuals instead of
  invisible misses. (Post RT/SC review — RT-log.md / SC-matrix.md. The first
  cut reported 0.882 before RT showed two probes — sanitizer and long-passage
  chunking — didn't exercise their runtime mechanism; both are now contextual.)
- Per-layer regression detection: a layer that silently dies drops exactly
  its tagged cases out of coverage and fails the gate.
- The cloud-profile swap stays adapter-shaped: Bedrock Guardrails / Model
  Armor / Content Safety replace the `detector` layer; the other layers are
  provider-independent code and keep their probes unchanged.

**Constrains:**
- Every new attack row must name its owning control (loader-enforced), which
  forces the threat-model discipline of mapping attack → control → seam.
- The detector threshold is a config value (`guardrail_threshold`); raising
  it is a gate-relevant change that must re-run the harness.

**Accepted residual risks (recorded per the metric-integrity mandate):**
- **rt-006 / rt-014 are validated by nothing that runs today** (RT-P3-6). They
  are prose persuasion of the critic; no deterministic local layer catches them,
  and the two-arm eval that owns them is credit-gated and unrun. They are
  *unmitigated, not merely residual* — surfaced by id in every run. The
  fail-closed critic (RT-2) bounds the blast radius (a disrupted critic
  escalates, never auto-approves). Closing trigger: the two-arm eval.
- **FP rate sits at exactly 0.200 against the ≤0.20 ceiling, zero margin**
  (SC-D), over a 5-row benign denominator; rt-c03 (the "disregard previous
  versions" supersession look-alike) is the one accepted hard FP. A single new
  benign false-positive tips the gate — see the corpus-growth reopen trigger.

**Reopen if:**
- The two-arm eval (when credits allow) shows the behavioral residuals are
  exploitable at a meaningful rate — that promotes a new control (e.g. a
  critic-side instruction-detection pass) into the exercised layer set.
- A detector model materially better on camouflaged prose becomes available
  under the same free/local constraints (re-run the threshold calibration).
- The corpus grows past ~50 attack rows OR the benign set is widened: revisit
  the 0.80 coverage floor, the zero-margin FP ceiling, and per-class minimums
  (a single rate over a larger mixed corpus hides class-level regressions).
