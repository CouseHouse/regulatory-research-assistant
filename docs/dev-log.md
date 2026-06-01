# Dev log

## 2026-05-31 — Title-shape regex patterns for device-specific filtering

Added `DEVICE_SPECIFIC_TITLE_PATTERNS` (10 compiled `re.IGNORECASE` patterns) to `scripts/scrape_fda_corpus.py` and updated `is_device_specific()` to check them before the keyword/prefix checks. Patterns target the structural shapes that FDA uses for single-device-class 510(k) submission guides: "Premarket Notification [510(k)] Submissions for X", "Guidance Document for X 510(k)s", "510(k) Submissions for X", "Submission Guidance for a 510(k)", etc. Compiled at module load; `search()` short-circuits before the keyword scan.

**Why:** Keyword-only matching (`DEVICE_SPECIFIC_HINTS`) peaked at ~57% noise in `pathway-classification` (80 of 141 candidates). Many older 510(k) submission guides use obscure clinical device names (keratoprosthesis, phacofragmentation, embolic protection, biological indicator) that no keyword list would enumerate exhaustively. Structural patterns capture the class regardless of device name.

**Before/after (no-verify, live FDA index, 2026-05-31):**
- Before: 141 total candidates, 80 pathway-classification, 351 dropped to device-specific/unclassified
- After: 129 total candidates, 68 pathway-classification, 363 dropped to device-specific/unclassified
- Net: 12 additional entries demoted by structural patterns; foundational docs (The 510(k) Program, Abbreviated 510(k), De Novo Classification Process, Refuse-to-Accept, Q-Submission, Special 510(k)) all survived in pathway-classification or modification-decisions.

**Note:** Two entries named in the task spec as expected-to-filter did not match the specified patterns and remain in pathway-classification: "Guidance on 510(k) Submissions for Keratoprostheses" (title uses "510(k) Submissions" not "Premarket Notification") and "Pulse Oximeters - Premarket Notification Submissions [510(k)s]" (device name leads the title). The structural patterns are additive; these can be caught in a future pass with broader patterns or by adding "pulse oximeter" / "keratoprosth" to `DEVICE_SPECIFIC_HINTS`.

## 2026-05-31 — Shared token-bucket rate limiter

Added `src/rra/rate_limit.py` — a stdlib-only token-bucket limiter (`RateLimiter` + `RateLimitStats`) with thread safety via `threading.Lock`, structlog observability, and a read-only `stats` property for end-of-run reporting. Wired to two callers: the scraper (`scripts/scrape_fda_corpus.py`, replacing the flat `INTER_REQUEST_DELAY = 0.15` constant with a proper 5 rps / burst-10 limiter and two new CLI flags `--rate-per-second` / `--burst`), and the ingest pipeline (`src/rra/ingest.py`, which previously had no limiting at all). The ingest limiter is constructed in `main()` and passed as a parameter to `download_guidances()` — parameter over module-level for testability. Rate is configurable via `DOWNLOAD_RATE_PER_SECOND` and `DOWNLOAD_BURST` env vars (no prefix; pydantic_settings maps field names directly). Default of 5 rps / 10 burst was chosen to be polite to FDA's public endpoints while still completing a 100-doc corpus ingest in ~20 s. Motivation: defensive against accidental retry storms from parallel runs and future callers; also forecloses the per-caller duplication drift the project already paid once (schema-code drift postmortem). One unexpected finding: the pre-existing `# type: ignore[call-arg]` on the `Settings()` singleton was now flagged as unused by mypy strict — the pydantic mypy plugin in the current version handles it cleanly, so it was removed.

## 2026-05-31 — Day 3: Basic RAG, no agents

### Files created

| File | Lines |
|---|---|
| `src/rra/schemas.py` | 58 |
| `src/rra/db.py` | 47 |
| `src/rra/retrieval.py` | 132 |
| `src/rra/api.py` | 174 |
| `tests/test_retrieval.py` | 281 |
| `tests/test_api.py` | 285 |

### Decisions made unilaterally

1. **`register_vector` called on each `get_conn()` borrow, not in pool `configure` callback.**
   The pool's `configure` callback fires in a background thread and races the first
   request when FastAPI's threadpool calls `pool.connection()` before the background
   setup completes. Moving `register_vector(conn)` into `get_conn()` is idempotent and
   eliminates the race unconditionally. The configure callback was removed.

