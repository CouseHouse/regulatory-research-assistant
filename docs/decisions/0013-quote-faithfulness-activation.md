# 0013 — Quote-faithfulness: analyst-emitted supporting quotes

**Status:** Active
**Date:** 2026-06-02
**Owner:** Kyle Couse
**Amends:** 0006 — supersedes **only** its "`quoted_text` is verified as a
substring of the stored chunk text" clause. The `guidance_id:chunk_index`
addressing decision in ADR 0006 remains Active and unchanged.

> **ℹ️ Superseded-in-action by the Day-8 critic-flip (2026-06-05).** The Decision's
> clause "the critic stays in key-existence mode until τ is calibrated" no longer
> holds: the critic now passes the analyst's `<q>` supporting quote into
> `check_citation` (`src/rra/agents/critic.py:298-303`), activating in-loop
> quote-faithfulness. The **CI gate remains key-existence** (ADR 0012 D2 —
> unchanged). A ratifying ADR is pending; see
> `docs/decisions/PENDING-DECISIONS.md` Decision 1.

## Context

`Citation.quoted_text` was populated by slicing the retrieved chunk itself
(`passage.text[:150]`, api.py), so feeding it back into `check_citation`
(the ADR 0010 matching engine) asked "is a prefix of the chunk inside the
chunk?" — always `verified=True`. The check was vacuous **by construction**,
and ADR 0006's "verified as a substring" clause both encoded and froze that
tautology. We want `check_citation` to measure something real, which it can
only do if `quoted_text` is the analyst's *own* supporting words rather than
text we sliced out of the source.

## Decision

`Citation.quoted_text` becomes the analyst's verbatim supporting span — emitted
inline as `[guidance_id:chunk_index]<q>…</q>`, **not** guaranteed to be a literal
substring of the stored chunk — and quote-faithfulness is measured **post-graph**
by `check_citation` (the ADR 0010 engine) and reported as a per-citation
`similarity_score`, while the critic stays in key-existence mode until τ is
calibrated against that distribution.

## Alternatives considered

- **Keep the `passage.text[:150]` slice (status quo)** — Rejected. It is the
  tautology itself: a chunk prefix is always a substring of the chunk, so the
  faithfulness number is a constant 1.0 that measures nothing.
- **Quote *inside* the bracket, `[guid:idx "quote"]`** — Rejected. Breaks all
  three independent address parsers (the API resolver's anchored
  `^([^\]:]+):(\d+)$`, the critic's `\[([^:\]]+):(\d+)\]`, and the eval's shared
  pair parser), and `"`/`]` inside FDA quotes collide with the delimiters.
- **Trailing citation block or JSON sidecar after the prose** — Rejected. The
  block's own `[guid:idx]` lines get re-counted by the address parsers (double
  count) and drift from the inline cites on revision; JSON-after-prose is the
  exact Day-6 failure that made `key_fact_coverage` N/A on all 30 cases.
- **Restructure the analyst into structured (tool-use / JSON) output** —
  Rejected as too invasive for this change: it breaks `answer = draft`, the
  critic reading `draft` prose, and revision-in-place. Revisit only if the whole
  answer pipeline goes structured.
- **Wire faithfulness into the critic's `revise` signal now** — Rejected *for
  this ADR*. τ = 0.85 is uncalibrated (config.py); a near-miss on an honest
  quote split by a boilerplate seam would trigger revision churn on a correct
  draft (ADR 0010's documented risk). Measure the distribution first; closing
  the loop is a calibration-gated follow-up.

## Consequences

**Enables:**
- The ADR 0010 three-step matching engine finally runs on real input (the live
  product surface via the API resolver, and the measurement via the eval).
- A genuine quote-faithfulness signal plus a per-citation `similarity_score`
  distribution — the data needed to calibrate τ (and to decide whether corpus
  cleaning, Priority 3, must precede calibration).
- Per-occurrence quotes: the same chunk cited for two claims carries two quotes,
  because each quote is physically adjacent to its citation bracket.

**Constrains:**
- `quoted_text` is **no longer a guaranteed substring** of the chunk. Any
  consumer that assumed a verbatim chunk prefix (e.g. a future UI highlight)
  must treat it as the analyst's span and verify via `check_citation`.
- `char_start`/`char_end` continue to address the **chunk** (ADR 0006), not the
  quoted span; sub-span offsets (`matched_doc_span`) are additive and deferred.
- An empty/missing analyst quote must be **counted as "no quote"** (faithfulness
  unassessable) — never scored faithful-by-emptiness, and never back-filled with
  the slice. One shared parser feeds both the API resolver and the eval so the
  two cannot drift.

**Reopen if:**
- The measured `similarity_score` distribution supports a defensible τ — then
  upgrade the critic to pass the analyst quote into `check_citation`, closing the
  loop (unfaithful quote → `revise` → fix).
- Corpus cleaning (Priority 3) materially changes the distribution — recalibrate
  τ against the clean signal rather than tuning it down to mask corpus noise.
- A UI requires highlighting the quoted span — pull the deferred
  `matched_doc_span` offset refinement forward.

## What changed (supersedes a clause of ADR 0006)

ADR 0006's Decision states that `quoted_text` "is verified as a substring of the
stored chunk text." That clause no longer holds: the substring guarantee was only
ever true because the value was sliced out of the chunk, which is precisely the
tautology this ADR removes. The decision to have the analyst emit its own
supporting span — so that faithfulness becomes measurable — forces dropping the
substring guarantee in favour of an engine-verified `similarity_score`. Everything
else in ADR 0006 (the `guidance_id:chunk_index` address, server-resolved char
offsets, the rejected alternatives) remains valid and Active; only the
`quoted_text` substring clause is superseded.

## Related

- ADR 0006 (citation span addressing) — amended here; addressing scheme intact.
- ADR 0010 (`check_citation` matching contract) — this ADR resolves its named
  "Day 7 architecture question": activation is **post-graph** (eval scorer + API
  resolver), not a new graph node or a pre-critic resolution move.
- ADR 0012 (eval-harness scoring and CI policy) — the no-quote count is the
  direct analog of D1's "zero-citation count must never become a hiding place";
  CI stays key-existence (D2), faithfulness runs only on golden/out-of-band.
- ADR 0008 (LangGraph state shape) — unchanged: no new `GraphState` field; the
  quote rides inside `draft`.
- docs/plan/day07-quote-faithfulness.md — the full wiring analysis behind this ADR.
- spec.md §4.7 (`check_citation`), §6.2–6.3 (citation scoring).
