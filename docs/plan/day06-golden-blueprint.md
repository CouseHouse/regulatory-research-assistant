# Day 6 — Golden Eval Dataset Blueprint (for human review)

**Status:** Draft for review. Phase 1 output only — `evals/golden.jsonl` is NOT
written yet. Approve this blueprint before Phase 2 authoring.

**Scope:** Author the 30-question golden set ONLY. No changes to `scorers.py`,
`run.py`, or harness wiring.

**Target path (confirmed from `dataset.py`):**
`GOLDEN_PATH = Path(__file__).resolve().parents[3] / "evals" / "golden.jsonl"`
→ resolves to top-level **`evals/golden.jsonl`** (NOT under `src/`).

## Method notes (how this was verified)

- **Corpus is source of truth.** Inventory pulled live from `corpus.chunks`:
  **71 distinct guidances, 2726 chunks total.** Counts below are from that query.
- **73126 verified absent:** `SELECT COUNT(*) ... WHERE guidance_id='73126'` → **0**.
  It is anchored nowhere in this plan.
- **Thin tail confirmed and excluded as anchors:** 89238=4, 72685=6, 72646=7,
  72446=7 chunks. Not used.
- **Cold-set rule honored.** All grounding came from reading `corpus.chunks` text
  via SQL (`ILIKE` probes). The answer pipeline (`run_graph`/analyst/agent) was
  **never invoked**. No question was previewed against system output.

---

## 1. Finalized anchor slate

All anchors ≥18 chunks except where a thin doc appears only as a *secondary*
synthesis support (never the sole anchor). Counts are live from `corpus.chunks`.

### CardioWatch (AI / SaMD)
| guidance_id | chunks | title (short) | why it's in |
|---|---|---|---|
| 184856 | 97 | AI-Enabled Device Software Functions: Lifecycle Management | Primary AI/SaMD doc; deepest in corpus. Easy + medium + 2 hard. |
| 166704 | 66 | Marketing Submission Recommendations for a PCCP | AI change-control; the distinctive AI lifecycle mechanism. |
| 180978 | 62 | Predetermined Change Control Plans (Draft) | General PCCP framework; pairs with 166704 (AI-specific) for synthesis. |
| 153781 | 48 | Content of Premarket Submissions for Device Software Functions | Software documentation level for a SaMD submission. |
| 109618 | 37 | Clinical Decision Support Software | CDS device-vs-nondevice line; SaMD classification. |
| 99785 | 36 | Deciding When to Submit a 510(k) for a Software Change | "New 510(k) vs covered change" for software. |
| 80958 | 50 | Policy for Device Software Functions and Mobile Medical Applications | Enforcement-discretion line for software functions (synthesis support). |
| 73141 | 52 | General Principles of Software Validation | Validation principles vs premarket documentation (synthesis support). |

### InfusePro (connected infusion pump)
| guidance_id | chunks | title (short) | why it's in |
|---|---|---|---|
| 99812 | 98 | Deciding When to Submit a 510(k) for a Change to an Existing Device | Deepest change-decision doc; the 510(k)-change flowchart. |
| 119933 | 79 | Cybersecurity in Medical Devices: QMS Considerations | Premarket cyber (SBOM, threat model, SPDF); also a hard anchor. |
| 86420 | 65 | Medical Device Reporting for Manufacturers | MDR postmarket reporting obligations. |
| 80481 | 53 | Applying Human Factors and Usability Engineering | URRA / critical-task analysis for a delivery device. |
| 163694 | 51 | Content of Human Factors Information in Marketing Submissions | What HF info goes in the submission (pairs with 80481). |
| 95862 | 37 | Postmarket Management of Cybersecurity | Postmarket vuln management (pairs with 119933 across lifecycle). |
| 84830 | 30 | Design Considerations for Devices Intended for Home Use | Use-environment for a home infusion pump. |
| 81015 | 33 | Postmarket Surveillance Under Section 522 | Postmarket reporting beyond MDR (synthesis support). |

