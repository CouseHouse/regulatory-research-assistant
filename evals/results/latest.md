# Eval run — 20260611T040923Z
Tag: `refactor-baseline`

**Baseline label:** key-existence only (ADR 0010 Day 6 — chunk address resolution, not quote faithfulness). Do not compare Day 6 numbers to Day 7+ without re-reading ADR 0010 and ADR 0012 P2.

**Cases:** 30  **Scored:** 24  **Errors:** 6
**Zero-citation answers:** 0 of 30 (excluded from citation_validity mean per ADR 0012 D1).
**Citations with NO analyst quote:** 11 (excluded from the citation_validity faithfulness mean — ADR 0012 D1 analog: a shrinking denominator must not inflate the mean).

## Aggregate scores

| Scorer | Mean | Pass rate | Gate | Threshold |
|---|---|---|---|---|
| citation_validity | 0.930 | 66.7% | **HARD** | 0.95 |
| key_fact_coverage | 0.833 | 54.2% | warn | 0.8 |
| position_quality | 0.908 | 100.0% | warn | 4.0 |

## Quote-faithfulness (ADR 0013 — τ-calibration data)

**τ (citation_match_threshold):** 0.85 — sims ≥ τ count as verified.
**Quotes assessed:** 346  ·  **Verified faithful:** 321 (92.8%)  ·  **No analyst quote (excluded):** 11

Match-path breakdown:
- Substring hit (Step 2, exact after whitespace-normalize): 283
- Coverage-ratio scored (Step 3): 63 (verified ≥ τ: 38)
- Key not found (hallucinated address): 0

similarity_score distribution — Step-3 coverage path:
| Band | Count |
|---|---|
| [0.00, 0.50) | 6 |
| [0.50, 0.70) | 14 |
| [0.70, 0.85) | 5 |
| [0.85, 1.00] | 38 |

_The 0.50–0.85 bands are the boilerplate-seam suspects flagged in the Day-7 plan §4 (honest quotes split by a mid-chunk header). Do NOT lower τ to absorb them — clean the corpus (Priority 3) first, then calibrate._
_Raw coverage sims (sorted): 0.349, 0.373, 0.379, 0.388, 0.492, 0.497, 0.516, 0.525, 0.537, 0.542, 0.550, 0.560, 0.567, 0.567, 0.568, 0.578, 0.588, 0.620, 0.652, 0.692, 0.701, 0.727, 0.750, 0.798, 0.815, 0.869, 0.885, 0.906, 0.909, 0.951, 0.952, 0.954, 0.989, 0.989, 0.989, 0.991, 0.992, 0.993, 0.993, 0.993, 0.994, 0.994, 0.994, 0.994, 0.994, 0.994, 0.995, 0.995, 0.995, 0.995, 0.995, 0.995, 0.995, 0.995, 0.995, 0.995, 0.996, 0.996, 0.996, 0.996, 0.996, 0.996, 0.996_

## Per-case detail

