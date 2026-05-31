# Ingest pipeline diagnosis — 2026-05-31

Written before any code changes. This is the Phase 1 deliverable.
Approve this document before Phase 2 begins.

---

## A. Schema-code drift: column-by-column

Two schemas define `corpus.chunks`. They disagree on almost every line.

| Column | `init-db/01-init.sql` | `_ensure_schema()` | `_UPSERT_SQL` writes it? | Crash? |
|---|---|---|---|---|
| id | BIGSERIAL PK | BIGSERIAL PK | no (auto) | — |
| guidance_id | TEXT NOT NULL | TEXT NOT NULL | yes | — |
| **guidance_title** | **TEXT NOT NULL** | **MISSING** | **no** | **YES — #1** |
| section | TEXT nullable | MISSING | no | no (nullable) |
| chunk_index | INT NOT NULL | INT NOT NULL | yes | — |
| text | TEXT NOT NULL | TEXT NOT NULL | yes | — |
| char_start | INT NOT NULL | INT NOT NULL | yes | — |
| char_end | INT NOT NULL | INT NOT NULL | yes | — |
| **token_count** | **MISSING** | **INT NOT NULL** | **yes** | **YES — #2** |
| embedding | vector(1024) **nullable** | vector(dim) NOT NULL | yes | no (code always fills it) |
| metadata | JSONB DEFAULT '{}' | MISSING | no | no (has default) |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | MISSING | no | no (has default) |
| UNIQUE (guidance_id, chunk_index) | **MISSING** | **present** | n/a | **YES — #3** |
| chunks_guidance_id_idx (B-tree) | present | MISSING | n/a | no (perf only) |
| HNSW index name | `chunks_embedding_hnsw_idx` | `chunks_hnsw` | n/a | no (produces duplicate) |

### Crash path in normal workflow

The normal developer workflow is `docker compose up` → `rra-ingest`. In that
order, init-db runs first. `_ensure_schema`'s `CREATE TABLE IF NOT EXISTS` is
a no-op; the table retains init-db's columns.

Failures in order of which error PostgreSQL reports first:

1. **guidance_title NOT NULL, no default, not written** → "null value in column
   'guidance_title' of relation 'chunks' violates not-null constraint"
   Every INSERT fails. The dev-log documents a `token_count` failure; that means
   this run used a `_ensure_schema`-created table, not an init-db-created one.
   In a fresh docker compose setup, guidance_title would have been the first
   crash.

2. **token_count column missing from init-db table** → "column 'token_count'
   does not exist". Every INSERT fails. Documented in dev-log.

3. **UNIQUE constraint missing from init-db table** → "there is no unique or
   exclusion constraint matching the ON CONFLICT specification".
   Every INSERT fails. Documented in dev-log.

Items 1–3 are independent crashes. Fixing any one in isolation immediately
exposes the next. All three must be fixed together.

### Constraint decisions needed from you

**guidance_title:** Three options:

- **A (simplest):** Make it nullable in init-db and drop the NOT NULL in the
  ALTER. Code never writes it; MCP tools that query it get NULL and must
  handle it. Safest for now; guidance_title is dead weight until something
  uses it.

- **B (right long-term):** Source it from `manifest.json` (which has `"title"`
  per entry), add it to the `Chunk` dataclass, write it in `_UPSERT_SQL`.
  Makes citations and `fetch_guidance` responses readable. Adds ~10 lines of
  code. This is the correct design; the question is whether to do it in Phase 2
  or defer.

- **C (clean house):** Drop the column entirely from init-db and remove from
  schema. Easiest but loses the forward-compatibility slot.

**My recommendation:** Option B. The manifest already has `title`; the MCP
`fetch_guidance` tool presumably wants to return it; it takes 10 lines to wire.
Deferring creates a schema slot that stays empty and confuses future readers.
But this is your call — flag which option you want before I write Phase 2 code.

**section and metadata and created_at:** All in init-db, not in `_ensure_schema`,
not written by code. Since they have defaults or are nullable, they don't crash.
Options: (a) add them to `_ensure_schema` so the two schemas converge, or (b)
drop them from init-db. I'd drop `section` (not referenced anywhere in the
codebase) and keep `metadata` and `created_at` (plausible future use, no cost
to keep). But again, your call.