### NeuroPath (digital therapeutic, De Novo)
| guidance_id | chunks | title (short) | why it's in |
|---|---|---|---|
| 87363 | 80 | Design Considerations for Pivotal Clinical Investigations | Pivotal study design for the therapeutic claim. |
| 190201 | 52 | Use of Real-World Evidence | RWE to support a submission; also a hard anchor (RWE-vs-trial). |
| 77832 | 47 | Patient-Reported Outcome Measures | PRO endpoints — central to a digital-therapeutic claim. |
| 92671 | 45 | Adaptive Designs for Medical Device Clinical Studies | Adaptive pivotal-study design. |
| 152657 | 41 | Acceptance Review for De Novo Classification Requests | De Novo administrative acceptance; also a hard anchor. |
| 111346 | 30 | Acceptance of Clinical Data to Support Medical Device Applications | Clinical-data acceptability (ISE/ISS-style) for synthesis. |
| 72674 | 18 | De Novo Classification Process | De Novo pathway mechanics; hard anchor (with 152657). |
| 141565 | 19 | Principles for Selecting/Developing/Modifying PRO | PRO instrument development (synthesis support only). |

### Cross-cutting (synthesis & hard fuel)
| guidance_id | chunks | title (short) | why it's in |
|---|---|---|---|
| 99769 | 83 | Factors to Consider When Making Benefit-Risk Determinations | Benefit-risk framing across all three products. |
| 112671 | 33 | Multiple Function Device Products | Multi-function policy (one regulated + one non-device function). |

**Distinct guidances anchored across the 30 questions: 24.**
(184856, 166704, 180978, 153781, 109618, 99785, 80958, 73141, 99812, 119933,
86420, 80481, 163694, 95862, 84830, 81015, 87363, 190201, 77832, 92671, 152657,
111346, 72674, 112671 — plus 99769 used as benefit-risk support in M5.)

---

## 2. The 30-question plan

`gist` is intent, not final wording. Final query text + 2–4 grounded
`expected_facts` are authored in Phase 2 from chunk text.

