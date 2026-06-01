# Corpus design

How the FDA guidance corpus for this project was scoped, curated, and
maintained. This document captures the methodology and the substantive
findings from the coverage analysis that shaped the final corpus.

For the decision record summarizing this work, see
[ADR 0007](decisions/0007-corpus-scope-and-methodology.md).

## TL;DR

- **Target size:** 60–75 final guidances. Large enough that synthesis
  questions span multiple docs, small enough to be defensible in an
  interview.
- **Six topical clusters** covering the regulatory lifecycle a compliance
  analyst would touch.
- **Two-stage curation:** automated scraper produces ~150 candidates from
  the FDA index; LLM-assisted coverage analysis identifies gaps and noise;
  manual review via terminal TUI produces the final manifest.

## Why this matters

The corpus is the substance the system reasons about. Every demo, every
eval question, every retrieval result depends on it. A weak corpus
silently caps the system's quality regardless of how good the agent
design is.

A naive approach — grab whatever the scraper produces — gives a corpus
that is 65% device-specific 510(k) noise (tampons, stents, dental
handpieces) and missing the foundational documents an analyst actually
references (Design Controls 1997, GMLP Guiding Principles, the QMSR final
rule). The system answers "510(k) for tampons" questions well and
"how does GMLP affect our SaMD change control plan" questions badly.

The audit below documents the work to fix that.

## Six clusters

Each cluster maps to a kind of question the system should answer well.
Targets are rough — 10–15 docs per cluster is the goal, but some clusters
are foundationally smaller than others.

### 1. pathway-classification (~12 docs target)

Foundational regulatory pathways: 510(k), De Novo, PMA, Breakthrough
Devices, Q-Submission, third-party review, intended-use determination,
HDE. These are the docs that answer "what pathway does our device take?"

### 2. modification-decisions (~8 docs target)

The use-case-defining cluster. "Do we need a new submission for this
change?" Includes the canonical "Deciding When to Submit" docs for
hardware and software, Special 510(k), PMA supplement decisions, 30-day
notices.

### 3. software-samd-ai (~15 docs target)

Software functions, SaMD, AI/ML, PCCP, GMLP, software validation,
clinical decision support. The most JD-aligned cluster — the project's
narrative around regulated-vertical AI engineering lives here.

### 4. cybersecurity (~6 docs target)

Premarket and postmarket cybersecurity, Section 524B "cyber devices"
under the PATCH Act, networked devices, off-the-shelf software security.

### 5. design-controls-qms (~12 docs target)

Design controls (the 1997 foundation), QMSR final rule, ISO 14971 risk
management, human factors / usability, medical device reporting,
postmarket surveillance, recalls.

### 6. clinical-evidence (~8 docs target)

Real-world evidence and data, patient-reported outcomes, adaptive
designs, animal studies, acceptance of clinical data — the methodology
docs, not device-specific clinical guidances.

## Curation methodology

### Stage 1 — Automated scraping

`scripts/scrape_fda_corpus.py` fetches the FDA guidance database JSON
endpoint, filters to CDRH Final guidances with downloadable PDFs, and
applies cluster keyword matching against titles. Output:
`data/corpus/manifest.candidates.json`.

Cluster keyword sets are deliberately broad. False positives are filtered
in review; false negatives (relevant docs not matched) are the larger
risk and are surfaced via the coverage analysis below.

After the initial round, two enhancements were added:

- **Device-specificity exclusion** — pathway-classification was catching
  ~50 single-device-class guidances (tampons, stents, etc.). A
  `DEVICE_SPECIFIC_HINTS` exclusion list reassigns these to a
  `device-specific` pseudo-cluster so they can be inspected but don't
  swamp the primary clusters.
- **Communication type loosening** — the initial filter excluded
  non–"Guidance Document" types, which silently dropped foundational
  docs like the GMLP Guiding Principles and the AI/ML Action Plan.

### Stage 2 — Coverage analysis (dual-Claude pattern)

Before per-entry review, the candidate manifest is pasted into a fresh
Claude chat (without project context) for coverage analysis. The fresh
chat does the domain-knowledge work — naming foundational guidances that
should be present, flagging clusters that are over- or under-represented,
identifying noise patterns.

