# Day 7 · Priority 2 — Quote-faithfulness wiring plan

**Status:** Plan / for review — **no code written, no pipeline run.**
**Date:** 2026-06-02
**Owner:** Kyle Couse
**Resolves:** ADR 0010's named "Day 7 architecture question" (matching engine activation).
**Decision already made (input to this plan):** the analyst emits **actual supporting quotes**, not the server-generated `passage.text[:150]` slice.

This document answers the six wiring questions, gives the exact load-bearing edit with line evidence, picks the verification ordering with justification, and ends with a flagged list of every place the tautology could silently come back. The job here is to find where it breaks before we touch code.

---

## 0. Orientation — current state (verified against the repo)

### 0.1 The tautology, stated precisely (with line evidence)

`_resolve_citations()` populates `quoted_text` from a slice of the retrieved passage:

```python
# src/rra/api.py:164–171
citations.append(
    Citation(
        guidance_id=guidance_id,
        chunk_index=chunk_index,
        char_start=passage.char_start,
        char_end=passage.char_end,
        quoted_text=passage.text[:150],   # ← api.py:170  THE LINE THAT MUST CHANGE
    )
)
```

Its own docstring admits the circularity:

> "quoted_text is the first 150 chars of the chunk, which is a verified substring **by construction**." — api.py:142–144

Why it is vacuous, end to end:

- `RetrievedPassage.text` is the **full stored chunk text, unmodified**: `text=row["text"]` (retrieval.py:142, selected by `_BASE_SQL` from `corpus.chunks`).
- `check_citation` reads the **same** `corpus.chunks.text` for the same `(guidance_id, chunk_index)` (tools.py:213–217).
- Therefore `passage.text[:150]` is a verbatim **prefix** of `chunk_text`. After `_normalize` (tools.py:191–193), `norm_quoted in norm_chunk` (tools.py:271) is **always true** — even a mid-word cut is still a substring.

So feeding `passage.text[:150]` back into `check_citation` measures "is a prefix of the chunk inside the chunk?" → always `verified=True`. The matching engine is real but, on this input, it can only ever say yes. **The fix is to put the analyst's own words into `quoted_text`, then verify those.**

### 0.2 The matching engine that finally runs live (confirm ADR 0010)

`check_citation` (tools.py:196–320) is the ADR-0010 engine:
- **Key-existence mode** (`quoted_text=None`): `verified=True` iff the chunk row exists (tools.py:247–253). This is the *only* mode the live pipeline exercises today.
- **Three-step matching** (`quoted_text` supplied): normalize (tools.py:257–259) → substring + whitespace-flexible regex span recovery (tools.py:271–294) → `SequenceMatcher` **coverage ratio** `longest.size / len(norm_quoted)` ≥ τ (tools.py:301–306). Confirmed: it is the normalize→substring→coverage engine, not `ratio()`.
- τ is `settings.citation_match_threshold = 0.85` (config.py:122) — **uncalibrated** default (ADR 0010 §"Threshold τ").
- The MCP server already exposes the `quoted_text` parameter (server.py:105, 112) — external clients can already call quote-faithfulness. No server change needed.

### 0.3 The three address parsers that consume the inline marker

The inline `[guidance_id:chunk_index]` marker in `draft` is parsed in **three independent places**. Any change to the *bracket* ripples to all three; this is the central constraint on Q1.

| Consumer | Regex / call | Behaviour |
|---|---|---|
| API resolver | `_BRACKET_RE` (api.py:27) + `_SINGLE_CITE_RE = ^([^\]:]+):(\d+)$` (api.py:29) via `_parse_citation_pairs` (api.py:120–132) | **anchored `^…$`** — anything after the digits in the bracket ⇒ no match ⇒ citation silently dropped |
| Critic | `_citation_re = \[([^:\]]+):(\d+)\]` (critic.py:236) | requires `]` immediately after digits; otherwise the citation is never checked |
| Eval runner | `_parse_citation_pairs` (run.py:81, imported from api.py) | same as the API resolver; no dedup at this layer |

### 0.4 How the draft travels to the resolver

`analyst → draft (GraphState, graph.py:70–71) → critic reads it → run_graph returns final_state → api.py reads final_state["draft"] (api.py:89) → _resolve_citations(draft, passages) (api.py:91) → QueryResponse.answer = draft (api.py:112)`.

`GraphState` (graph.py:49–79) has **no field for a quote** — `draft: str` is the only analyst output. The analyst is a **text completion** (analyst.py:199–223), not a tool call; whatever it emits must live **inside the `draft` string**. There is no structured side-channel today.

