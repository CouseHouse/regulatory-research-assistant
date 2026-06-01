# Dev log

## 2026-06-01 — Day 4: LangGraph multi-agent orchestrator

### What was built

Four-node LangGraph state machine replacing the Day 3 single-shot Anthropic call:

```
START → planner → researcher → analyst → critic
                                   ↑           │
                                   │  route_after_critic()
                               revise+count<cap │
                                   └───────────┘ approve/escalate/cap_hit → END
```

**Files created:**
- `src/rra/agents/types.py` — CriticNote, CriticOutput internal types
- `src/rra/agents/planner.py` — Sonnet; tool-based decomposition (PlannerOutput)
- `src/rra/agents/researcher.py` — Haiku; query reformulation + direct search_corpus call; chunk dedup
- `src/rra/agents/analyst.py` — Sonnet; synthesis + edit-in-place revision; _format_user_prompt moved here
- `src/rra/agents/critic.py` — Sonnet; context-match citation check; sets revision_count and cap_hit
- `src/rra/graph.py` — GraphState TypedDict (13 fields), PostgresSaver checkpointer, run_graph()

**Files updated:**
- `src/rra/api.py` — replaced Anthropic call block with run_graph(); kept _resolve_citations unchanged
- `src/rra/schemas.py` — added QueryResponse.warning: str | None (ADR 0008 additive extension)
- `src/rra/config.py` — planner/analyst/critic model defaults updated to claude-sonnet-4-6
- `tests/test_api.py` — updated mocking layer to patch rra.api.run_graph; all assertions unchanged

**Tests added:** test_graph.py (4 routing scenarios), test_agents.py (per-agent contract tests).

### Decisions made

**test_api.py mocking update:** The task asked for test_api.py to "pass unchanged" but the old patches (`rra.api.search_corpus`, `rra.api.Anthropic`) target imports that no longer exist in api.py after Day 4. Updated the mocking layer to patch `rra.api.run_graph` instead. All HTTP contract assertions (status codes, response schema, citation resolution, auth) are unchanged. The "unchanged" constraint means contract preservation, not frozen test internals.

**Prompt caching placement:**
- Planner: system prompt includes 3 few-shot examples (~680 tokens) to push past the 1024-token cache threshold. `cache_control=ephemeral` applied.
- Analyst: system prompt ~500 tokens with formatting rules; estimated ~500 tokens. Applied cache_control; may not cache on every call if under threshold in some environments. The system prompt is stable across all calls (only the user message changes per query).
- Critic: system prompt ~450 tokens with audit instructions. Applied cache_control; same reasoning as analyst.

**Token_usage reducer:** Used `Annotated[dict[str, int], _merge_token_usage]` in GraphState TypedDict so each agent's token keys are merged without node functions needing to read prior state. Keys are unique per agent (e.g., `planner_input`, `analyst_input_rev1`).

**PostgresSaver initialization:** `lru_cache(maxsize=1)` singleton backed by `get_pool()` from rra.db (ADR 0004). `setup()` is idempotent (creates tables if missing, runs pending migrations).

**Graph cache reset in tests:** `_graph` is a module-level singleton. Tests use an `autouse` fixture to reset it to `None` and use `MemorySaver` (LangGraph in-memory checkpointer) via `patch("rra.graph._get_checkpointer", return_value=MemorySaver())`. This avoids DB dependency in unit tests.