### Easy (10) — single-guidance factual lookup
| id | product | target ids | gist | rationale |
|---|---|---|---|---|
| easy-001 | CardioWatch | 184856 | What does the AI lifecycle guidance recommend documenting about training/development data? | Verified: #36 data independence, #4/#30 representativeness & bias-from-underrepresentation. Clear single-doc answer. |
| easy-002 | CardioWatch | 166704 | What are the required components of a PCCP? | Verified: ToC shows Modification Protocol + (Description of Modifications, Impact Assessment). Canonical single-doc fact. |
| easy-003 | CardioWatch | 109618 | What is FDA's regulatory approach to CDS software functions (device vs non-device)? | Verified: #4 states purpose is to describe FDA's regulatory approach to CDS per 520(o). |
| easy-004 | CardioWatch | 153781 | What software documentation does FDA recommend in a premarket submission? | Verified: #19 inputs/outputs, #25 SRS formatting, documentation-level concept. |
| easy-005 | InfusePro | 119933 | What cybersecurity artifacts does FDA recommend in a premarket submission? | Verified: #19 security risk management report (AAMI TIR57/SW96), SBOM, threat modeling. |
| easy-006 | InfusePro | 80481 | What does the HF guidance recommend for use-related risk analysis / critical tasks? | Verified: defines task/use error/hazardous situation/HFE; URRA + critical-task core content. |
| easy-007 | InfusePro | 86420 | What are manufacturer MDR reporting obligations / timeframes? | Verified: #12 "reasonably suggests," #30 supplemental reports; 30-day reporting is the headline fact. |
| easy-008 | NeuroPath | 152657 | What does the De Novo acceptance review check for? | Verified: it is an acceptance checklist (#22/#29/#30) — administrative completeness, not substantive review. |
| easy-009 | NeuroPath | 77832 | What does the PRO guidance say about content validity of a PRO instrument? | Verified: #8 "evidence that the instrument measures what it is intended to measure," #4 iterative development. |
| easy-010 | NeuroPath | 92671 | What is an adaptive design and what must be pre-specified? | Verified: title + ToC cover preplanned vs not-preplanned changes using blinded/unblinded data. |

### Medium (15) — synthesis across 2–3 guidances
| id | product | target ids | gist | rationale |
|---|---|---|---|---|
| medium-001 | CardioWatch | 166704, 180978, 184856 | How does a PCCP fit the AI total-product-lifecycle approach? | Synthesis of AI-specific PCCP (166704) + general PCCP framework (180978) + TPLC (184856). |
| medium-002 | CardioWatch | 99785, 166704 | When does a software change need a new 510(k) vs being covered by an authorized PCCP? | Pairs change-decision logic (99785) with PCCP coverage boundary (166704 #18). |
| medium-003 | CardioWatch | 109618, 80958 | Is a given CDS feature a regulated device function or under enforcement discretion? | CDS criteria (109618) + software-function policy line (80958). |
| medium-004 | CardioWatch | 153781, 73141 | How do premarket software documentation expectations relate to software-validation principles? | Submission content (153781) vs validation lifecycle (73141). |
| medium-005 | CardioWatch | 184856, 99769 | How should benefit-risk be framed for an AI-enabled device? | AI-specific risks (184856) through the benefit-risk lens (99769). |
| medium-006 | InfusePro | 119933, 95862 | What are the lifecycle cybersecurity obligations from premarket through postmarket? | Premarket SPDF/SBOM (119933) → postmarket vuln management (95862). |
| medium-007 | InfusePro | 163694, 80481 | What HF information belongs in the submission and how is it generated? | Submission HF content (163694) + HF engineering process (80481). |
| medium-008 | InfusePro | 84830, 80481 | What HF / use-environment considerations apply to a home-use infusion pump? | Home-use environment (84830) + HF analysis (80481). |
| medium-009 | InfusePro | 99812, 119933 | When does a cybersecurity-driven change to the pump require a new 510(k)? | Change-decision flowchart (99812) applied to a cyber update (119933). |
| medium-010 | InfusePro | 86420, 81015 | What postmarket reporting obligations apply beyond individual adverse-event reports? | MDR (86420) + §522 postmarket surveillance (81015). |
| medium-011 | NeuroPath | 72674, 152657 | What is the De Novo pathway end-to-end (process + acceptance gate)? | Process mechanics (72674) + acceptance review (152657). |
| medium-012 | NeuroPath | 77832, 111346 | How can a PRO endpoint serve as acceptable clinical evidence? | PRO instrument (77832) + clinical-data acceptability (111346). |
| medium-013 | NeuroPath | 92671, 87363 | How should a pivotal study for the therapeutic be designed (incl. adaptive elements)? | Pivotal design (87363) + adaptive design (92671). |
| medium-014 | NeuroPath | 190201, 111346 | How can real-world evidence support (not replace) a device application? | RWE relevance/reliability (190201) + clinical-data acceptance (111346). |
| medium-015 | CardioWatch | 112671, 109618 | How is a multi-function product handled when one function is CDS/AI and another is not a device? | Multi-function policy (112671) + CDS line (109618). |

### Hard (5) — refusal-to-hallucinate (partial / "not clearly addressed")
| id | product | target ids | gist | rationale (gap) |
|---|---|---|---|---|
| hard-001 | CardioWatch | 184856 | What quantitative subgroup performance-parity threshold must the AI meet to be considered unbiased? | Guidance addresses bias **qualitatively only** — no numeric threshold exists. Correct answer is partial. |
| hard-002 | CardioWatch | 184856 | What additional validation does FDA require for a **generative-AI / LLM** feature? | Generative AI / LLM **entirely absent** from corpus. Correct answer: only general AI expectations apply; no genAI-specific guidance. |
| hard-003 | InfusePro | 119933, 153781 | What device-to-device **interoperability** design requirements/standards must the pump meet to connect to hospital systems? | No dedicated interoperability guidance in corpus — only external references. Correct answer is partial + "not in corpus." |
| hard-004 | NeuroPath | 152657, 72674 | What level of **clinical evidence / effect size** does FDA require to grant De Novo for a digital therapeutic? | De Novo docs are administrative (acceptance checklist + process); they require "effectiveness data" but never set the substantive bar. Partial. |
| hard-005 | NeuroPath | 190201, 87363 | Can real-world evidence **substitute for** the pivotal premarket clinical trial? | RWE doc supports/informs decisions; it never authorizes replacing a premarket trial. Correct answer: RWE supports, does not replace. Partial. |

---

## 3. Hard-five verified-gap evidence

For each hard case, the chunks read and why the gap is genuine (not "answer
hiding in chunk N").

**hard-001 — Subgroup parity thresholds (184856).**
Read 184856 #4, #9, #24, #30, #31, #36. The doc treats bias qualitatively:
#4 recommends evaluating "whether a device benefits all relevant demographic
groups (e.g., race, ethnicity, sex, and age) similarly"; #30 warns
underrepresentation "could lead to … AI bias"; #9 calls for "control of bias"
through the TPLC; #24 describes reporting "corresponding performance for
different operating points." **No chunk states a numeric parity threshold or
acceptance criterion.** The honest answer must say the guidance expects subgroup
evaluation but does not set a quantitative threshold.

**hard-002 — Generative-AI / LLM validation (184856).**
Probed the **entire corpus** for `generative`, `large language model`, `LLM`,
`foundation model` → **zero matches**. 184856 covers AI-enabled device software
functions generically (TPLC, data management, performance). There is no
generative-AI-specific content anywhere. The honest answer: general AI lifecycle
expectations apply, but the corpus contains no guidance specific to generative
AI / LLMs.

**hard-003 — Device interoperability requirements (119933, 153781).**
Probed `interoperab%` across the whole corpus. The only hits are *references*,
not requirements: 119933 #23 points to a separate "Interoperability Guidance"
(not ingested) and says only to "consider the appropriate cybersecurity risks …
associated with the interoperability"; 153781 #20 merely asks the submitter to
state "what methods, standards, and specifications are used to interact and/or
communicate with other … devices." **No chunk specifies interoperability design
requirements or names consensus standards.** The honest answer is partial and
should flag that the dedicated interoperability guidance is not in the corpus.

**hard-004 — De Novo clinical evidence bar (152657, 72674).**
Read 152657 #22, #29, #30, #35, #36 and 72674 #2, #3, #5. 152657 is an
**acceptance checklist** — it lists "6. Effectiveness data ☐ ☐ ☐" as a required
*element* but never states how much/what kind of evidence is sufficient. 72674
is purely **procedural** (statutory pathway under 513(f)(2), how to submit,
timelines). **Neither sets a substantive clinical-evidence threshold or effect
size for a digital therapeutic.** Correct answer is partial: the De Novo docs
confirm effectiveness data is required and administratively reviewed, but the
clinical bar itself is not specified in the corpus.

**hard-005 — RWE substituting for a pivotal trial (190201, 87363).**
Probed 190201 for `in lieu of`, `instead of`, `replace` → **no matches**.
190201 #10 frames RWE as "appropriate to generate RWE when the RWD are relevant
to and reliable for **informing or supporting** a particular [decision]"; #30
stresses careful study design/assessment. **No chunk states RWE can replace or
substitute for a premarket clinical investigation.** 87363 (pivotal design) is
about running a prospective study, not waiving one. Correct answer: RWE can
support a submission; the corpus does not say it can replace the pivotal trial.

---

## 4. Notes for the reviewer

- **180978 vs 166704:** both PCCP docs are anchored; 180978 (general framework)
  appears only as a 3rd support in medium-001. If you'd rather give 180978 its
  own easy lookup, say so and I'll swap it in (it has 62 chunks, comfortably
  groundable).
- **141565** (PRO development, 19 ch) is listed in the slate but NOT anchored in
  any of the 30 — held as optional support. It is thin; I kept 77832 (47 ch) as
  the PRO anchor. No question depends on a thin doc as its sole anchor.
- **Product balance:** CardioWatch 11 (4E/5M/2H), InfusePro 9 (3E/5M/1H),
  NeuroPath 9 (3E/4M/2H), cross-cutting 1 (multi-function medium).
- **Anti-trap check:** PCCP-for-intended-use-change was considered as a hard
  candidate and **rejected** — 166704 #11 and #18 explicitly say major
  intended-use changes fall outside a PCCP and require a new submission, so it is
  *answerable*, not a refusal case. Recorded here so it isn't reintroduced.

**Phase 1 ends here. `golden.jsonl` is intentionally NOT written.**