2. **Query embedding passed as a pgvector text literal `'[v1,v2,...]'::vector`, not via adapter.**
   Even with `register_vector` called per-borrow, psycopg3's type adapter for `list[float]`
   did not serialize correctly through the pool (still sent as `double precision[]`, causing
   `operator does not exist: vector <=> double precision[]`). Root cause is not fully
   understood — likely an interaction between psycopg_pool's sync pool, anyio's threadpool,
   and psycopg3's per-connection adapter maps. The fix: format the embedding as
   `"[v1,v2,...]"` (pgvector text input) and cast with `::vector` in SQL. Postgres's own
   text→vector parser is unambiguous and not affected by adapter threading issues.

3. **`_resolve_citations` parses grouped citations `[a:1, b:2]` in addition to `[a:1]`.**
   Claude occasionally puts multiple citations in one bracket even when instructed
   to use one per bracket. The parser now splits on `, ` inside any `[...]` bracket
   and validates each item against `guidance_id:chunk_index` format before resolving.
   The prompt was also strengthened with an explicit example of correct form.

4. **`quoted_text` = first 150 chars of the chunk text (guaranteed substring, no model
   involvement).** The model emits `[guidance_id:chunk_index]` and `char_start/char_end`
   are resolved server-side (ADR 0006). `quoted_text` is a representative excerpt for
   the critic (Day 5) to use as a starting anchor, not the model's selected quote. Day 5's
   `check_citation` tool does the actual claim→chunk verification.

### Deferred items

- ~~**Langfuse trace wiring**~~ — **Shipped in Day 3 follow-up** (see below).
- **Connection pool root cause**: The adapter serialization failure under psycopg_pool + anyio
  threadpool is not fully diagnosed; the text-literal workaround is robust but not elegant.
  Worth filing a psycopg_pool issue or pinning to a tested version.
- **Prompt caching on system prompt**: The system prompt is stable per-session and would
  benefit from Anthropic's `cache_control` headers. Not implemented in Day 3's single-call
  path; most valuable on the critic's system prompt in Day 4's multi-call loop.
- **`app.query_audit` table not populated**: The schema has `app.query_audit` for audit logging.
  Day 3 does not write to it. Day 4 (session-based orchestrator) is the natural place to add it.
- **`_voyage_client` lru_cache + pool interaction**: the singleton Voyage client is technically
  fine (the Voyage SDK is thread-safe), but the lru_cache means test isolation requires clearing
  the cache between runs. Integration tests do this explicitly.

### Open questions for Day 4

