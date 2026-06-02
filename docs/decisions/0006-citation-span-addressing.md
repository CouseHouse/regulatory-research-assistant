# 0006 — Citation span addressing: `[guidance_id:chunk_index]`

**Status:** Active
**Date:** 2026-05-31
**Owner:** Kyle Couse

## Context

Answers cite sources inline as `[guidance_id:span]` (spec §5), but "span" is
undefined — and three independent consumers each depend on its exact form, each
with effective veto power: the **response contract** (`Citation`, frozen across
Days 3–5), the **deterministic `CitationValidityScorer`** (string match, gate
≥ 0.95, spec §6.2–6.3), and the **`check_citation` MCP tool** (spec §4.7, the
project's distinctive feature). Critically, the inline citation is emitted by the
**analyst LLM**, so the span must be something a model can faithfully copy from
the passages it was shown — not something it has to compute. The `corpus.chunks`
schema already carries `chunk_index` (with `UNIQUE(guidance_id, chunk_index)`),
`char_start`, and `char_end`.

## Decision

A citation addresses a source by **`guidance_id` + `chunk_index`** — inline form
`[guidance_id:chunk_index]`. The structured `Citation` carries
`{guidance_id, chunk_index, char_start, char_end, quoted_text}`, where the
char offsets are **resolved server-side from the chunk row** (never typed by the
model) and `quoted_text` is verified as a substring of the stored chunk text.

## Alternatives considered

- **Char offsets emitted inline by the model (`[guidance_id:char_start-char_end]`)**
  — Rejected. LLMs cannot reliably reproduce exact integer character offsets;
  `citation_validity` would then measure the model's arithmetic, not its
  grounding, and the ≥ 0.95 gate would fail on copying errors. Offsets belong in
  the *resolved* structured citation, computed from the DB row.
- **Verbatim quoted text as the sole address (`[guidance_id:"…"]`)** — Rejected.
  Brittle for deterministic matching (whitespace, OCR artifacts, quoting drift
  break string equality), bloats the answer, and provides no stable key for
  joins or UI anchoring. Retained instead as a *supplementary* `quoted_text`
  field, verified as a substring of the chunk — not as the address itself.
- **`chunk_index` as the span (chosen)** — A stable discrete key the model copies
  verbatim from the passages it was shown; trivially deterministic to validate
  (row existence under `UNIQUE(guidance_id, chunk_index)`); resolves cleanly in
  `check_citation`; char offsets recoverable from the row for UI highlighting.
- **Synthetic per-query passage label (`[P3]`)** — Rejected. Easy for the model,
  but meaningless outside the request; `check_citation` and the eval scorer need
  a corpus-stable identifier, and a label→chunk map would have to be persisted
  per query anyway. `guidance_id:chunk_index` is already stable and global.
- **Page / section numbers (`[guidance_id:p12]` or `:§4.2`)** — Rejected for v1.
  More human-readable for analyst-facing output, but pypdf parsing does not
  reliably preserve page boundaries post-chunking and FDA section numbering is
  inconsistent across documents; neither is derivable from the current schema
  without re-ingest. Strong reopen candidate if page/section metadata is added.

## Consequences

**Enables:**
- A single deterministic validity check — `(guidance_id, chunk_index)` must exist
  — that measures grounding, not the model's ability to copy numbers, keeping the
  ≥ 0.95 gate meaningful.
- `check_citation(claim, guidance_id, chunk_index)` resolves a stable key to the
  exact stored chunk text for the critic to score the claim against.
- Char offsets and verified `quoted_text` available for a future UI without
  changing the address scheme.
- The response contract is frozen for Days 4 (multi-agent) and 5 (MCP).

**Constrains:**
- Citation granularity is the **chunk** (~512 tokens), not the sentence. A claim
  cites a chunk; claim-level precision rides on the optional `quoted_text`
  substring check, not on the address.
- Depends on `chunk_index` remaining the stable key (it is —
  `UNIQUE(guidance_id, chunk_index)`).
- **Re-chunking changes `chunk_index` values**, so any stored/cached citation
  predating a re-chunk goes stale (ties to the Day 2 "stale high-index rows on
  re-chunk" open question).

**Reopen if:**
- Evals show chunk-level citation is too coarse — i.e. key-fact-coverage or
  position-quality misses trace to "cited the right chunk, but the claim is about
  one sentence in it." Then add verified sentence-level `quoted_text` spans or
  sub-chunk addressing.
- Page/section metadata is captured at ingest — then human-readable
  `[guidance_id:§X]` becomes viable for analyst-facing output.
- Re-chunking becomes routine such that `chunk_index` instability is a problem —
  then move to a content-hash-based stable chunk key.

## Related

- spec.md §4.7 (`check_citation`), §5 (steps 5–6, inline citation form), §6.2
  (CitationValidityScorer), §6.3 (≥ 0.95 gate)
- ADR 0001 (LangGraph) — the critic loop consumes citation verification
- ADR 0002 (pgvector) — `corpus.chunks` schema and the `UNIQUE` key this relies on
- Day 2 dev-log open question: "stale high-index rows on re-chunk"