**Cap-hit written by critic node:** The design doc mentioned a "thin wrapping node" for cap_hit. Implemented more cleanly: the critic node itself computes `cap_hit = (new_revision_count >= settings.max_critic_revisions)` after incrementing, then `route_after_critic` reads `state["cap_hit"]` directly. One less node in the graph; same semantics.

 Open docs/dev-log.md and replace the two "Not yet recorded" sections with
 the real numbers from this session's smoke test:

 Per-agent token cost (approve path, unforced):
   planner   440 in / 222 out
   researcher 4 calls ~255 in / ~21 out each (query reformulation)
   analyst   10408 in / 1011 out
   critic    12271 in / 50 out
   total     ~25,547 tokens, ~$0.05/query
   (analyst + critic dominate; researcher reformulation is cheap)

 Langfuse trace structure (confirmed):
   query → planner → researcher (4 reformulation generations + 4
   search_corpus retrievers) → analyst → critic. Forced-verdict runs show
   bare critic spans (no generation child, no LLM call).

 Loop verified live (3 modes): revise→cap-out→warning,
   escalate→immediate-exit→warning, unset→approve→null-warning.

 Citation-precision issue: quoted_text spans land on PDF boilerplate
   (line numbers, "Contains Nonbinding Recommendations" headers) rather
   than the claim-supporting sentence. Root causes: (1) chunk text retains
   PDF artifacts, (2) citation resolves to chunk-leading chars. Targets:
   Day 5 check_citation (resolution) + ingest cleaning pass (artifacts).
   This is the Day 7 improvement target + Day 11 postmortem candidate.

### Surprises / open items

- The planner system prompt may not reliably exceed 1024 tokens in all configurations since token count varies by exact prompt text. If cache hit rate is low on the planner, consider adding more few-shot examples in Day 7 (when retrieval recall evals run).
- LangGraph 0.2.50 passes state as `dict[str, Any]` to node functions at runtime even when `StateGraph[GraphState]` is used, requiring `# type: ignore[type-var]` on `add_node` calls. This is a known limitation of LangGraph's TypedDict typing.
- The `_format_user_prompt` move from api.py to analyst.py is a breaking change for any caller that imported it from api.py directly. Exported as `format_user_prompt` from `rra.agents.analyst` with the same signature.

### checkpointer autocommit bug

First real query 500'd: PostgresSaver.setup() runs CREATE INDEX
CONCURRENTLY, which Postgres forbids inside a transaction. The checkpointer
connection was in psycopg3's default transaction mode.

Two fixes: (1) dedicated autocommit connection for the checkpointer,
separate from the ADR-0004 request pool; (2) cache the checkpointer as a
process singleton so setup() runs once, not per request.

Class of bug: same family as the Day 2.5 ingest failures — code passes
unit tests (which mock the checkpointer) but the real-infrastructure
integration surfaces a constraint the mocks can't model. Reinforces why
the live smoke test is a stop condition, not the mocked test suite.

### citation precision observation

The four-agent pipeline works end-to-end (CardioWatch query produced a
correct cross-document synthesis, critic approved, warning=null). But the
resolved quoted_text spans often land on PDF boilerplate (line numbers,
"Contains Nonbinding Recommendations" headers, "contact FDA staff"
footers) rather than the specific sentence supporting each claim.

Two root causes for Day 6/7:
1. Chunk text retains PDF extraction artifacts (line numbers, headers,
   footers) — a cleaning pass during ingest would help.
2. Citation resolves to the chunk's leading chars, not the
   claim-supporting sentence within the chunk. The check_citation MCP
   tool (Day 5) + tighter span resolution is the fix.

This is the kind of thing the citation_validity eval scorer will catch
and quantify on Day 6. Logging now as a known issue, not fixing in Day 4.

---

## 2026-06-01 — Expanded title-shape regex patterns; pathway-classification 68 → 44

Extended `DEVICE_SPECIFIC_TITLE_PATTERNS` from 11 to 21 patterns and added 12 entries to `DEVICE_SPECIFIC_HINTS`.

**Root cause of prior plateau at 68:** FDA uses `(510(k))` with parentheses as often as `[510(k)]` with brackets; the previous patterns only handled the bracket form. Other gaps: "Guidance Document for the Preparation of Premarket Notification for X" has no 510(k) token, mid-title forms need an unanchored pattern, and some device-specific titles (Biological Indicator, Intravascular Administration Sets) need keyword matching.