1. **register_vector / psycopg_pool threading**: the text-literal workaround is safe, but
   the root cause (why the per-connection adapter registration doesn't propagate) should be
   understood before Day 5 when the MCP server may use the same pool in a different threading
   context.
2. **Corpus content gap**: all 20 ingested documents are device-specific 510(k) submission
   guidelines (suction pumps, tampons, hip systems, etc.). There are no software validation,
   SaMD, cybersecurity, or De Novo guidances — the topics most relevant to the project
   narrative. The manifest.json needs a scraper pass to expand the corpus before Day 6 evals.
3. **Prompt quality**: 9/10 smoke queries produced citations; 1 was out of scope and
   correctly abstained. Quality on in-corpus queries was "good for lookups, adequate for
   synthesis." The single-call architecture has no planner/critic, so synthesis quality is
   limited — this is expected; Day 4 replaces it.

### Surprises / real-world failures

- **`register_vector` adapter threading bug (severity: high)**: The first real-world failure was
  `operator does not exist: vector <=> double precision[]`. The pool's `configure` callback
  approach, which is the recommended pgvector usage pattern, did not work here. The workaround
  (text literal `::vector` cast) took 3 server restarts to diagnose and fix.
- **Corpus content mismatch**: The 20 ingested docs are narrow 510(k) device guidances, not the
  broad regulatory-landscape corpus the project narrative implies. Queries about SaMD, software
  validation, cybersecurity correctly returned "not in corpus" — which is honest but means
  the smoke tests had to be reoriented toward device-submission questions.
- **Citation grouping**: The model ignored "one citation per bracket" instructions and emitted
  `[a:1, b:2, c:3]` grouped citations. Fixed with a more specific prompt and a robust regex parser.

### Token cost (real numbers from smoke tests, analyst_model = claude-sonnet-4-5)

10 queries, each retrieving 5 passages (~2500 tokens of context):

| Stat | Value |
|---|---|
| Input tokens (range) | 2,981 – 3,689 |
| Output tokens (range) | 92 – 841 |
| Median input | ~3,300 |
| Median output | ~390 |
| Voyage embedding | ~$0.0001/query (negligible at free tier) |
| Voyage rerank-2 | ~100 tokens reranked × $0.05/1M = ~$0.000005/query |
| Claude claude-sonnet-4-5 input @ $3/MTok | ~$0.010/query |
| Claude claude-sonnet-4-5 output @ $15/MTok | ~$0.006/query |
| **Estimated total** | **~$0.016–0.020/query** |

Cost will rise ~4× in Day 4 (4 Sonnet calls per query per spec §4.2) → ~$0.06–0.08/query
before prompt caching. With caching on stable system prompts, expect ~50% reduction.

### curl example (stop condition)

```bash
curl -X POST localhost:8000/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: dev-key-change-me" \
  -d '{"query":"What performance testing is required for spinal systems in 510(k) submissions?"}'
```

Sample response (truncated):
```json
{
  "answer": "## Performance Testing Requirements...\n- Evaluate the smallest diameter rod [71604:19]",
  "citations": [{"guidance_id":"71604","chunk_index":19,"char_start":41046,"char_end":43650,"quoted_text":"ified sketches of the major steps\n• identification of each supplemental..."}],
  "passages": [...5 passages...],
  "trace_id": null
}
```

**Eyeball quality:** Good on device-specific lookup questions (Q1–Q9). Correctly abstains when
query is outside corpus scope (Q10 biologics). Weak on synthesis across guidances (corpus is
too narrow for the multi-guidance questions spec §2 envisions — a corpus expansion is needed
before evals). No hallucinated citations in 10 queries; all cited chunks verified against DB.

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

---

## 2026-05-31 — Day 3 follow-up: Langfuse instrumentation

### What was instrumented

Three scopes, all gated on `settings.langfuse_enabled`:

1. **`/query` endpoint** (`src/rra/api.py`) — each request becomes a top-level `SPAN` named `query`. Input is `{query, product_context}`; output is `{answer, citation_count}`. The `trace_id` field in `QueryResponse` now carries the real Langfuse trace ID (was `None` in every Day 3 response).

2. **`search_corpus`** (`src/rra/retrieval.py`) — wrapped in a `RETRIEVER` span. Input is `{query, k}`; output is `{passage_count, passages: [{guidance_id, chunk_index, title, score}]}`. Shows vector recall candidates narrowed by rerank.

3. **Anthropic call** (`src/rra/api.py`) — `GENERATION` span named `anthropic-call` with `model`, full messages array as input, final answer text as output, and `usage_details: {input, output}` token counts.

### SDK version note

`pyproject.toml` declared `langfuse>=2.50.0` but uv resolved to **4.7.1**. The v4 SDK switched from a stateful `trace.span()` / `trace.generation()` API to OpenTelemetry context managers (`start_as_current_observation`). The instrumentation uses `contextlib.nullcontext` as the no-op gate when Langfuse is disabled, so no errors or warnings occur in environments without keys.

### Why it was deferred originally

The Day 3 commit left `trace_id=None  # Langfuse wired in Day 4` in the response and made no entry in `future-work.md`. The reasoning was that the trace object in the planned Day 4 design is naturally tied to a LangGraph run, making it the "right" place to hook in. That's true for the orchestrator-level trace — but the retrieval + single-LLM-call path is already complete and observable now, and deferring left a blind spot precisely when the retrieval pipeline is being tuned.

### Lesson

A `# TODO: wire in Day N` comment in shipped code is invisible to future-work planning. If a feature is genuinely deferred, it belongs in `docs/future-work.md` with a reopen trigger, not in a code comment that no one scans at planning time. The rule going forward: either ship the instrumentation with the feature, or add an explicit entry to `future-work.md` — comments in code don't count as tracking.

---

## 2026-05-31 — cluster field propagation through ingest pipeline

Added `cluster: str | None = None` to `Chunk` and `DownloadedDoc` dataclasses; updated `chunk_text` to accept and thread it; updated `download_guidances` to extract it from each manifest entry with `.get("cluster")` so runs against older manifests (no `cluster` key) produce `None` without error. In `write_to_postgres`, cluster lands as a top-level key in the existing `metadata` JSONB column — `Jsonb({"cluster": ec.chunk.cluster})` — rather than as a new dedicated column. Chose JSONB key to avoid another schema migration and the schema/code drift class of bug documented in the day-2.5 postmortem; the column already exists with default `'{}'::jsonb`, so no DDL change is needed. Queries against it use `metadata->>'cluster'` or `(metadata->>'cluster') = 'software-samd-ai'`. Corpus re-ingest (with the new manifest) is required to populate existing rows.