This is a deliberate methodology choice: domain analysis benefits from
NOT having project context (which crowds out general FDA knowledge in
the model's attention). Decision execution benefits from full project
context. Splitting the two across chats produces better outputs for both.

The Pass 1 coverage report from the first analysis is reproduced below
as a representative artifact. The findings drove the scraper enhancements
in Stage 1.

### Stage 3 — TUI review

`scripts/review_corpus.py` provides a terminal UI for approving,
rejecting, and reassigning candidates. Decisions are saved continuously,
the session is resumable, and the final approved set exports to
`data/corpus/manifest.json` — the input to `src/rra/ingest.py`.

Triage criteria:

**Approve:** foundational/canonical guidance for its cluster; covers a
regulatory concept (not a specific device type); currently in force.

**Reject:** Class II Special Controls Guidance for a specific device
type; niche device-class-specific docs; Small Entity Compliance Guides
when the parent guidance is present; anything pre-2000 unless
foundational (the 2002 General Principles of Software Validation is
foundational; most older docs are superseded).

**Unsure:** title ambiguous; could be foundational or device-specific
without reading the PDF. These get manual PDF inspection.

## Coverage analysis — Pass 1 (representative artifact)

The following is the verbatim Pass 1 output from the dual-Claude coverage
analysis on the first scraper run (~147 candidates). It is preserved here
because the *methodology* of producing this output is part of the
project's design narrative, and the *findings* drove concrete scraper
changes.

### Distribution at a glance

Counting by the scraper's cluster field (~147 candidates total):

| Cluster                  | Count | vs. target (10–15)        |
|--------------------------|-------|---------------------------|
| pathway-classification   | ~95   | 🚩 wildly over (>25)      |
| clinical-evidence        | ~22   | over, and noisy           |
| design-controls-qms      | ~14   | in range, but missing anchors |
| software-samd-ai         | 9     | slightly light            |
| modification-decisions   | 5     | 🚩 at the floor           |
| cybersecurity            | 2     | 🚩 under (<5)             |

**Headline problem:** pathway-classification is ~65% of the corpus and
is mostly device-specific noise, while the two clusters most central to
the use case (modification-decisions, cybersecurity) and the AI-specific
part of software-samd-ai are thin.

### pathway-classification — over-represented, heavy noise

Foundational docs present and good: 510(k) Substantial Equivalence
(82395), Abbreviated 510(k) (72646), De Novo Process (72674), De Novo
Acceptance Review (152657), Refuse-to-Accept 510(k) (83888), Benefit-Risk
for SE (89019), Benefit-Risk PMA/De Novo (99769, 115672), Q-Submission
Program (114034), HDE Program (74307), PMA Acceptance/Filing (83408),
Determination of Intended Use (72446), Class II exemptions (72685,
89238), Third-Party Review (85284).

Noise is enormous — dozens of single-device-class 510(k)/PMA guidances
that are not foundational regulatory concepts: Surgical Masks, Menstrual
Tampons, Latex Condoms, Pulse Oximeters, Tonometers, Dental
Handpieces/Curing Lights, Keratome Blades, Wheelchairs, Exercise
Equipment, Brachytherapy Sources, Bone Anchors, Biliary Stents,
Atherectomy/PTA Catheters, etc. Easily 50+ of these.

**Missing foundational pathway docs to verify and add:**

- Breakthrough Devices Program — major modern designation pathway,
  conspicuously absent
- Safer Technologies Program (STeP) — companion to Breakthrough
- 513(g) Requests for Information — core "is this a device / what class"
  pathway
- Foundational IDE submission guidance (have IDE Decisions at 81792, but
  not the foundational IDE submission doc)
- Combination-product classification (RFD / Requests for Designation),
  if the corpus touches combination products

### modification-decisions — at the floor, but the core is solid

Present and genuinely foundational: both "Deciding When to Submit a
510(k) for a Change" (99812) and the software variant (99785), Special
510(k) (116418), PMA Supplement Decision-Making (81431), 30-Day
Notices/PMA Supplements (72663). That's the canonical core for the
"do we need a new submission?" question.

Count looks low only because related modification docs were scattered
into pathway-classification: Real-Time PMA Supplements (73126), Annual
Reports for PMA (73391), Enforcement Policy for PMA/HDE Supplements
(138265). Reassigning these brings the cluster to ~8 without
re-scraping.

### cybersecurity — under-represented

Present: Postmarket Cybersecurity (95862) and the 2026 premarket omnibus,
Cybersecurity QMS + Premarket Submissions (119933).

**Missing / worth targeted re-scrape:**

- Section 524B "cyber devices" content (PATCH Act) — if a standalone
  524B/RTA-for-cyber-devices guidance exists separate from 119933
- Cybersecurity for Networked Devices Containing Off-the-Shelf Software
  (2005) — older but still cited

Two docs is defensible only treating cyber as minor; for a
compliance-analyst tool, push to 4–6.

### software-samd-ai — slightly light, missing the AI-specific anchors

Strong on software functions: General Principles of Software Validation
(73141, foundational), Off-the-Shelf Software (71794), Device Software
Functions/Mobile Medical Apps (80958), Content of Premarket Submissions
for Device Software Functions (153781), Multiple Function Devices
(112671), CDS Software (109618), PCCP for AI-Enabled DSF (166704), CAD
Radiology pair (77635/77642).

**Missing the headline AI/ML items:**

- Good Machine Learning Practice (GMLP) Guiding Principles — absent.
  Likely filtered because it's "Guiding Principles," not a formal
  Guidance Document
- SaMD Clinical Evaluation (IMDRF-based, ~2017) — absent
- AI/ML-Based SaMD Action Plan (2021) — absent (also not a formal
  guidance)
- Jan 2025 draft "AI-Enabled Device Software Functions: Lifecycle
  Management and Marketing Submission Recommendations" — likely
  filtered as draft

### design-controls-qms — in count, but canonical anchors missing

Good coverage on human factors (80481, 163694, 171855), recalls (89909,
136987, 110457), postmarket surveillance (81015), home-use design
(84830), biocompat via ISO 10993-1 (142959), interoperability (95636).

**Missing most-cited QMS foundations:**

- Design Control Guidance for Medical Device Manufacturers (1997) — the
  canonical design-controls doc. Its absence is the biggest single gap
  in this cluster
- QMSR / 21 CFR 820 harmonization with ISO 13485 (final rule Feb 2024,
  effective Feb 2026) — likely filtered as a rule, not a guidance
- Medical Device Reporting (MDR) — postmarket adverse-event reporting;
  foundational and absent
- A dedicated ISO 14971 risk-management application guidance

Minor noise to drop: Metallic Plasma Sprayed Coatings (74184, 2000) and
Dear-Doctor-Letters-for-ICDs (71206, narrow).

### clinical-evidence — over-counted by noise

Real anchors present: Real-World Evidence (190201), PROs (77832, 141565),
Adaptive Designs (92671), Animal Studies general (93963), Acceptance of
Clinical Data FAQ (111346), IDE Decisions (81792).

The 22 is inflated by:

- Generic human-subjects / IRB / informed-consent / monitoring docs
  that are really CDER/OHRP, not device clinical-evidence methodology:
  75222, 83121, 83801, 85183, 116754, 116850, 121479, 117042
- Device-specific clinical-investigation docs: Urinary Incontinence
  (71054), BPH (79397), Prostate Ablation (128263), Power Morcellation
  (159294)

Note: Pivotal Clinical Investigations design (87363) is labeled
design-controls-qms but belongs here — a cluster-fix.

### Document types filtered out that should probably be included

- **Draft guidances in software-samd-ai** where no final exists — the
  Jan 2025 draft "AI-Enabled Device Software Functions: Lifecycle
  Management and Marketing Submission Recommendations" is the obvious
  one
- **Non–"Guidance Document" communication types** that are nonetheless
  foundational: GMLP Guiding Principles, AI/ML Action Plan, QMSR final
  rule

## Scraper changes driven by Pass 1

Based on the coverage analysis, `scripts/scrape_fda_corpus.py` was
enhanced:

- **Keyword additions** in five clusters to catch missing foundationals
  (breakthrough device, 513(g), QMSR, GMLP, design control, MDR, ISO
  14971, lifecycle management, cyber device, real-time pma, etc.)
- **`DEVICE_SPECIFIC_HINTS` exclusion list** to reassign device-class
  guidances out of pathway-classification
- **Communication-type filter loosening** to allow Guiding Principles
  and similar non–"Guidance Document" types
- **`--include-drafts`** to catch the Jan 2025 AI Lifecycle draft

## Maintenance

The corpus is a living artifact. Triggers to revisit:

- **Recall@10 stalls below 0.75** after chunking tuning — the corpus
  may have a content gap. Re-run Pass 1 analysis to verify.
- **A new foundational guidance is issued by FDA** — add to
  `manifest.json`, re-run targeted ingest with `--guidance-id <id>` (if
  ingest supports it) or full re-ingest.
- **Eval questions consistently fail** on a specific topic — check
  whether the corpus contains the supporting docs; if not, expand.

Do NOT silently grow the corpus past ~100 documents. Beyond that, the
"defensible in an interview" property weakens and ingest costs grow
without proportional retrieval benefit.

## Related

- [ADR 0007 — Corpus scope and curation methodology](decisions/0007-corpus-scope-and-methodology.md)
- [spec.md §4.4 — Chunking strategy](spec.md)
- [scripts/scrape_fda_corpus.py](../scripts/scrape_fda_corpus.py)
- [scripts/review_corpus.py](../scripts/review_corpus.py)