**New patterns:** parentheses variant of the Premarket Notification anchored/unanchored forms; "Guidance Document for the Preparation of Premarket Notification"; "Guidance on 510(k) Submissions for X"; "Guidance on ... of a Premarket Notification for X"; "Submission of Premarket Notifications for X"; "Recommendations for Premarket Notifications for X"; "X - Submission Guidance for a 510(k)"; unanchored "Premarket Notification [510(k)] Submissions for X"; "Content and Format for Abbreviated 510(k)s for X".

**New hints:** biological indicator, intravascular administration, spinal system, chorionic gonadotropin, phacofragmentation, retinal prosth, pulse oximeter, artificial pancreas, " gown", hypothermic, total artificial disc, medical laser.

**Also added `src/rra/py.typed`** (missing PEP 561 marker; caused mypy `import-untyped` error on `rra.rate_limit` under `--strict`).

**Before/after (--include-drafts --no-verify, live FDA index, 2026-06-01):**
- Before: 135 total candidates, 68 pathway-classification, 401 dropped
- After: 111 total candidates, 44 pathway-classification, 425 dropped

All required spot-checks passed: foundational docs (The 510(k) Program, Abbreviated 510(k), De Novo, Refuse-to-Accept, Determination of Intended Use, Real-Time PMA Supplements) survive; device-specific entries (Pulse Oximeters, Powered Suction Pump, Surgical Gowns, Aqueous Shunts) are absent from pathway-classification.

**Note:** The achievable floor is ~44, not the ~15-20 projected in the task spec. The remaining 44 entries (Benefit-Risk factors, FDA Actions on 510(k)/PMA/De Novo, User Fees, IDE guidance, Q-Submission, Safer Technologies, Breakthrough Devices, etc.) are genuinely cross-cutting pathway docs that structural patterns cannot filter without false positives.

## 2026-05-31 - "Day 3" draft:

## Day 3 — Phase 1 design surfaces

**Phase 1** review caught a correctness issue we'd otherwise have shipped:
ingest uses Voyage `input_type="document"`, so the query path must use
`input_type="query"`. Voyage 3 is asymmetric — symmetric embeddings on
both sides degrade retrieval quality measurably. Folded into ADR 0005
(query-time embeddings) so the rationale is locked.


**Phase 2**  smoke test results (5 queries)

Strong signals captured:

**Refusal works.** Two trap queries against topics the corpus doesn't cover
(SaMD definition, cybersecurity controls) produced informative refusals
that named the missing documents. This is the regulated-vertical refusal
behavior the spec §6.1 hard band tests for — already passing manually
before Day 6 evals. Examples:
- SaMD query: "...you would need to consult other FDA guidance documents
  specifically dedicated to that topic, such as FDA's guidance on
  'Software as a Medical Device (SAMD): Clinical Evaluation,' which is
  not among the passages provided."
- Cybersecurity query: distinguished retrieved "software documentation"
  passages from cybersecurity specifically.

**Synthesis works (mostly).** Multi-part query about 510(k) modification
decisions + Special vs Traditional pulled from 5 distinct guidances and
constructed a structured answer. Citations are approximately correct but
unverified — Day 5's check_citation tool will validate.

**Off-topic refusal works.** Python framework question returned reranker
scores 0.23-0.28 (vs 0.8+ for on-topic) and was refused. Possible future
optimization: skip LLM call when max score < 0.5.

**The diversity issue from the first query (multiple chunks from same
guidance) does NOT appear on the synthesis-type queries.** The reranker
surfaced diverse sources when the query naturally spanned topics. This
suggests future-work §12 (MMR/per-source cap) may be redundant once the
multi-agent on Day 4 generates multiple sub-queries — the planner
naturally creates topic diversity.

Token costs per query: ~3000-3500 input, ~300-400 output. ~$0.015-0.018
per query at Sonnet pricing. Latency 6-8s.

Day 3 confidence: high. Single-shot retrieval+answer endpoint produces
production-credible output on real questions with real refusal behavior.

## 2026-05-31 — Day 2 postmortem: schema drift + systemic ingest hardening

### Root cause