---

## B. Single-failure blast radius

After the download fix (per-URL try/except in `download_guidances`), the pipeline
fault-tolerates download failures. Everything downstream is still all-or-nothing
per run.

### `main()` — lines 347–362 — no per-document exception handling

```
paths = download_guidances(args.limit)   # line 341 — fault-tolerant now ✓

for path in paths:                       # line 347
    text = parse_pdf(path)               # line 349 — UNPROTECTED
    chunks = chunk_text(text, ...)       # line 359 — UNPROTECTED
    embedded = embed_chunks(chunks)      # line 360 — UNPROTECTED
    write_to_postgres(embedded)          # line 361 — UNPROTECTED
```

**parse_pdf (line 349):** pypdf can raise on encrypted PDFs, corrupted files,
or permission errors. One bad file crashes the remaining loop. Already-processed
docs are safe (already written). Work lost: the remaining N-1 documents.

**embed_chunks (line 360):** Calls `_embed_batch` which has 4-retry tenacity.
After all retries fail, reraises. `embed_chunks` itself raises `NotImplementedError`
on count mismatch (line 221). Both propagate uncaught. Work lost: remaining docs
AND the Voyage API cost for whatever batches ran before the failure.

**write_to_postgres (line 361):** If Postgres is down or a constraint violation
hits (schema drift — see §A), raises uncaught. Work lost: remaining docs. The
embeddings computed for THIS document are also lost (not written, not cached).

**The embedding-before-write gap:** Because embed_chunks runs before write_to_postgres
in the same unprotected block, a DB failure after a large embedding job wastes
the Voyage API cost. For a document with 300 chunks (2–3 Voyage batches), that's
real money if the failure keeps repeating.

**Fix needed (Phase 2):** Wrap the per-document block in try/except, same pattern
as the download fix. Log the failure, append to a list, continue.

### `write_to_postgres` — `_ensure_schema` inside the data transaction

`_ensure_schema` runs DDL on every call (one per document). In a healthy run
this is fine — `CREATE TABLE IF NOT EXISTS` is a cheap no-op after the first
call. But if init-db and `_ensure_schema` created different tables (which they
have — see §A), the first call runs DDL that changes nothing (because IF NOT
EXISTS is satisfied), and then the subsequent INSERT fails.

Better placement: call `_ensure_schema` once at the start of `main()`, before
the loop, not inside `write_to_postgres`. This separates "bootstrap" from "write"
and makes failures in each clearly distinct.

---

## C. Tenacity decorators

### `_download_one` (line 120)

```python
retry=retry_if_exception(_is_retryable)
```

`_is_retryable` predicate:
- 5xx HTTPStatusError → retry ✓
- 4xx HTTPStatusError → do NOT retry ✓ (fixed in this session)
- TimeoutException → retry ✓
- ConnectError → retry ✓
- ReadError → retry ✓

Missing: `httpx.RemoteProtocolError` (server closes connection mid-response).
This is rare but happens with some FDA servers that time out on slow clients.
Technically retryable. Minor omission; not a blocking issue.

**Verdict: correct for the known failure modes.**

### `_embed_batch` (line 230)

```python
retry=retry_if_exception_type(Exception)
```

Too broad. Retries on:
- `voyageai.error.RateLimitError` (429) — correct to retry ✓
- `voyageai.error.AuthenticationError` — **wrong**; retrying a bad API key
  wastes 4 × backoff time (up to 4 × 60s = 4 minutes) and still fails.
- `voyageai.error.InvalidRequestError` — **wrong**; bad input is permanent.
- `AttributeError`, `TypeError` from a bug in this file — **wrong**; masks
  programming errors during development.

The comment ("voyageai error types have no public stubs") is correct; the SDK
doesn't export public exception types in a way that mypy can resolve.

**Pragmatic fix:** Import voyageai exception types with a try/except (they do
exist at runtime), and use a predicate-style filter. If the import fails,
fall back to the current broad catch but log a warning. Alternatively, inspect
the exception's `status_code` attribute if it exists: `getattr(exc, 'status_code', None) not in (400, 401)`.