### `easy-001` (easy)
> What does the FDA AI lifecycle guidance recommend about documenting training and development data for an AI-enabled device?
- ❌ **citation_validity**: 0.938  _(assessed 16, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `easy-002` (easy)
> What are the required components of a Predetermined Change Control Plan (PCCP) for an AI-enabled device software function?
- ✅ **citation_validity**: 1.000  _(assessed 14, no-quote 1)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 0.800

### `easy-003` (easy)
> What types of device modifications does FDA consider generally appropriate for inclusion in a Predetermined Change Control Plan (PCCP) for a non-AI hardware device?
- ❌ **citation_validity**: 0.769  _(assessed 13, no-quote 0)_
- ❌ **key_fact_coverage**: 0.500
- ✅ **position_quality**: 0.800

### `easy-004` (easy)
> Under FDA's clinical decision support (CDS) software guidance, what criteria determine whether a CDS function is a non-device CDS not subject to FDA oversight?
- ✅ **citation_validity**: 1.000  _(assessed 16, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `easy-005` (easy)
> What software documentation does FDA recommend including in a premarket submission for a device software function?
- ✅ **citation_validity**: 1.000  _(assessed 17, no-quote 2)_
- ❌ **key_fact_coverage**: 0.500
- ✅ **position_quality**: 0.800

### `easy-006` (easy)
> What cybersecurity artifacts does FDA recommend including in a premarket submission for a networked medical device?
- ✅ **citation_validity**: 1.000  _(assessed 16, no-quote 1)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `easy-007` (easy)
> What does FDA's human factors guidance recommend for conducting a use-related risk analysis (URRA) for a medical device?
- ✅ **citation_validity**: 1.000  _(assessed 16, no-quote 0)_
- ❌ **key_fact_coverage**: 0.500
- ✅ **position_quality**: 0.800

### `easy-008` (easy)
> What are a manufacturer's MDR reporting obligations when a device malfunction occurs, and what timeframes apply?
- ❌ **citation_validity**: 0.818  _(assessed 11, no-quote 4)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `easy-009` (easy)
> What does FDA's acceptance review check for when evaluating a De Novo classification request?
- ✅ **citation_validity**: 1.000  _(assessed 11, no-quote 0)_
- ❌ **key_fact_coverage**: 0.500
- ✅ **position_quality**: 1.000

### `easy-010` (easy)
> What does FDA's patient-reported outcome (PRO) guidance say about establishing content validity for a PRO instrument?
- ✅ **citation_validity**: 1.000  _(assessed 15, no-quote 1)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `medium-001` (medium)
> How does a Predetermined Change Control Plan (PCCP) fit within FDA's total product lifecycle (TPLC) approach for an AI-enabled device, and what happens if a modification is made outside the authorized PCCP?
- ✅ **citation_validity**: 1.000  _(assessed 10, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `medium-002` (medium)
> When does a software change to an AI/SaMD require submitting a new 510(k), versus being covered by an authorized Predetermined Change Control Plan?
- ✅ **citation_validity**: 1.000  _(assessed 11, no-quote 0)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 1.000

### `medium-003` (medium)
> How should CardioWatch determine whether its risk-score display feature (showing AI output alongside referenced clinical guidelines) is a regulated device function or falls under enforcement discretion?
- ✅ **citation_validity**: 1.000  _(assessed 10, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `medium-004` (medium)
> How do the premarket submission software documentation requirements relate to the software validation lifecycle principles FDA expects manufacturers to follow?
- ✅ **citation_validity**: 1.000  _(assessed 12, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `medium-005` (medium)
> How should CardioWatch's manufacturer frame the benefit-risk analysis when the AI model shows lower sensitivity in elderly patients compared to the overall population?
- ❌ **citation_validity**: 0.538  _(assessed 13, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `medium-006` (medium)
> What are InfusePro's cybersecurity obligations across the full device lifecycle, from premarket submission through postmarket vulnerability management?
- ❌ **citation_validity**: 0.688  _(assessed 16, no-quote 1)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `medium-007` (medium)
> What human factors information must be included in InfusePro's marketing submission, and how is that information generated through the HF engineering process?
- ✅ **citation_validity**: 1.000  _(assessed 19, no-quote 1)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 0.800

### `medium-008` (medium)
> What human factors and use-environment considerations apply specifically to a connected infusion pump intended for home use?
- ❌ **citation_validity**: 0.833  _(assessed 18, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 0.800

### `medium-009` (medium)
> When a cybersecurity update to InfusePro changes its network communication protocol (TLS 1.2 to TLS 1.3), does that require a new 510(k)? What factors govern the decision?
- ✅ **citation_validity**: 1.000  _(assessed 12, no-quote 0)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 1.000

### `medium-010` (medium)
> What postmarket reporting obligations apply to InfusePro beyond individual MDR reports — specifically, what triggers a Section 522 postmarket surveillance order?
- ✅ **citation_validity**: 1.000  _(assessed 13, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `medium-011` (medium)
> What is the end-to-end De Novo classification process, from initial submission through the acceptance review and into substantive review?
- ❌ **citation_validity**: 0.789  _(assessed 19, no-quote 0)_
- ❌ **key_fact_coverage**: 0.500
- ✅ **position_quality**: 1.000

### `medium-012` (medium)
> What does FDA require to accept a patient-reported outcome (PRO) instrument as valid clinical evidence supporting a device's primary effectiveness endpoint?
- ✅ **citation_validity**: 1.000  _(assessed 14, no-quote 0)_
- ✅ **key_fact_coverage**: 1.000
- ✅ **position_quality**: 1.000

### `medium-013` (medium)
> How should NeuroPath design a pivotal clinical study for a digital therapeutic, and under what conditions can an adaptive design be incorporated?
- ✅ **citation_validity**: 1.000  _(assessed 18, no-quote 0)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 1.000

### `medium-014` (medium)
> How can NeuroPath use real-world evidence from its patient registry to support the De Novo submission, and what does FDA require for RWD-derived evidence to be considered reliable?
- ❌ **citation_validity**: 0.938  _(assessed 16, no-quote 0)_
- ❌ **key_fact_coverage**: 0.750
- ✅ **position_quality**: 1.000

### `medium-015` (medium)
> CardioWatch combines an AI arrhythmia-detection algorithm with a general-wellness coaching feature. How does FDA's multiple-function device policy apply, and must the wellness feature be evaluated as a medical device?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJemViLFPc1vmWosM3'}

### `hard-001` (hard)
> What quantitative subgroup performance-parity threshold must CardioWatch meet across demographic groups for FDA to consider the AI model adequately non-biased?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJfYsw9gqQArk8wE79'}

### `hard-002` (hard)
> CardioWatch 2.0 incorporates a large language model (LLM) that generates free-text clinical summaries of arrhythmia findings. What additional FDA-specific validation or regulatory requirements apply to the generative-AI/LLM component?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJg3eEcEuoDNFETRuF'}

### `hard-003` (hard)
> What specific interoperability design requirements and consensus standards must InfusePro satisfy to connect to hospital EMR systems and IV infusion management platforms?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJgaPtFke8Q3qD71RQ'}

### `hard-004` (hard)
> What level of clinical evidence — effect size, trial duration, control group type, or minimum patient population size — does FDA require to grant a De Novo classification for a digital therapeutic like NeuroPath?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJh98QJmPKNLamL9JF'}

### `hard-005` (hard)
> Can NeuroPath use its 1,200-patient real-world patient registry as a substitute for the randomized controlled trial, avoiding the need for a traditional pivotal study in the De Novo submission?

**ERROR:** Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits.'}, 'request_id': 'req_011CbvpJiFqyrYZbg7Dfeswo'}