`init-db/01-init.sql` and `_ensure_schema()` in `ingest.py` defined two
completely different tables. In the normal developer workflow (`docker compose up`
→ `rra-ingest`), init-db runs first. `_ensure_schema`'s `CREATE TABLE IF NOT
EXISTS` is then a no-op, so the table keeps init-db's schema. Every INSERT
immediately fails.

Three crash-level drifts, triggered in sequence as each was individually fixed:
1. `token_count` present in code's INSERT, missing from init-db table
2. `UNIQUE (guidance_id, chunk_index)` required by `ON CONFLICT`, missing from
   init-db table
3. `guidance_title TEXT NOT NULL` present in init-db, never written by code

### What was fixed (full enumeration)

**Schema:**
- `init-db/01-init.sql`: added `token_count INT NOT NULL`, `UNIQUE (guidance_id,
  chunk_index)`, changed `embedding` to `NOT NULL`. Added sync comment: both
  files must be updated together.
- `_ensure_schema()`: rewritten to match init-db exactly (full column list,
  same index names, same constraints).
- Running DB (no rows): applied three ALTER TABLE statements directly.
- `_ensure_schema` now called once in `main()` before any download/embed work,
  so schema problems surface before API costs are incurred.

**Ingest hardening:**
- `Chunk` gained `guidance_title` sourced from manifest `"title"` field.
  `_urls_from_manifest()` replaced by `_entries_from_manifest()` returning full
  entry dicts; `guidance_id` now comes from manifest `"id"`, not URL parsing.
- `DownloadedDoc(path, guidance_id, guidance_title)` dataclass threads identity
  through the download → ingest pipeline.
- `_download_one` accepts explicit `guidance_id` param (eliminated URL stem
  heuristic that would silently produce wrong IDs for non-standard URLs).
- `main()` per-doc loop: `parse_pdf → chunk_text → embed_chunks → write_to_postgres`
  wrapped in `try/except`; one bad doc logs an error and continues.
- `--truncate` flag added: TRUNCATEs `corpus.chunks` before ingesting. Use after
  schema changes for a clean-slate re-ingest.
- `_embed_batch` retry predicate narrowed from `Exception` (retried everything,
  including permanent auth and bad-input errors) to a whitelist of retryable
  Voyage error types: `RateLimitError`, `ServerError`, `ServiceUnavailableError`,
  `APIConnectionError`, `TryAgain`, `Timeout`.
- `NotImplementedError` for embedding count mismatch replaced with `RuntimeError`.

**Tests:**
- `tests/test_ingest_integration.py` added (two tests, `@pytest.mark.integration`):
  - `test_write_populates_all_columns`: asserts every column lands in the live DB
  - `test_write_is_idempotent`: two identical writes → exactly N rows
  These two tests would have caught every schema-drift crash before it reached
  a live ingest run.
- `@pytest.mark.integration` registered in `pyproject.toml`.
- Unit tests updated for new `Chunk.guidance_title` field and `DownloadedDoc`
  return type.

### Systemic lesson

The root failure was two files defining the same table independently with no
enforcement that they stayed in sync. The fixes:
1. Added a warning comment in init-db pointing at `_ensure_schema`.
2. Added integration tests that actually write to Postgres — unit tests mocking
   `_ensure_schema` cannot catch schema drift by construction.
3. `_ensure_schema` is now called at the start of `main()` before any
   download/embed work, so schema failures are cheap to discover.

### Open questions (carry forward to Day 6)

1. **Stale high-index rows on re-chunk.** If a document is re-chunked and
   produces fewer chunks than before, old high-index rows linger. The `--truncate`
   flag handles full re-ingests; per-doc cleanup would need a
   `DELETE FROM corpus.chunks WHERE guidance_id = %s` before each upsert batch.
   Accept or fix? Depends on whether re-chunking is needed before evals.

2. **`_embed_batch` creates a new `voyageai.Client` per call.** Fine for the
   batch job, but the query path (day 3+) should share a singleton client.

3. **Partial download file corruption.** `dest.write_bytes()` is atomic if
   the process runs to completion; a mid-write kill can leave a truncated file
   that the cache check (`if dest.exists()`) will accept as valid. Atomic
   write via tmp-file + `os.replace` is the fix. Low priority until a
   corruption event is actually observed.

## 2026-05-31 — Day 2 follow-up: ingest hardening

### Decisions

1. **Replaced `_CORPUS_URLS` with `_urls_from_manifest()`.** The hardcoded list
   was the root cause of 404 failures against real FDA URLs — no one had verified
   them. `_urls_from_manifest()` reads `data/corpus/manifest.json` and skips any
   entry where `verification.ok is False`. The manifest is now the single source
   of truth for which documents to ingest.

2. **Narrowed the tenacity retry predicate on `_download_one`.** The old
   `retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError))`
   retried 4xx responses, burning all 4 attempts on a permanent 404.  Switched to
   `retry_if_exception(_is_retryable)` which retries only on 5xx status codes
   and transient network exceptions (`TimeoutException`, `ConnectError`,
   `ReadError`).

3. **`download_guidances` now fault-tolerant.** One bad document no longer
   crashes the whole batch: each `_download_one` call is wrapped in try/except,
   failures are logged at error level and appended to a failure list, and only
   successful paths are returned. A summary log line (succeeded/failed counts)
   fires after every run.

### Open questions

1. **Manifest `verification.ok` is `"skipped"` for all current entries.** The
   scraper writes `{"ok": true, "reason": "skipped"}` without ever doing a live
   HEAD check. Real verification (HTTP HEAD → confirm 200 + Content-Type PDF)
   would let the filter actually do work. Worth a scraper pass before next ingest
   run.

2. **`--limit` now defaults to `None` (all entries).** The manifest has 20 entries
   in the current snapshot. If the manifest grows large, callers should pass
   `--limit` explicitly to avoid long ingest runs.

### Issues

1. **Voyage rate limit**

After fixing download resilience, hit the next failure mode: Voyage's
free-tier rate limit (3 RPM / 10K TPM). One rate-limit error crashed
the entire ingest, including the 5 successful downloads.

**Resolution:** added payment method to Voyage account (no charge — still
in the 200M free-token grant), unlocks 300 RPM / 1M TPM.

**Lesson:** same fault-tolerance gap as the 404 issue — embedding failure
should not lose download work. Deferred a retry-on-RateLimitError fix
in _embed_batch; the rate limit lift removes the immediate need but
the fault-tolerance issue stands.

**Decision:** keep the deferral on the radar. If Day 6 evals show recall
problems and we need to re-embed the corpus with different settings,
the retry logic becomes worth shipping.

## 2026-05-30 — Day 2: ingest pipeline

### What was built

**Phase 1 (design, Opus):** `docs/ingest-design.md` (266 lines) — full proposal
with function signatures, data carrier types, Postgres schema DDL, six key design
decisions, spec cross-references, and five open questions for the human.

**Phase 2 (implementation, Sonnet):** `src/rra/ingest.py` (348 lines) and
`tests/test_ingest.py` (334 lines).

Key functions in `ingest.py`:
- `download_guidances(limit)` + `_download_one` — httpx download with tenacity retry
- `parse_pdf(path)` — pypdf extraction + scanned-PDF detection (< 500 chars)
- `chunk_text(text, guidance_id)` → `list[Chunk]` — tiktoken sliding window
- `embed_chunks(chunks)` → `list[EmbeddedChunk]` — Voyage batches ≤ 128
- `_embed_batch(texts)` — tenacity-wrapped Voyage call
- `write_to_postgres(chunks)` + `_ensure_schema` — psycopg3 upsert, one txn/doc
- `main()` — argparse entry point; returns int exit code for CI use

18/18 tests pass; `uv run mypy src/rra/ingest.py` clean under strict mode.

### What was deferred and why

- **Natural boundary chunking** (RecursiveCharacterTextSplitter): `langchain-text-splitters`
  is not a declared project dependency and isn't pulled in by `langchain-anthropic`.
  The tiktoken sliding window is used instead. Per spec §4.4, if recall@10 stalls
  below 0.75 the chunking strategy is the first lever to pull — that's when to
  either declare `langchain-text-splitters` as a dependency or implement the
  recursive splitter directly.

- **`download_guidances` for real**: the hardcoded `_CORPUS_URLS` list (8 URLs)
  has never been verified against the live FDA server. The actual download path
  is not covered by tests. See Open Questions #1 below.

- **Connection pooling**: `write_to_postgres` opens a fresh `psycopg.connect()`
  on every call (one per document). For the batch ingest job this is fine;
  the query path will need a shared pool. Noted but deferred — no `get_conn()`
  helper exists anywhere in `src/rra` yet.

### Decisions made unilaterally

1. **Tiktoken windowing instead of RecursiveCharacterTextSplitter.** The design
   doc proposed langchain_core's splitter but that class lives in
   `langchain-text-splitters`, which isn't installed. The tiktoken window produces
   identical chunk sizes and overlaps; the only loss is structural-boundary
   preference. Added a comment in `chunk_text` pointing to the spec §4.4 reopen
   condition so the deviation is visible.

2. **All DDL in a single transaction with the data write.** The design doc showed
   `_ensure_schema` committing separately. Since all DDL is `CREATE IF NOT EXISTS`
   (idempotent), merging it into the document transaction simplifies the code
   without changing semantics.

3. **`_embed_batch` creates a new `voyageai.Client` per call.** Slightly inefficient 
   (~50ms per batch of overhead) but acceptable for the once-a-week ingest job. 
   A lazy module-level singleton via functools.lru_cache would be cleaner and is the right fix; 
   deferred because the query-path code (day 3+) will need a shared client and is the 
   natural place to introduce the helper.

4. **PDF filename stem as `guidance_id`.** Used `path.stem` (e.g., `"72674"` for
   `72674.pdf`). See Open Question #2 for the readability tradeoff.

5. **8-URL hardcoded corpus, marked TODO.** Represents enough variety
   (SaMD, De Novo, 510k, design controls, software validation, cybersecurity)
   to bootstrap the retrieval eval. URLs have NOT been live-verified.

### Open questions to resolve next session

1. **Are the `_CORPUS_URLS` in `ingest.py` correct?** None were verified against
   the live FDA server (`https://www.fda.gov/media/{id}/download`). Before running
   ingest for real, check each URL returns a PDF (not a 404 or redirect to an HTML
   page). Add a `TODO(verify-urls)` search-and-fix pass to the Day 3 checklist.

2. **`guidance_id` = filename stem (e.g., `"72674"`) vs. human-readable title.**
   Numeric IDs are stable and collision-free but opaque in citations. A manifest
   file (`data/corpus/manifest.json`) mapping `id → {url, title, date}` would
   make citations readable without changing the schema — worth doing if you demo
   the system to a non-technical audience.

3. **psycopg directly vs. SQLAlchemy ORM.** The design doc flagged this.
   `write_to_postgres` uses psycopg3 directly for the bulk upsert. If the rest
   of the app (graph.py, MCP server) goes through SQLAlchemy Core/ORM, there may
   be a reason to standardize. No decision forced yet.

4. **Stale high-index rows on re-chunk.** If a re-run produces fewer chunks than
   the previous run (e.g., chunking strategy change), rows with high `chunk_index`
   values linger. The current upsert does not delete orphan rows. Accept or add a
   `DELETE ... WHERE guidance_id = %s` before the insert?

5. **`RecursiveCharacterTextSplitter` boundary preference.** Add
   `langchain-text-splitters` as a declared dependency and swap the chunker after
   baseline recall numbers are in hand (spec §4.4 trigger: recall@10 < 0.75).

### Commands blocked by hooks

None — no hook restrictions were encountered during this session.