---

## 1. Quote origin — how the analyst emits a supporting quote

**Recommendation: an inline-adjacent quote envelope `[guid:idx]<q>…</q>`** — the quote immediately follows the citation bracket, wrapped in `<q></q>`. The bracket itself is **byte-for-byte unchanged**.

Example analyst output:
> The device must include validation evidence aligned to intended use [72674:3]<q>a risk-based approach to software validation aligned with the intended use</q>.

### Why this form

1. **Zero change to the three address parsers (§0.3).** `<q>…</q>` lives *outside* the bracket, so `_BRACKET_RE`/`_SINGLE_CITE_RE`, the critic's `_citation_re`, and the eval's `_parse_citation_pairs` all still match `[72674:3]` exactly as today. **Address parsing and quote parsing are fully decoupled** — the single most important safety property: a quote-parse failure can never cost us a citation address.
2. **Per-occurrence quotes, for free.** Each inline citation carries its own quote, so the same chunk cited for two different claims gets two different quotes. No keying/ordering problem.
3. **Survives revision-in-place.** The quote is physically adjacent to its claim, so when the analyst edits a sentence (analyst.py:116–147 revision path) the quote moves with it; there is no separate structure to drift out of sync.
4. **Robust to regulatory text.** Quotes contain `]`, `"`, `(b)(2)`, `21 CFR 820`. `<q>…</q>` with a non-greedy capture is immune to `]`/`"` collisions; `</q>` essentially never appears in FDA prose.
5. **One optional-group regex** parses both: `\[([^:\]]+):(\d+)\](?:\s*<q>(.*?)</q>)?` — group 3 optional ⇒ graceful degradation (citation with no `<q>` → key-existence fallback + log, never a drop).

### Prompt change (analyst.py `_SYSTEM_PROMPT`, ~line 40)

Add one citation rule: *after the `[guid:idx]` bracket, append the shortest verbatim span (≤ ~25 words, single sentence) copied from that passage's `<text>` that supports the claim, wrapped in `<q>…</q>`. Copy characters exactly; do not paraphrase. If you cannot copy a supporting span verbatim, omit `<q>` (do not invent one).* The "shortest verbatim span" instruction is load-bearing for Q4 (short quotes cross fewer dirty-corpus seams) and Q9-#4 (blocks whole-chunk quotes).

### Alternatives considered

| Form | Verdict | Reason |
|---|---|---|
| `[guid:idx "quote"]` (quote **inside** the bracket) | **Rejected** | Breaks two live parsers: `_SINGLE_CITE_RE`'s `^…$` anchor (api.py:29) drops the citation; the critic's `\](critic.py:236)` never matches ⇒ citation unchecked. Even after rewriting both, `"`/`]` inside FDA quotes still collide. Most invasive, least robust. |
| **`[guid:idx]<q>…</q>` (inline-adjacent)** | **Chosen** | Decoupled from all address parsers; per-occurrence; survives revision; collision-proof; one optional regex. |
| Trailing line block `===CITATIONS===\n[guid:idx] "q"` | Viable fallback | Keeps prose clean, but (a) the analyst must mirror every inline cite in a separate list that can drift on revision, (b) the block's own `[guid:idx]` lines would be **re-counted** by `_parse_citation_pairs`/critic regex unless prose and block are split first (double-count bug), (c) needs prose/block separation + stripping. More moving parts, lower model reliability. |
| Trailing JSON sidecar | **Rejected** | JSON-after-prose is the **exact Day-6 failure** that made `key_fact_coverage` N/A on all 30 cases (Haiku wrapped JSON in prose; see test_evals_scorers.py docstring, baseline memory). Re-importing that footgun into the analyst is a step backward. |
| Structured analyst output (tool-use / JSON answer) | **Rejected (too invasive for Day 7)** | Breaks `answer = draft` (api.py:112), the critic reading `draft` prose (critic.py:308–319), revision-in-place, and the draft-preview logging. Disproportionate; revisit only if the whole answer pipeline goes structured. |

---

## 2. Quote survival — the critical path (where the tautology hides)

Trace of a single quote, analyst → response, with every point it could be dropped, overwritten, or re-sliced:

```
analyst writes "[72674:3]<q>…words…</q>"   (analyst.py:219–223, inside `draft`)
   │   ⚠ D-A: analyst omits <q> (compliance)              → fallback decision (no slice!)
   ▼
GraphState["draft"]   (graph.py:70–71; quote rides INSIDE draft, no new field)
   │   ⚠ D-B: revision pass regenerates the sentence       → quote re-emitted with it (acceptable)
   ▼
critic reads draft    (critic.py:161, 308–319; key-existence only — see Q3, does NOT touch the quote)
   ▼
final_state["draft"]  (run_graph → api.py:89)
   ▼
parse_answer(draft)  →  (clean_prose, [(guid, idx, quote|None), …])      ← NEW shared parser
   │   ⚠ D-C: parser drops/mis-binds the quote             → optional-group regex never drops the address
   ▼
_resolve_citations(triples, passages)   (api.py:135)
   │   ⚠ D-D: ***quoted_text = passage.text[:150]***  ← THE LOAD-BEARING EDIT (api.py:170)
   │   ⚠ D-E: slice fallback `quote or passage.text[:150]` → tautology returns silently
   ▼
Citation.quoted_text = <analyst quote>      QueryResponse.answer = clean_prose (stripped of <q>)
```

### The load-bearing edit (D-D), stated precisely

**Delete** `quoted_text=passage.text[:150]` (api.py:170) and the docstring claim at api.py:142–144. **Replace** with the analyst-emitted quote for that citation. Concretely, restructure as:

- Introduce one **shared parser** (new `src/rra/citations.py`, or a function in `analyst.py`) imported by **both** api.py and evals/run.py — this is the only way to stop the two parsers drifting (see Q9-#5):

  ```python
  _CITE_Q_RE = re.compile(r"\[([^:\]]+):(\d+)\](?:\s*<q>(.*?)</q>)?", re.DOTALL)

  def parse_answer(draft: str) -> tuple[str, list[tuple[str, int, str | None]]]:
      triples = [(m[1], int(m[2]), (m[3] or None)) for m in _CITE_Q_RE.finditer(draft)]
      clean_prose = re.sub(r"\s*<q>.*?</q>", "", draft, flags=re.DOTALL)  # keep [guid:idx], drop <q>
      return clean_prose, triples
  ```

- `api.py::query` calls `clean_prose, triples = parse_answer(draft)`, returns `answer=clean_prose`, and passes `triples` to `_resolve_citations`.
- `_resolve_citations` (api.py:135) keeps dedup-by-key (first occurrence wins for the **response** shape) and sets:

  ```python
  quoted_text = quote if quote else ""     # NO slice fallback — ever (D-E)
  ```

  When `quote` is `None`/empty: emit `quoted_text=""` **and** `log.info("citation.no_quote", …)`. Empty `quoted_text` is the honest signal "analyst gave no quote"; the report counts it (Q9-#1/#8).

> char_start/char_end stay the **chunk** offsets (api.py:168–169) — they address the chunk per ADR 0006, which is still correct. Refining them to the matched sub-span (`matched_doc_span`) is additive and **deferred** (note the coherence caveat: with a real quote, the offsets describe the chunk, not the quoted span).

### Drop-point summary

| Point | Risk | Mitigation in this plan |
|---|---|---|
| D-A | Analyst omits `<q>` | `quoted_text=""` + log + **counted in report**; no slice fallback |
| D-B | Revision regenerates sentence | Quote is adjacent → re-emitted with the edit; acceptable |
| D-C | Parser mis-binds quote | Optional capture group ⇒ address always recovered |
| **D-D** | **Slice still generated** | **Delete api.py:170; use analyst quote** |
| D-E | Slice fallback sneaks back | **Forbidden**; `"" + log + count` instead |

---

## 3. Verification ordering — resolving ADR 0010's open question

ADR 0010 framed the choice as **(a)** move `_resolve_citations` before the critic, or **(b)** a post-resolution verification pass outside the critic node. **That framing is partly obsoleted by the decision the analyst emits the quote:** `quoted_text` now exists the moment the draft exists, so it no longer has to be *manufactured* by `_resolve_citations` to reach anyone. The quote is already in `draft` before the critic, before the API.

**Decision — Day 7 activates quote-faithfulness POST-GRAPH only:**

- **Eval `CitationValidityScorer`** calls `check_citation` **with** the analyst's quote (the measurement). *(Q5)*
- **API `_resolve_citations`** stops slicing and emits the real quote into `Citation.quoted_text` (the product surface). *(Q2)*
- **The critic stays in key-existence mode for Day 7** (critic.py:256–261, `quoted_text=None` — unchanged).
- This is a **(b)-style** post-resolution pass that runs **outside the LangGraph graph** (eval harness + API boundary). The critic node is **not moved**; resolution is **not moved before it**. No new graph node.

### Justification

1. **Graph shape preserved (ADR 0008).** No field added to `GraphState`, no new node, the one-writer rule is intact (analyst still solely owns `draft`). Moving resolution into the graph would force a checkpointer-schema change and updates to every full-state test (ADR 0008 "Constrains") — disproportionate for activating a measurement.
2. **Critic's role stays clean (ADR 0010 separation).** `check_citation` answers "is this quote in the text?"; the critic answers "does this text support the claim?" Wiring quote-matching into the critic's `revise` signal collapses that separation.
3. **Measure before you close the loop (the τ-safety argument).** τ=0.85 is uncalibrated (config.py:122; ADR 0010 §τ). If the critic issued `revise` on a near-miss (e.g. 0.83 < 0.85), a faithful-but-boilerplate-split quote becomes "definitive evidence the citation is wrong" (ADR 0010's critic-handling table) → hard note → revision churn → possible `cap_hit` on a correct draft. ADR 0010 explicitly warns of this. **You must measure the similarity-score distribution before any control loop reacts to τ.** Day 7 measures; the critic loop is a *later* step, gated on calibration.
4. **Respects the Day-7/Day-8 boundary.** Staying out of the critic node avoids the `source_text` truncation / cache cost-paths that are explicitly Day 8.

**Deferred (with trigger):** upgrade the critic to pass the analyst quote into `check_citation` (closing the loop: unfaithful quote → revise → fix) **after** τ is calibrated against the Day-7 distribution. Reopen when the measured distribution shows a defensible τ.

---

## 4. Dirty-corpus interaction — and why τ is the open variable

Genuine analyst quotes will hit the two documented corpus defects (ADR 0010 §Context): PDF-embedded newlines (~74% of chunks) and mid-sentence "Contains Nonbinding Recommendations" boilerplate at chunk boundaries.

- **Whitespace / newlines → absorbed.** `_normalize` collapses all whitespace (tools.py:191–193) before matching, so PDF newlines inside an honest quote disappear and Step-2 substring (tools.py:271) succeeds. This class is handled.
- **Boilerplate seam → the real failure mode.** The coverage metric is `longest *contiguous* match / len(quote)` (tools.py:301–303). If a boilerplate header is spliced **into the middle** of an honest quote's span, the quote splits into two contiguous halves; the longest single run is ~half ⇒ coverage ≈ 0.5 < τ=0.85 ⇒ **false `verified=False` on an honest quote.** Coverage ≥ 0.85 means "85% of the quote is one contiguous block," which is *stricter* than "85% of the words appear."
- **Short-quote prompting** (Q1) is the cheap mitigation: a ≤25-word single-sentence quote is far less likely to straddle a seam or a chunk boundary.

**τ is so-far-uncalibrated and possibly load-bearing.** Day 7's measurement job (Q5) is to **emit the per-citation `similarity_score` distribution** and inspect:
- how many honest quotes land in the **0.5–0.84 band** (boilerplate-seam victims vs. genuine misquotes), and
- whether the Step-2 substring path already handles the vast majority (in which case τ is incidental, per ADR 0010 §τ).

This is exactly **why Priority 3 (re-chunk / clean corpus) follows P2**: cleaning removes the seams, so honest cross-boundary quotes match and τ can be set against a clean signal instead of compensating for corpus noise. **Flag:** do not "tune τ down" to paper over a dirty corpus — that would hide the corpus problem behind a loosened ruler. Measure first; clean (P3); then calibrate.

---

## 5. Scorer impact — activating quote-faithfulness in the eval

The scorer currently runs **key-existence** (scorers.py:92–93 calls `resolves(guid, idx)` → `check_citation(quoted_text=None)`, run.py:120–124). Activation requires the quote to reach the scorer. Changes:

1. **`AgentResponse.citations` gains a quote per pair** — `{"guidance_id", "chunk_index", "quoted_text"}` (scorers.py:46–47). This is a **value-shape** change inside the existing `citations: list[dict]` field, **not** a new `AgentResponse` field — so `tests/test_evals_scorers.py` (which builds `AgentResponse(..., citations=[])`, line 44) is unaffected.
2. **`run_agent` (run.py:63–90)** uses the shared `parse_answer` (Q2) instead of `_parse_citation_pairs`, attaching each citation's quote:
   `citations = [{"guidance_id": g, "chunk_index": i, "quoted_text": q} for g, i, q in triples]`.
3. **`make_resolver` → `make_verifier` (run.py:110–124):** the callback becomes `verify(guid, idx, quoted_text) -> (verified: bool, similarity_score: float | None)`, calling `check_citation` **with** the quote. Keep a `quoted_text=None` path for the CI fixture (see point 5).
4. **`CitationValidityScorer.score` (scorers.py:79–104):** call `verify` with `c["quoted_text"]`; count a citation valid iff faithful; **record `similarity_score` per citation in `detail`** so `write_report` can build the τ-distribution (Q4). Add the **no-quote guardrail**: count citations whose `quoted_text` is empty/None and surface that count prominently — the direct analog of ADR 0012 D1's "zero-citation count must never become a hiding place." Without it, an analyst that stops emitting quotes would show a *falsely perfect* faithfulness mean as fewer cases carry quotes.
5. **CI stays key-existence (ADR 0012 D2 preserved).** `_make_ci_response` (run.py:93–105) builds citations from `case.ci_citations` with **no quotes**; `verify` with `quoted_text=None` ⇒ key-existence. So the **CI hard gate is unchanged** (deterministic, zero-API-cost, fixture-keyed). Quote-faithfulness is produced **only on the full/golden out-of-band run** — consistent with ADR 0012 D2 and P2 ("full eval out-of-band"). Whether quote-faithfulness ever *becomes* a CI gate is a post-calibration decision; it cannot today without running the graph (API calls) in CI, which D2 forbids.

> Net: the eval gains a **new quote-faithfulness number** (golden runs) alongside the **unchanged key-existence gate** (CI). The Day-6 baseline (`citation_validity = 1.000`, key-existence; baseline memory) remains non-comparable to the Day-7 faithfulness number (ADR 0012 P2 triple-confound).

---

## 6. Backward compatibility — what breaks

| # | Breaks | Detail / fix |
|---|---|---|
| 1 | **`Citation.quoted_text` semantics flip** (slice → analyst quote) | Schema docstring "Verified to be a substring of the stored chunk text" (schemas.py:31–34) becomes **false**. ADR 0006's "verified as a substring" clause no longer holds. **Requires ADR 0013** (below) + docstring rewrite. Shape unchanged; **semantics changed** — a frozen-contract-semantics change. |
| 2 | `tests/test_api.py:167 test_quoted_text_is_substring_of_chunk` | Asserts the old invariant (`quoted_text in sample_passage.text`) — **will fail** on any honest non-substring quote. Reframe to the new semantics (quoted_text == the analyst's emitted span; faithfulness reported separately). |
| 3 | `tests/test_api.py:147` + `mocked_stack` fixture (test_api.py:77–81, 164) | The mock draft has `[72674:3]` and **no `<q>`** ⇒ post-change `quoted_text=""` ⇒ `assert len(cit["quoted_text"]) > 0` fails. Update mock drafts to include `<q>…</q>`. |
| 4 | API `answer` text | `<q>…</q>` must be **stripped** before returning `answer` (api.py:112) or it leaks into the user answer. Handled by `parse_answer`'s `clean_prose` (Q2). Inline `[guid:idx]` markers stay (current behaviour). |
| 5 | Response consumers assuming the slice | Anything assuming `quoted_text` is a verbatim chunk prefix (UI highlight, etc.) mis-behaves on unfaithful quotes. **No UI exists yet** (future-work) — low real risk, flagged. |
| 6 | `Citation.char_start/char_end` coherence | Now describe the chunk, not the quoted span. Unchanged for P2 (chunk is the ADR-0006 address); optional `matched_doc_span` refinement is additive/deferred. |
| 7 | `tests/test_mcp_tools.py` | **No break** — already tests `check_citation` *with* `quoted_text` (lines 265–423). Confirms the engine is unit-tested; P2 just feeds it live input. |

---

## 7. Flagged — every way the tautology could silently reproduce

This is the crux. Each item is a place where the change could "pass" while measuring nothing.

1. **Slice fallback in `_resolve_citations`** — `quote or passage.text[:150]`. Reinstates the tautology for every quote-less citation **and** makes the eval (if it mirrors the fallback) read a vacuous 1.0. **Forbidden.** Use `"" + log + count`.
2. **Eval re-slices** — `run_agent` building `quoted_text` from `passage.text` instead of the analyst's `<q>`. Measures the slice. **Forbidden.** Eval reads the analyst quote only.
3. **Empty/whitespace quote → vacuous `verified=True`** — `check_citation` treats empty `norm_quoted` as key-existence (tools.py:261–268). A parser that yields `""` would score faithful-by-emptiness. Treat empty parsed quote as **"no quote"** (count it); never pass `""` as a faithful match.
4. **Whole-chunk quote** — if the analyst quotes the entire chunk, it is trivially a substring. Not strictly the slice tautology but a degenerate quote. Mitigation: "shortest verbatim span ≤25 words" prompt (Q1); optionally flag over-long quotes in the report.
5. **Parser drift** — two parsers (api.py vs run.py) diverging so one counts quotes and the other slices ⇒ numbers disagree silently. **One shared `parse_answer`** imported by both (Q2). This is the highest-probability regression vector.
6. **`check_citation` fed the passage as source** — any "optimization" that passes `passage.text` as the source instead of letting `check_citation` read `corpus.chunks` independently would re-introduce circularity. The tool already reads the DB itself (tools.py:213–217); **do not change that.**
7. **Faithfulness computed on the CI fixture** — the CI fixture has no quotes; a faithfulness number there would be vacuous. Keep CI key-existence; faithfulness only on golden (Q5-#5).
8. **The no-quote count is not surfaced** — without a prominent "N citations had no analyst quote" line in every report, an analyst that degrades to emitting no quotes shows a *rising* faithfulness mean as the denominator shrinks (the metric improves as the system worsens — exactly ADR 0012 D1's hiding-place failure). **The count is a required guardrail, not optional.**

---

## 8. Required ADR before implementation — proposed ADR 0013

Per CLAUDE.md ("decision changes need a matching ADR"; "never edit the Decision section of an Active ADR — supersede it"), the `quoted_text` semantics change **must** be ratified before code. Draft to write at implementation start (next ADR number is **0013**; index.md last row is 0012):

> **ADR 0013 — Quote-faithfulness activation and `quoted_text` semantics.**
> Supersedes ADR 0006's "quoted_text is verified as a substring" clause. **Decision:** `Citation.quoted_text` is the analyst's verbatim supporting span (emitted inline as `[guid:idx]<q>…</q>`), **not** guaranteed to be a substring; faithfulness is measured separately by `check_citation` (ADR 0010 engine) and reported via the eval's per-citation `similarity_score`. Resolves ADR 0010's "Day 7 architecture question": activation is **post-graph** (eval scorer + API resolver); the critic stays key-existence until τ is calibrated. Update index.md (0006 → "amended by 0013"; add 0013 Active).

This plan contains the ADR's full substance, so ratifying it is mechanical.

---

## 9. Implementation order (AFTER approval — not now)

1. Ratify **ADR 0013**; update `index.md` and the schemas.py / api.py docstrings.
2. Add the shared `parse_answer` parser (one module, imported by api.py + run.py).
3. Analyst prompt: add the `<q>…</q>` citation rule (analyst.py `_SYSTEM_PROMPT`).
4. `api.py`: wire `parse_answer`; **delete api.py:170 slice**; emit real quote; return `clean_prose`.
5. Evals: `AgentResponse` quote field; `run_agent` quote attach; `make_verifier`; `CitationValidityScorer` faithfulness + `similarity_score` capture + **no-quote count**; `write_report` distribution + guardrail line.
6. Fix tests `test_api.py` (#2, #3 above); add a faithfulness/parser test (honest match, boilerplate-seam miss, no-`<q>` fallback, empty-quote).
7. **Run the eval** (`uv run python -m rra.evals.run` on golden) — read the `similarity_score` distribution; decide whether τ holds or P3 (clean corpus) must precede calibration. Do **not** declare done on green CI alone (CI is still key-existence).

---

## 10. Open questions for review (flag anything off)

- **Quote form:** I recommend inline-adjacent `<q>…</q>` over a trailing block on decoupling + revision-survival + model-reliability grounds (Q1). If you'd rather keep prose pristine of `<q>`, the trailing line-block is the fallback — but it carries the double-count and drift risks noted.
- **Critic-in-loop, deferred:** I am deliberately **not** wiring faithfulness into the critic's `revise` signal this day (Q3, τ-safety). If you want the closed loop sooner, we calibrate τ first on the Day-7 distribution, then do it as a fast follow.
- **char offsets:** keeping chunk-level `char_start/char_end` for P2; `matched_doc_span` refinement deferred. Flag if a UI need pulls that forward.
- **ADR 0013:** written at implementation start, not now (this is plan-only).

**STOP — for review. No code will be written until this plan is approved.**