**Verdict: retries permanent errors; wastes time on auth failures and bad-input
errors. Should-fix.**

### `write_to_postgres` — no retry

No tenacity decorator. If Postgres briefly hiccups (restart, connection timeout),
the write fails permanently with no retry. For a once-a-week batch job this is
acceptable (just re-run), but since embeddings are computed BEFORE the write
(see §B), a transient DB failure wastes Voyage API calls.

**Verdict: acceptable for now; should-fix if embedding cost matters.**

---

## D. Other bug classes

### D1 — Duplicate HNSW index (silent waste)

If init-db ran first (normal workflow), it creates `chunks_embedding_hnsw_idx`.
When `_ensure_schema` then runs, it creates `chunks_hnsw` — a second HNSW index
on the same column. Both are `CREATE INDEX IF NOT EXISTS` by name, so both survive.

HNSW indexes are expensive in memory (proportional to table size). Two of them
double memory use. PostgreSQL will use whichever the planner picks; the other is
pure waste. Fix: reconcile the names in init-db and `_ensure_schema` and add an
explicit DROP of the stale one in the ALTER script.

### D2 — guidance_id B-tree index missing from `_ensure_schema`

init-db creates `chunks_guidance_id_idx` (B-tree on `guidance_id`). `_ensure_schema`
does not. If `_ensure_schema` created the table (init-db never ran), guidance_id
lookups from MCP `fetch_guidance` and `check_citation` tools do sequential scans.
At 50k chunks this is tolerable but wrong. Fix: add to `_ensure_schema`.

### D3 — Partial download file corruption

`_download_one` (line 139): if the process is killed mid-`write_bytes`, the
destination file exists but is truncated. On re-run, `if dest.exists(): return dest`
(line 131) returns the corrupt file to the parse stage, which either fails
(pypdf error) or silently produces garbage text.

Fix: write to `dest.with_suffix('.pdf.tmp')`, then `os.replace(tmp, dest)` —
`os.replace` is POSIX-atomic. Minor issue; rare in practice but can produce
silent data corruption.

### D4 — `embed_chunks` count mismatch raises `NotImplementedError`

Line 221:
```python
raise NotImplementedError(
    f"Voyage returned {len(embeddings)} embeddings for {len(batch)} inputs"
)
```

`NotImplementedError` conventionally means "abstract method not implemented."
This is a runtime API contract violation. Should be a `RuntimeError` or a typed
exception. Minor ergonomic issue; doesn't affect correctness.

### D5 — `_embed_batch` creates a new `voyageai.Client` per call

Line 239: `voyageai.Client(...)` is instantiated on every call to `_embed_batch`.
For a document with 200 chunks (2 batches), this creates 2 Client instances.
The client constructor likely reads the API key and initializes an HTTP session;
the overhead is ~10ms per call. For a batch job with 20 documents × 3 batches
each = 60 client instantiations, this is ~600ms of unnecessary overhead.

Not a correctness issue. The `_embed_batch` retry decorator means retried calls
also create new clients — which is actually fine (fresh client on retry is safer
than a potentially-dirty one). Noted for future cleanup.

### D6 — `_ensure_schema` called per-document (lock contention latent risk)

`_ensure_schema` runs inside `write_to_postgres`, which is called once per
document. `CREATE TABLE IF NOT EXISTS` acquires a brief `ShareLock`; in the
single-process batch job this is fine. But if the batch job ever runs concurrently
(two processes, parallelized docs), concurrent `CREATE TABLE` on the same table
causes lock contention.

Fix (Phase 2): move `_ensure_schema` to `main()` before the loop.

### D7 — `guidance_id` derived from URL path segment, not manifest `id`

`_download_one` derives the stem from the URL: `url.rstrip("/").split("/")[-2]`.
For `https://www.fda.gov/media/72010/download`, this gives `"72010"`.

The manifest has `"id": "72010"` matching this stem — consistent today. But if
a manifest entry has a non-standard URL (e.g., from a different FDA subdomain or
a redirect), the derived stem could differ from the manifest `id`, producing
mismatched `guidance_id` values in the DB vs. the manifest.

