# Day 7 · Priority 3 — Corpus clean + semantic re-chunk (PLAN ONLY)

**Status:** Draft for review. **Nothing cleaned, re-chunked, or re-embedded.** STOP-for-review at the end.
**Date:** 2026-06-02
**Owner:** Kyle Couse / Claude

**Goal of the work this plans:** move the Day-7 quote-faithfulness smoke from its dirty-corpus
baseline of **18/47 verified at τ=0.85** upward, by (A) cleaning PDF boilerplate/newlines at
ingest and (B) replacing fixed-size chunking with structure-aware chunking — and to **prove the
strategy on text before paying for a re-embed.**

---

## 0. Orientation — how it works *today* (verified against the repo, with line evidence)

### 0.1 Chunking is fixed-size token windowing, not structural

`chunk_text` (`ingest.py:208–269`) is a pure `tiktoken` (`cl100k_base`) sliding window:

- `size = settings.chunk_size_tokens` (512), `overlap = settings.chunk_overlap_tokens` (50) — `ingest.py:231–232`, `config.py:126–127`.
- Loop `ingest.py:242–267`: `end = min(start+size, n)`; `chunk_str = enc.decode(all_tokens[start:end])`; advance `start = end - overlap`.
- **No structural awareness.** Boundaries fall wherever the 512-token count lands — mid-sentence, mid-word-fragment, mid-boilerplate. This is the root cause of boundary-straddle (`ingest.py:223–228` already flags `RecursiveCharacterTextSplitter` as the deferred fix, gated on spec §4.4's "recall@10 < 0.75" trigger).

### 0.2 There is no cleaning stage anywhere

`parse_pdf` (`ingest.py:200–203`) is `"\n".join(page.extract_text() or "" for page in reader.pages)` — raw pypdf, page-joined with `\n`. The per-doc pipeline in `main()` is:

```
ingest.py:464   text = parse_pdf(doc.path)            # raw, dirty
ingest.py:466–472  (skip if < PDF_MIN_TEXT_LEN)
ingest.py:474   chunks = chunk_text(text, ...)        # chunk the raw text
ingest.py:475   embedded = embed_chunks(chunks)
ingest.py:476   write_to_postgres(embedded)
```

**The clean hook belongs at `ingest.py:464`**, between `parse_pdf` and `chunk_text`: `text = clean_text(parse_pdf(doc.path))`. Cleaning *must* precede chunking (see §0.3).

### 0.3 char offsets are computed from the token stream `chunk_text` is handed — this is the offset-safety lever

`chunk_text` derives offsets by decoding the token prefix (`ingest.py:248–250`):

```
prefix_str = enc.decode(all_tokens[:start])
char_start = len(prefix_str)
char_end   = char_start + len(chunk_str)
```

So `char_start/char_end` are offsets into **whatever string `chunk_text` received** — today the raw text, tomorrow the cleaned text. The stored triple `(text, char_start, char_end)` is **self-consistent by construction**, because `chunk_text` is the sole producer of all three from one token stream.

**Invariant the re-chunk must preserve:** clean **once, before** `chunk_text`; never mutate stored chunk text after chunking. Then offsets remain valid against the stored text automatically — no offset-shift bookkeeping needed. `check_citation` only ever uses offsets *within a single stored chunk's text* (`tools.py:287–289`, `doc_start = char_start + m.start()`); nothing maps them back to the original PDF, so cleaning shifting absolute positions is harmless as long as the row is internally consistent. (No UI consumes offsets yet — `day07-quote-faithfulness.md` §6 row 6.)

### 0.4 Writes are in-place upsert — **not** idempotent on a shrinking chunk count

`write_to_postgres` (`ingest.py:328–381`) upserts with `ON CONFLICT (guidance_id, chunk_index) DO UPDATE` (`_UPSERT_SQL`, `ingest.py:334–341`). There is **no scratch table** and **no orphan cleanup**:

- Re-chunk to the **same/more** chunks → fine (overwrite / insert).
- Re-chunk to **fewer** chunks → **old high-index rows linger as phantom-valid keys.** If `184856` goes 97 → 80 chunks, indices 81–96 survive and `check_citation` would resolve them. This is the Day-2 open question ("stale high-index rows on re-chunk", `dev-log` 2026-05-31 Day-2 postmortem; ADR 0006 §Consequences:80–83). **This is the one genuinely load-bearing cascade item** (§5).
- `--truncate` (`ingest.py:431–438, 446–449`) does `TRUNCATE … RESTART IDENTITY` — full-table wipe, all docs at once. Blunt but correct for a full re-chunk.

### 0.5 Re-chunk **requires** re-embed (confirmed)

`embed_chunks` embeds `c.text` (`ingest.py:280` → `_embed_batch`, `input_type="document"`, `ingest.py:315–323`). The stored vector is a function of the stored text. Change boundaries (different text per chunk) or change text (cleaning) and every stored vector is stale → retrieval would rank by old vectors while returning new text (silent recall loss + vector/text incoherence). **Re-chunk ⟹ mandatory re-embed.** No way around it.

### 0.6 `check_citation` matches **TEXT**, never the embedding — this is what makes the cheap path possible

`check_citation` (`tools.py:196–329`):

- SQL selects `text, char_start, char_end` only (`tools.py:216–220`); the `embedding` column is **never read**.
- Matching runs on `row["text"]` (`tools.py:246`): `_normalize` (`191–193`) → normalized-substring + whitespace-flexible regex (`280–303`) → `SequenceMatcher` **coverage** ratio `longest/len(norm_quoted)` ≥ τ (`305–321`, τ at `314` ← `config.py:122`).

**Therefore the faithfulness smoke can run on re-chunked text with zero embeddings** — `check_citation`'s answer depends only on `text`. (This is the foundation of §3.)

### 0.7 The current corpus and the matching engine

- **71 distinct guidances, 2726 chunks** (`day06-golden-blueprint.md:16`). `184856` = 97 chunks (`ci_key_fixture.jsonl:2` comment). Manifest has 72 entries, 0 marked not-ok (one, `73126`, ingested to 0 chunks — `dev-log` golden notes).
- Analyst now emits `[guid:idx]<q>…</q>`; `parse_answer` (`citations.py:43–67`) yields `(guidance_id, chunk_index, quoted_text|None)`. Eval taps it in `run_agent` (`run.py:90–94`); `make_verifier` (`run.py:129–151`) feeds `check_citation`; `_faithfulness_summary` (`run.py:212+`) aggregates the distribution. **The 18/47 baseline is the output of this path on the dirty corpus.**

### 0.8 ADRs in scope

- **ADR 0006** — `guidance_id:chunk_index` is the citation address; re-chunk renumbers `chunk_index` → stale cached citations (`0006:80–83`); reopen "re-chunk becomes routine → content-hash key" (`0006:91–92`). Address scheme stays; **not** superseded by this work.
- **ADR 0010** — the matching engine the clean corpus feeds; documents the two corpus defects (`0010:15`) and that coverage is *contiguous* (boundary-sensitive). τ=0.85 uncalibrated (`0010:116–122`).
- **ADR 0012 P2** — Day-6/7/post-re-chunk numbers are triple-confounded (ruler, substrate, corpus); only a full fresh run compares. `key_fact_coverage` is boundary-independent and stays comparable.
- **ADR 0013** — analyst-emitted quote, post-graph measurement, critic stays key-existence until τ is calibrated; reopen: "corpus cleaning materially changes the distribution → recalibrate against the clean signal, don't tune τ down to mask noise" (`0013:79–80`).

---

## 1. Lever A — Cleaning (what gets stripped, where it hooks, why offsets stay valid)

**Where:** a new `clean_text(raw: str) -> str`, applied at `ingest.py:464` — `text = clean_text(parse_pdf(doc.path))`. Pre-chunk, so §0.3's offset invariant holds for free.

**What gets stripped / repaired** (the three documented defects, ADR 0010:15):

1. **"Contains Nonbinding Recommendations" boilerplate** — the running header FDA stamps on every page; pypdf's page-join (`ingest.py:203`) splices it *mid-sentence* at page breaks, which is precisely what splits an honest quote into two contiguous halves → coverage ≈ 0.5 < τ (the dominant failure mode, `day07-quote-faithfulness.md` §4). Strip via a whitespace-flexible, case-insensitive regex wherever it appears, plus its common companions ("Draft — Not for Implementation").
2. **Running headers/footers** — page numbers / "Page X of Y", date stamps, "U.S. Food and Drug Administration", repeated title lines. **v1 (recommended start):** targeted regexes for these known FDA patterns — low risk, directly removes seams. **v2 (escalate only if the smoke shows residual header noise):** repeated-line detection — any short (≤ ~10-word) line recurring on ≥ N pages is boilerplate. Starting targeted keeps blast radius small and is re-measurable cheaply (§3).
3. **PDF-embedded mid-sentence newlines (~74% of chunks)** — pypdf inserts `\n` at every visual line-wrap. Repair conservatively: join a single `\n` between a continuing line and the next (`(?<=[a-z,;])\n(?=[a-z])` → `" "`), join hyphenated splits (`-\n` → `""`), but **preserve `\n\n` and `\n` after sentence punctuation** as structural paragraph markers — Lever B needs those boundaries. (Note: `_normalize` already neutralizes newlines for *matching*; cleaning them additionally improves chunk-boundary quality and readable stored text.)

**Why offsets stay consistent:** cleaning runs entirely before `chunk_text`; `chunk_text` recomputes `char_start/char_end` from the cleaned token stream (§0.3). No retro-fitting of stored offsets. The only semantic shift: `matched_doc_span` becomes "document-relative within cleaned text" — which has no external consumer (§0.3).

---

## 2. Lever B — Semantic chunking (replace the fixed window)

**Approach:** structure-aware splitting that prefers paragraph → sentence → clause boundaries under the **same 512-token budget with 50-token overlap** (hold size/overlap constant so we isolate the *boundary-quality* change from any retrieval-size effect; recall stays comparable).

**Why it cuts boundary-straddle:** an honest quote is almost always a sentence or a clause. Breaking only at sentence/paragraph ends keeps a quote whole inside one chunk; overlap insures the case where a sentence lands exactly on a boundary. Contrast today: a 512-token cut mid-sentence guarantees some quotes straddle two chunks, and coverage is *contiguous* so a straddle scores ≈ 0.5 (ADR 0010; `day07-quote-faithfulness.md` §4).

**Implementation choice — a review decision point (do not pick for me silently):**

| Option | What | Cost | Fit |
|---|---|---|---|
| **A (recommend)** | `langchain-text-splitters` `RecursiveCharacterTextSplitter.from_tiktoken_encoder(chunk_size=512, chunk_overlap=50, separators=["\n\n","\n",". ","; ",", "," ",""])` | New declared dependency (pulls `langchain-core`); ~10 lines | Spec §4.4-sanctioned (`ingest.py:223–228`); token-budget-preserving; least custom code |
| **B** | Hand-rolled paragraph→sentence packer (split on `\n\n`, sentence-segment, greedily pack to ≤512 tok with 50 tok overlap) | No new dep; ~40 lines + tests | Aligns with ADR 0003's "no LangChain wrappers" ethos; full control; more to maintain |

I lean **A** for spec-alignment and minimal surface, but flag the tension with ADR 0003 (which rejected LangChain *orchestration* wrappers — a text-splitter is a data-prep utility, arguably out of 0003's scope, but the "keep LangChain out of the tree" spirit is real). New dep ⟹ one-line justification in the commit body + the ADR (§5.6). **Your call at review.**

Keep **overlap = 50**; if the smoke shows residual straddle, raising overlap is a cheap text-only lever (re-measure via §3 before re-embed).

---

## 3. The cheap-measure path — validate on TEXT before paying Voyage

Rests on §0.6 (`check_citation` reads text, not vectors). **Recommended mechanism: scratch table + standalone matcher.** In-place-with-rollback is **rejected** — `embedding` is `NOT NULL` (`db.py:404`) so you'd need dummy vectors, and mutating `corpus.chunks` (even transactionally) risks breaking live retrieval and churns the HNSW index for nothing.

**Sequence:**

**Step 0 — Lock the baseline as an artifact.** Persist the P2 dirty-corpus golden run's per-citation `(guidance_id, chunk_index, quoted_text, similarity_score, verified)` set — that's the **47 analyst quotes** and the **18/47** number. Source: the most recent P2 golden run's `citation_validity` detail (`scorers.py:137,141–143` already records faithfulness + invalid-with-quote), or one fresh golden run (analyst API cost only — **no** re-chunk/embed). The 47 quotes are the fixed comparison input; they are the analyst's *own words*, independent of chunk boundaries.

**Step 1 — Build scratch TEXT (no embeddings).** Create `corpus.chunks_rechunk` (same columns, `embedding` nullable/omitted, no HNSW). For every PDF: `clean_text(parse_pdf(pdf))` → semantic `chunk_text` → write `text, char_start, char_end, chunk_index`. **Live `corpus.chunks` is untouched — retrieval stays up throughout iteration.**

**Step 2 — Refactor the matcher into a pure function.** Extract `match_quote(quoted_text, chunk_text, char_start) -> (verified, similarity_score, span)` from `check_citation` (the §0.6 Step-1→3 body). Both `check_citation` and the smoke then call the **identical** algorithm, so the smoke measures exactly what production will (no drift — the `day07-quote-faithfulness.md` §7-#5 hazard).

**Step 3 — Run the smoke on the 47 quotes against scratch.** Because `chunk_index` is renumbered, do **not** replay old `(guid, idx)` pairs. Instead, per quote, report **two** numbers:

- **Best-chunk match (production-realistic):** over all scratch chunks of that `guidance_id`, take the max coverage; verified iff ≥ τ. → the new **X/47**, directly comparable to 18/47 (simulates "analyst cites the chunk its quote lives in").
- **Doc-level match (isolates Lever A):** match against the full cleaned doc text (concat of that doc's scratch chunks). → upper bound — how many honest quotes become faithful once boilerplate/newlines are gone, ignoring boundaries.

Emit both + the per-quote similarity distribution.

**Step 4 — Decide cheaply (iterate on text, $0, seconds):**

| Smoke reading | Diagnosis | Action |
|---|---|---|
| best-chunk **X ≫ 18** and ≈ doc-level | Levers A+B work | → proceed to re-embed (§4) |
| best-chunk low, **doc-level high** | cleaning works; boundaries still straddle | tune Lever B (overlap/separators) and/or schedule **Lever D** (§6); re-run §3 |
| **doc-level low** | cleaning insufficient | tune Lever A regexes; re-run §3 |

Iterate Steps 1–3 entirely on text. **Re-embed only after best-chunk X/47 clears the bar.**

> **Why this matters more than the Voyage dollars (see §4: it's cents):** the cost the cheap path actually saves is the **iteration loop** — each full re-embed + HNSW rebuild + full golden re-run burns real analyst/judge API spend (~$2+/run, Day-4 cost datum) and minutes, and churns the live corpus. Validating cleaning/chunking on text first lets you tune the regexes and separators for free, embed **once**, and keep Levers A/B (faithfulness) cleanly separated from re-embed (retrieval recall) so the two measurements don't confound.

---

## 4. Re-embed — the single Voyage spend (only after §3 validates)

**Cost estimate (voyage-3, `config.py:116`):**

- Tokens to embed ≈ raw corpus tokens + overlap re-embedding ≈ **1.3–1.5M** (2726 chunks today at 512/50; cleaning trims a few %, semantic boundaries shift the count but not the total text much).
- voyage-3 list ≈ **$0.06 / 1M tokens** → **≈ $0.08–0.09** (a dime). **$0.00 under the active 200M free-token grant** (`dev-log` 2026-05-31: "still in the 200M free-token grant"). **< $0.20 even at 2× the estimate.**
- *Confirm current Voyage list price at implementation; the order of magnitude (cents) is robust regardless.*

**The dollar cost is a rounding error. The real cost of re-embed is operational** — corpus churn + the mandatory full re-validation (§5) — which is exactly why §3 gates it.

**Mechanism — build-into-scratch then atomic swap (recommended over `--truncate`):**

```
1. Build corpus.chunks_rechunk WITH embeddings (clean→chunk→embed→write).
2. Build the HNSW index on the scratch table.
3. BEGIN;
     ALTER TABLE corpus.chunks         RENAME TO chunks_old;
     ALTER TABLE corpus.chunks_rechunk RENAME TO chunks;
   COMMIT;
4. Keep chunks_old until the post-swap golden run is green; then DROP.
```

This (a) **eliminates orphan rows** — the whole table is replaced, not upserted (§0.4); (b) ~0 retrieval downtime; (c) trivially reversible. `rra-ingest --truncate` (`ingest.py:446–449`) is the simpler existing alternative but has an empty-corpus window + live HNSW rebuild — acceptable on a dev box, not preferred. *(Ingest has no scratch/swap/clean/semantic support today — these are the implementation deltas, not for now.)*

---

## 5. Cascade — exactly what re-chunk renumbering touches (verified, corrections flagged)

Re-chunk reassigns every `chunk_index`. What that actually breaks, in priority order:

1. **Orphan rows — the only load-bearing functional item.** In-place upsert leaves stale high-index rows on a shrinking chunk count (§0.4), creating phantom-valid keys `check_citation` would resolve. **Fix:** the atomic swap (§4) or `--truncate` — both replace the whole table. (ADR 0006:80–83; Day-2 open Q.)

2. **Golden `notes` chunk references — stale prose, documentation-only, NOT gating.** Verified: `golden.jsonl` stores **no** machine-checked `chunk_index` — only doc-level `expected_guidance_ids` (`dataset.py:38`) and human `notes` like "Grounded in 184856 #3/#4/#9" / "166704 #18/#29" / "99769 #4/#5/#7". After re-chunk those "#N" point to different text. Re-map or de-reference them for honesty (a reviewer expects "#4" to contain the cited fact). `expected_guidance_ids` (doc-level) survive unchanged.

3. **CI fixture — keys `184856:1,2` do NOT need re-keying** *(correction to the task's framing).* Verified: `load_ci_fixture.py:29–70` **plants** chunks 1 & 2 synthetically (dummy 1024-zero embedding, `ON CONFLICT DO NOTHING`) into a **fresh** CI Postgres (`.github/workflows/evals.yml:55–56`); it never reads the real corpus. Re-chunk cannot break them, and low indices 1/2 + bogus 99999 survive any reasonable re-chunk. **What's actually stale:** the comment *"184856 has 97 chunks"* (`ci_key_fixture.jsonl:2`) → update to the new count; re-confirm 99999 stays absent. The synthetic keys themselves: leave them.

4. **Tests with `chunk_index` literals — re-chunk-safe, no change.** Verified: `test_api.py` (3,0,1), `test_mcp_tools.py` (0,1), `test_citation_validity_scorer.py`, `test_evals_report.py` all use **mocked/synthetic** rows, not live-corpus lookups. None assert a specific real `chunk_index` resolves to specific real text.

5. **Baselines / non-comparability.** Tag a fresh post-clean golden run; keep `day7-prerechunk-baseline` as the *before*. **Do not compare across the re-chunk** (ADR 0012 P2): `citation_validity` + `position_quality` substrate shifts; `key_fact_coverage` (boundary-independent) stays comparable.

6. **ADR 0014 (new) + spec §4.4 — required before code** (CLAUDE.md: decision changes need a matching ADR; new dep needs justification). Draft (do not write now): *"Corpus cleaning + structure-aware chunking"* — adopts `clean_text` and structural splitting, declares the splitter dependency (if Option A), references the ADR 0006:80–83 cascade and resolves it via full-table swap. **Additive — does not supersede** ADR 0006's address scheme. Update spec §4.4 (the `RecursiveCharacterTextSplitter` / recall@10<0.75 framing) in the same commit.

7. **τ-calibration against the clean signal** (ADR 0013:79–80). After the swap + a full golden run, calibrate τ on the *clean* `similarity_score` distribution — never tune τ down to paper over corpus noise.

---

## 6. Lever D — multi-chunk-above-threshold verification (CONDITIONAL — DO NOT BUILD)

**Build only if §3's smoke shows a residual fail band where best-chunk < τ but doc-level ≥ τ** — i.e., honest quotes that straddle even the new structural boundaries (the gap between the two §3 numbers quantifies exactly how much of a problem this is).

**Design sketch (for the record, not for implementation):** extend `check_citation` so that when single-chunk coverage < τ, it also tests the quote against the cited chunk's neighbors (`chunk_index ± 1`), or against the cited chunk concatenated with its neighbor, and verifies if the quote is faithful across the span — returning a multi-chunk `matched_doc_span` or a document-level citation. This is ADR 0006's "sub-chunk / span addressing" reopen (`0006:84–88`) and an ADR-0010 multi-chunk extension; it would need its own ADR.

**Hold it as the residual fix.** Levers A+B (clean + structural boundaries) should remove the bulk of straddle; Lever D only mops up what survives. Decision gated strictly on the §3 result — **note only.**

---

## STOP — for review

This plan changes ingest architecture (new cleaning stage, fixed→structural chunking, likely a new dependency) and touches the corpus substrate. Per the task and CLAUDE.md, **nothing is cleaned, re-chunked, or re-embedded.** 

**Decisions I need from you before any implementation:**
1. **Lever B splitter:** Option A (`langchain-text-splitters`, new dep) or Option B (hand-rolled, no dep)? (§2)
2. **Confirm the cascade corrections** in §5 (CI keys synthetic / golden notes are prose-only / orphan rows are the real risk) — or tell me where I've misread.
3. **Go/no-go on drafting ADR 0014** (§5.6) as the next step, before code.
