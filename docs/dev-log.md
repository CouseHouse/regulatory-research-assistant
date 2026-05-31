# Dev log

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