Fix: derive `guidance_id` from the manifest `"id"` field directly, not from the
URL. This requires passing the manifest entry (not just the URL) through to
the ingest loop. Touches the Chunk dataclass and the manifest loading.
Low priority now; medium priority if any non-standard URLs enter the manifest.

---

## E. Prioritized fix list

### Must-fix (will crash ingest again)

| # | Issue | Fix |
|---|---|---|
| 1 | `guidance_title NOT NULL` in init-db; not written | Make nullable (option A) OR add to Chunk + UPSERT (option B — my rec) |
| 2 | `token_count` missing from init-db schema | Add to init-db; ALTER running DB |
| 3 | UNIQUE constraint missing from init-db schema | Add to init-db; ALTER running DB |
| 4 | Per-document fault tolerance in `main()` | try/except around parse→chunk→embed→write |

### Should-fix (latent bugs not yet triggered)

| # | Issue | Fix |
|---|---|---|
| 5 | Duplicate HNSW index (double memory) | Unify index name; DROP stale one in ALTER |
| 6 | Missing `guidance_id` B-tree in `_ensure_schema` | Add to `_ensure_schema` |
| 7 | `_embed_batch` retries permanent errors | Narrow predicate; see §C |
| 8 | `_ensure_schema` called per-document | Move to start of `main()` |
| 9 | `embedding` nullable in init-db | Add NOT NULL; ALTER running DB |

### Nice-to-have

| # | Issue | Fix |
|---|---|---|
| 10 | Partial download file corruption (D3) | Atomic write via tmp file |
| 11 | `NotImplementedError` for count mismatch | Use `RuntimeError` |
| 12 | `voyageai.Client` per call | Module-level singleton |
| 13 | `guidance_id` from manifest `id` not URL (D7) | Thread manifest entry through ingest |

---

## Decisions needed before Phase 2 starts

1. **guidance_title:** Option A (nullable), B (add to Chunk + write from manifest),
   or C (drop)?

2. **section, metadata, created_at:** Keep in init-db + add to `_ensure_schema`,
   or drop from init-db?

3. **Running DB state:** Do you know if `corpus.chunks` currently has any rows,
   or is it empty from all the failed inserts? This determines whether the
   backfill step in the ALTER script matters.

4. **`_embed_batch` retry scope:** Accept the broad-catch-with-comment as-is
   (pragmatic), or narrow it (requires inspecting voyageai runtime exceptions)?

---

## ALTER statements for the running DB (Phase 2 will apply these)

These assume option A for guidance_title (make nullable). If you choose option B,
add `ALTER TABLE corpus.chunks ALTER COLUMN guidance_title DROP NOT NULL` instead
of the drop, and handle the data load separately.

```sql
-- 1. Add missing token_count column (backfill existing rows to 0)
ALTER TABLE corpus.chunks ADD COLUMN IF NOT EXISTS token_count INT;
UPDATE corpus.chunks SET token_count = 0 WHERE token_count IS NULL;
ALTER TABLE corpus.chunks ALTER COLUMN token_count SET NOT NULL;

-- 2. Add UNIQUE constraint required by ON CONFLICT clause
ALTER TABLE corpus.chunks
    ADD CONSTRAINT chunks_guidance_chunk_unique
    UNIQUE (guidance_id, chunk_index);

-- 3. Make guidance_title nullable (stops NOT NULL violation)
--    If option B (write from manifest): skip this, add to Chunk + upsert instead.
ALTER TABLE corpus.chunks ALTER COLUMN guidance_title DROP NOT NULL;

-- 4. Make embedding NOT NULL (init-db has it nullable; code always writes it)
ALTER TABLE corpus.chunks ALTER COLUMN embedding SET NOT NULL;

-- 5. Drop the duplicate HNSW index left by init-db
--    (chunks_hnsw will be the canonical name going forward)
DROP INDEX IF EXISTS corpus.chunks_embedding_hnsw_idx;
```

Phase 2 will also update `init-db/01-init.sql` so these never diverge again,
and align `_ensure_schema` to match init-db exactly.
