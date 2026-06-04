# 0014 — Corpus cleaning + structural chunking

**Status:** Active
**Date:** 2026-06-02
**Owner:** Kyle Couse

> **Validated 2026-06-03 (Resolution A):** Structural chunking re-ingested as the live `corpus.chunks` via `chunk_text_structural + clean_text --truncate`: 2745 chunks across 71 docs, 0 boilerplate rows. Re-measured on the live embedded corpus: recall@10=1.00 (13/13), faithfulness=386/446 at τ=0.85 — both held. The prior "DEFERRED" note (delta=0 from Day-7 smoke) reflected that faithfulness didn't require re-embed; Resolution A re-embeds to align code, local corpus, and Day-9 cloud ingest on one architecture. Restore path: `corpus.chunks_fixedsize_backup` (2726 rows, fixed-size+dirty). See dev-log 2026-06-03.
>
> **Cutover reconciliation (D4a, 2026-06-03):** the cutover was executed via `uv run python -m rra.ingest --truncate`, **not** the atomic RENAME swap that the Decision section (step 4) and the `0006:17` / `0010:12` cross-refs describe. Outcome is equivalent — `--truncate` also clears orphan rows; validated **386/446** faithfulness + **recall@10=1.00**. The "swap" references are the *documented-preferred* approach; `--truncate` is the *executed* one — both acceptable per this ADR (§Alternatives: "`--truncate` remains viable on a dev box"). Append-only note; Decision/Context/Consequences bodies unchanged.

## Context

The Day-7 quote-faithfulness smoke produced a baseline of **18/47 analyst-emitted quotes verified at τ=0.85** on the dirty corpus. ADR 0010 §Context documents the two root causes:

1. **Mid-sentence PDF newlines** (~74% of chunks): pypdf inserts `\n` at every visual line wrap; a model quoting the source verbatim may produce a quote that straddles a newline, causing `_normalize` to collapse it to a space while the stored chunk text still contains the break — but Step 2 (substring) passes normalization, so this is only a problem when the quote is long enough to straddle a chunk *boundary* (see cause 2).

2. **"Contains Nonbinding Recommendations" boilerplate** spliced mid-sentence at page breaks: pypdf's page join (`ingest.py:203`) inserts the running FDA header between the last sentence of one page and the first of the next. This splits an honest quote into two halves, each ~50% of the quote, producing Step-3 coverage ≈ 0.5 < τ regardless of chunk placement. This is the dominant failure mode identified in the Day-7 faithfulness run.

3. **Fixed-size 512-token boundaries with no structural awareness**: a boundary falling mid-sentence guarantees that some quotes straddle two chunks; Step-3 coverage against either chunk ≈ 0.5 < τ. ADR 0006 §Consequences flagged that re-chunking leaves orphan high-index rows via the in-place `ON CONFLICT ... DO UPDATE` mechanism, with no safe path to a smaller chunk count.

A text-only validation path is possible because `check_citation` reads `text`, never `embedding` (ADR 0010 §0.6). This makes it possible to validate cleaning and chunking strategy against text alone before paying for a re-embed.

## Decision

We will:

1. **Add `clean_text(raw: str) -> str`** applied at `ingest.py:464` before `chunk_text`, stripping FDA boilerplate headers ("Contains Nonbinding Recommendations", page numbers), repairing hyphenated line breaks, and joining mid-sentence single-`\n` wraps to spaces while preserving paragraph breaks (`\n\n`) for structural splitting.

2. **Replace the fixed-size tiktoken window** (`chunk_text`, `ingest.py:208–269`) with a hand-rolled structural splitter (`chunk_text_structural`) that splits on paragraph (`\n\n`) then sentence boundaries, packing segments greedily to the same 512-token budget with 50-token soft overlap. No new dependency; `langchain-text-splitters` is rejected (§"Alternatives").

3. **Validate on text before paying for re-embed**: clean and re-chunk into a scratch table (`corpus.chunks_rechunk`) with text + offsets but no embedding column. The live `corpus.chunks` table remains untouched during validation. A `match_quote` pure function (extracted from `check_citation`) runs the ADR-0010 three-step algorithm directly against scratch table rows — `$0`, no Voyage call. The smoke reports two numbers: best-chunk X/47 and doc-level X/47.

