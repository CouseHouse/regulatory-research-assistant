# 0007 — Corpus scope and curation methodology

**Status:** Active
**Date:** 2025-05-31
**Owner:** Kyle Couse

## Context

The system answers questions a compliance analyst would ask about FDA
medical device guidance. The corpus IS the substance the system reasons
about — every demo, eval question, and retrieval result depends on it.

A naive corpus (whatever the scraper produces) is ~65% device-specific
510(k) noise and missing foundational guidances like the 1997 Design
Controls doc, the GMLP Guiding Principles, and the QMSR final rule.
The system answers narrow 510(k) questions well and AI/ML, QMS, and
cybersecurity questions badly.

A curated corpus is needed to support the JD-aligned project narrative
and to ensure synthesis questions in the eval golden set have multiple
relevant documents to span.

## Decision

The corpus targets **60–75 final FDA guidances organized into six
topical clusters** (pathway-classification, modification-decisions,
software-samd-ai, cybersecurity, design-controls-qms, clinical-evidence).
Curation uses a **two-stage process**: an automated scraper produces ~150
candidates from the FDA index; a terminal review tool drives manual
approval/rejection. Coverage gaps and noise patterns are surfaced via a
**dual-Claude pattern** — domain analysis in a fresh chat without project
context, decision execution in the project chat with full context.

See [docs/corpus-design.md](../corpus-design.md) for the methodology in
detail and the representative Pass 1 coverage analysis.

## Alternatives considered

- **Take whatever the scraper produces** — Rejected. Initial scrape was
  65% pathway-classification noise (50+ device-specific 510(k) docs) and
  missing foundational anchors like Design Controls 1997, GMLP Guiding
  Principles, and the QMSR rule. The system narrative would be limited
  to 510(k) device questions and would not credibly cover AI/ML.

- **Pure manual curation, no scraper** — Rejected. Browsing the FDA
  guidance index by hand to identify 60+ docs is ~3–4 hours of repetitive
  filtering work that doesn't build domain knowledge proportional to the
  time invested. The scraper makes the candidate set comprehensive; the
  manual review is where judgment happens.

- **Larger corpus (200+ docs)** — Rejected. Beyond ~100 docs, the
  "defensible in an interview" property weakens (can you really claim to
  have read 200 guidances?), ingest costs grow without proportional
  retrieval benefit, and the noise-to-signal ratio degrades.

- **Smaller corpus (20–30 docs)** — Rejected. Synthesis questions in the
  eval golden set need multiple relevant documents to span. With 20 docs,
  most "compare what X and Y say about Z" questions have only one
  document on Z. Multi-agent decomposition has nothing meaningful to
  decompose.

- **Single Claude session for all curation work** — Rejected. The fresh
  Claude chat doing domain coverage analysis benefits from NOT having
  project context (it crowds out general FDA knowledge in the model's
  attention). Decision execution benefits from full project context.
  Splitting across chats produces better outputs for both.

## Consequences

**Enables:**

- A corpus that credibly supports the AI/ML, cybersecurity, and design
  controls parts of the project narrative — not just 510(k)
- Synthesis questions in the eval golden set that span multiple
  guidances by design
- A defensible answer to "how did you pick these documents?" in
  interviews — the methodology IS the answer
- A reusable pattern (`scripts/scrape_fda_corpus.py` + `review_corpus.py`)
  for refreshing the corpus when new guidances are issued

**Constrains:**

- The corpus must be re-reviewed periodically as FDA issues new guidances
  or supersedes existing ones
- The dual-Claude pattern requires manual paste between sessions; no
  automation
- Scraper enhancements (device-specificity exclusion, communication-type
  filter) introduce maintenance — if FDA changes the index schema, the
  scraper breaks

**Reopen if:**

- Recall@10 stalls below 0.75 after chunking tuning AND the corpus is
  suspected of having content gaps (re-run coverage analysis)
- A new foundational FDA guidance is issued that should be added (e.g.,
  the AI Lifecycle draft becomes final)
- Eval questions consistently fail on a specific topic, suggesting the
  corpus is missing supporting docs
- Project scope changes to include pharmaceutical (CDER) or biologics
  (CBER) guidances, requiring a different cluster taxonomy

## Related

- [docs/corpus-design.md](../corpus-design.md) — methodology and Pass 1
  analysis
- [spec.md §6.1 — Evaluation golden set](../spec.md)
- [ADR 0002 — pgvector in Postgres](0002-pgvector-in-postgres.md)
  (the corpus scale assumption underlying the vector store choice)