4. **Re-embed only after the text-only smoke validates the strategy.** Re-embed proceeds only after best-chunk X/47 clears the bar (or the gap analysis resolves the next action). Implementation is atomic-table-swap: build `corpus.chunks_rechunk` with embeddings, build the HNSW index, then `BEGIN; RENAME chunks → chunks_old; RENAME chunks_rechunk → chunks; COMMIT;`. This eliminates orphan rows (the ADR 0006 Day-2 open question) by replacing the whole table rather than upserting into it.

## Alternatives considered

- **`langchain-text-splitters` `RecursiveCharacterTextSplitter`** — Rejected. `langchain-text-splitters` pulls `langchain-core` and, while a text-splitter is data-prep not orchestration, it violates the spirit of ADR 0003's "no LangChain in the dependency tree" constraint. The hand-rolled paragraph→sentence packer is ~40 lines, gives equivalent boundary control, and adds no new dependency.

- **`--truncate` in-place re-ingest** — Rejected for the validation phase. `--truncate` produces an empty-corpus window during the re-index and cannot be iterated cheaply (each iteration costs a full Voyage re-embed). Scratch table isolation lets cleaning/chunking parameters be tuned at $0 per iteration. For the final atomic swap, `--truncate` remains viable on a dev box but is not preferred.

- **In-place upsert re-chunk** — Rejected. `ON CONFLICT (guidance_id, chunk_index) DO UPDATE` leaves orphan rows when a doc re-chunks to fewer pieces. High-index phantom rows would pass `check_citation`'s key-existence check and corrupt the CI gate. Atomic swap eliminates this.

- **Cleaning after chunking** — Rejected. `chunk_text` derives `char_start`/`char_end` from the text it was handed (§0.3 of the rechunk plan). Cleaning stored chunk text post-hoc shifts character positions without updating the offsets, producing incoherent `matched_doc_span` values. Cleaning before chunking keeps all three fields self-consistent by construction.

## Consequences

**Enables:**
- Honest FDA quotes that currently fail (boilerplate splice or boundary straddle) can verify at τ=0.85, making the faithfulness metric meaningful.
- Structural boundaries make it much more likely a quote drawn from coherent source text lands within a single chunk.
- Atomic table-swap resolves the Day-2 open question (ADR 0006 §Consequences): orphan high-index rows are impossible when the whole table is replaced.
- Text-only iteration loop: cleaning/chunking parameters can be tuned at $0, re-embedding only once after the strategy is confirmed.
- `match_quote` as a pure function: the smoke and `check_citation` share identical matching logic — no drift between the diagnostic and production paths.

**Constrains:**
- Re-chunk renumbers all `chunk_index` values. Citations stored/cached before this work go stale (ADR 0006 §Constraints — known, accepted). Golden `notes` with "#N" chunk references become stale prose; `expected_guidance_ids` (doc-level) survive unchanged.
- τ must be recalibrated against the clean corpus after the swap; never tuned against the dirty-corpus distribution (ADR 0013 §Reopen).
- `embedding` must be nullable in `corpus.chunks_rechunk` (the live table has `embedding NOT NULL`).

**Reopen if:**
- The text-only smoke shows best-chunk X/47 is much lower than doc-level X/47 (gap quantifies residual straddle) → build Lever D (multi-chunk verification, design in `day07-priority3-rechunk.md` §6).
- `recall@10` degrades post-swap (structural boundaries yield smaller usable chunks for some docs) → tune separator list or token budget.
- τ-calibration post-swap shows the clean distribution warrants a different default (ADR 0013 reopen trigger).

## Related

- ADR 0006 (citation span addressing) — `guidance_id:chunk_index` addressing unchanged; Day-2 orphan-row open question resolved here via atomic swap.
- ADR 0010 (`check_citation` matching contract) — the two corpus defects documented in §Context are cleaned here; the matching algorithm and τ are unchanged. τ recalibration follows the swap.
- ADR 0013 (quote-faithfulness activation) — τ must be recalibrated against the clean distribution post-swap; never tuned to mask dirty-corpus noise.
- `docs/plan/day07-priority3-rechunk.md` — full design with line-level evidence, cascade analysis, and smoke decision table.
- spec.md §4.4 — chunking strategy updated in the same commit.
