# Design proposal: `src/rra/ingest.py`

Status: **proposed** — review before implementation.

Scope: the offline corpus-build pipeline. Download FDA guidance PDFs → parse →
chunk → embed (Voyage) → write to Postgres/pgvector. Invoked via the
`rra-ingest` console script (`rra.ingest:main`). This is a one-shot batch job,
not part of the request path; correctness and reproducibility matter more than
latency.

Cross-references: `docs/spec.md` §4.3 (vector store), §4.4 (chunking),
§4.5 (embeddings). Schema is constrained by the eval layer — see
`src/rra/evals/run.py:80` and `src/rra/evals/scorers.py:42`.

---

## 1. Function signatures and execution order

```python
def main() -> int: ...

def download_guidances(limit: int) -> list[Path]: ...
def _download_one(client: httpx.Client, url: str, dest: Path) -> Path | None: ...

def parse_pdf(path: Path) -> str: ...

def chunk_text(text: str, guidance_id: str) -> list[Chunk]: ...

def embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]: ...
def _embed_batch(texts: list[str]) -> list[list[float]]: ...   # tenacity-wrapped

def write_to_postgres(chunks: list[EmbeddedChunk]) -> None: ...
def _ensure_schema(conn: psycopg.Connection) -> None: ...
```

### Call chain (`main()` orchestration)

```
main(limit from CLI arg / default)
│
├─ paths = download_guidances(limit)          # network → ./data/corpus/*.pdf
│     └─ for each url: _download_one(...)      # tenacity retry per file
│
└─ for path in paths:
      ├─ text = parse_pdf(path)
      │     └─ if len(text) < 500: log + `continue`  (scanned-PDF skip)
      │
      ├─ chunks = chunk_text(text, guidance_id)        # guidance_id = path.stem
      ├─ embedded = embed_chunks(chunks)               # batches of ≤128
      └─ write_to_postgres(embedded)                   # one txn per document
```

Per-document loop (not "parse-all-then-embed-all") so a single corrupt PDF
fails in isolation and a partial run still commits completed documents. Each
document is one Postgres transaction — a document is either fully present or
absent, never half-ingested.

`main()` returns an `int` exit code (0 = at least one document ingested,
non-zero = nothing ingested / fatal config error) so `rra-ingest` is usable in
CI and shell pipelines.

---

## 2. Data carrier types

Two frozen `dataclass`es, defined in this module. Rationale for dataclass over
`TypedDict`: these flow between functions as positional carriers, benefit from
`frozen=True` immutability, and a `Chunk` → `EmbeddedChunk` widening is clearer
as two distinct types than one dict that "gains a key" partway through. Strict
mypy is happy with both; dataclasses give us `__init__` typing for free.

```python
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Chunk:
    guidance_id: str    # stable doc id; = source PDF filename stem (e.g. "GUID-001")
    chunk_index: int    # 0-based ordinal within the document; ORDER BY this reconstructs text
    text: str           # the chunk body
    char_start: int     # offset into the parsed full text (inclusive)
    char_end: int       # offset into the parsed full text (exclusive)
    token_count: int    # tiktoken count, for cost reporting + sanity assertions

@dataclass(frozen=True, slots=True)
class EmbeddedChunk:
    chunk: Chunk
    embedding: list[float]   # length == settings.embedding_dim (1024)
```

### Why these fields

- `guidance_id` + `chunk_index` is the natural key the eval layer already
  assumes: `SELECT text FROM corpus.chunks WHERE guidance_id=%s ORDER BY
  chunk_index` (`evals/run.py:80`). Honoring it now means the day-2 corpus
  lookup is a drop-in.
- `char_start` / `char_end` exist because the citation contract carries
  `char_start`/`char_end` (`evals/scorers.py:42`). Recording offsets at ingest
  time lets the `check_citation` MCP tool resolve a cited span back to an exact
  source location without re-deriving offsets later.
- `EmbeddedChunk` wraps rather than copies `Chunk` so the embedding stays
  paired with its source and we never re-thread chunk metadata by hand.

---

## 3. Postgres schema (created by `_ensure_schema`)

Idempotent DDL run once at the top of `write_to_postgres`. Lives in the
`corpus` schema to match the eval layer's `corpus.chunks` reference.

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS corpus;

CREATE TABLE IF NOT EXISTS corpus.chunks (
    id           bigserial PRIMARY KEY,
    guidance_id  text        NOT NULL,
    chunk_index  int         NOT NULL,
    text         text        NOT NULL,
    char_start   int         NOT NULL,
    char_end     int         NOT NULL,
    token_count  int         NOT NULL,
    embedding    vector(1024) NOT NULL,    -- dim asserted against settings at runtime
    UNIQUE (guidance_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_hnsw
    ON corpus.chunks USING hnsw (embedding vector_cosine_ops);
```

- `vector(1024)` is written from `settings.embedding_dim` via a parameterized
  DDL build; we assert `len(embedding) == settings.embedding_dim` before insert
  so a model/dim mismatch fails loudly at ingest, not silently at query time.
- HNSW + `vector_cosine_ops` per spec §4.3 (HNSW) and §4.5 (cosine).
- `UNIQUE (guidance_id, chunk_index)` makes re-ingest of a single document safe
  via `ON CONFLICT ... DO UPDATE` (upsert), so re-running after a chunking
  tweak replaces rather than duplicates.

---

## 4. Key design decisions

### D1 — Downloader: hardcoded URL list vs. scraping the FDA database

- **Question:** Where does `download_guidances` get its list of PDFs?
- **Recommendation:** Hardcode a module-level list of 10–20 known FDA guidance
  PDF URLs; `download_guidances(limit)` fetches up to `limit` of them into a
  local cache dir (`PROJECT_ROOT/data/corpus`), skipping any already present.
- **Rationale:** A fixed, reproducible corpus makes evals deterministic and
  removes a flaky scraping dependency from a portfolio v1.
- **Rejected:** Scraping `fda.gov/.../search-fda-guidance-documents` or
  paginating the FDA guidance API — more moving parts, brittle HTML, and a
  shifting corpus that would make recall numbers non-reproducible. **This is an
  explicit v1 shortcut** (see Open Questions / spec note below). Production
  would paginate the FDA guidance database API, persist a manifest of
  doc id → URL → checksum, and detect new/updated guidances incrementally.

### D2 — Retry strategy: `tenacity` on the smallest failing unit

- **Question:** How do we make network + embedding calls resilient without a
  hand-rolled loop?
- **Recommendation:** Decorate `_download_one` and `_embed_batch` with
  `@tenacity.retry` using `wait_exponential` + `stop_after_attempt(n)` +
  `retry_if_exception_type(...)` (httpx transport/5xx errors; Voyage rate-limit
  errors). Per-file and per-batch granularity so one failure doesn't replay the
  whole job.
- **Rationale:** A hard constraint, and tenacity gives jittered exponential
  backoff declaratively with correct typing.
- **Rejected:** Custom `for attempt in range(...)` loops — explicitly
  disallowed by constraint 1 and harder to get backoff/jitter right.

### D3 — Voyage batching: chunk the chunk-list into ≤128-item batches

- **Question:** How do we respect Voyage's 128-items-per-request cap?
- **Recommendation:** `embed_chunks` slices `chunks` into windows of
  `VOYAGE_MAX_BATCH = 128` (module constant), calls the tenacity-wrapped
  `_embed_batch` per window, then zips the flat embedding list back onto the
  ordered chunks to build `EmbeddedChunk`s. `input_type="document"` is passed
  to Voyage (ingest-side documents, not queries).
- **Rationale:** Honors the API limit while keeping order-preservation explicit
  and the batch size in one named place.
- **Rejected:** One-call-per-chunk (slow, rate-limit-prone) and assuming Voyage
  preserves order without asserting `len(out) == len(batch)`.

### D4 — Chunking: `RecursiveCharacterTextSplitter` driven by a tiktoken length fn

- **Question:** How do we hit 512-token / 50-token-overlap chunks with a
  *character* splitter while staying spec-compliant (§4.4)?
- **Recommendation:** Use `langchain_core` `RecursiveCharacterTextSplitter`
  constructed via `.from_tiktoken_encoder(...)` (or `length_function=` a
  tiktoken-based counter) with `chunk_size=settings.chunk_size_tokens`,
  `chunk_overlap=settings.chunk_overlap_tokens`. After splitting, compute each
  chunk's `char_start`/`char_end` by locating chunks sequentially in the source
  text (advance a cursor with `str.find` from the last end to handle repeated
  substrings).
- **Rationale:** Matches spec §4.4 exactly (recursive splitter, token budget
  from config) while still recording exact char offsets for citation checking.
- **Rejected:** A pure tiktoken windowing splitter (loses the structural-
  boundary preference §4.4 calls out); naive `text.index()` without a cursor
  (mis-locates repeated boilerplate like headers/footers).

### D5 — Data carrier: frozen dataclass vs. `TypedDict`

- **Question:** What type flows between the pipeline stages?
- **Recommendation:** Two frozen `slots=True` dataclasses (`Chunk`,
  `EmbeddedChunk`) — see §2.
- **Rationale:** Immutability + an explicit pre→post-embedding type widening is
  clearer and safer under strict mypy than a single growing dict.
- **Rejected:** `TypedDict` (no immutability, "key appears later" is implicit);
  `NamedTuple` (fine, but dataclass composition `EmbeddedChunk.chunk` reads
  better than tuple nesting).

### D6 — Write path: psycopg `executemany` upsert, one transaction per document

- **Question:** How do rows land in Postgres safely and re-runnably?
- **Recommendation:** Open one `psycopg.Connection` per `write_to_postgres`
  call, `_ensure_schema` once, then `executemany` an
  `INSERT ... ON CONFLICT (guidance_id, chunk_index) DO UPDATE` over the
  document's chunks inside a single transaction. Register pgvector's psycopg
  adapter so `list[float]` binds to `vector`.
- **Rationale:** One txn per document gives all-or-nothing per-doc semantics and
  makes re-ingest idempotent; `executemany` keeps it a single round-trip batch.
- **Rejected:** Row-at-a-time autocommit inserts (no atomicity, slow); a single
  giant transaction across the whole corpus (one bad doc rolls back everything).
  SQLAlchemy ORM is available but adds mapping ceremony for what is a flat bulk
  insert — psycopg directly is simpler here; flag for review if the team
  prefers ORM consistency.

---

## 5. Conflicts with spec.md

**No conflicts.** The implementation requirements are spec-consistent:

- §4.3 (pgvector in same Postgres, HNSW) → satisfied by §3 schema.
- §4.4 (recursive splitter, 512/50 from config) → satisfied by D4, sourced from
  `settings.chunk_size_tokens` / `settings.chunk_overlap_tokens`.
- §4.5 (Voyage 3, 1024-d, cosine) → `settings.embedding_model` /
  `settings.embedding_dim`, `vector_cosine_ops`, `input_type="document"`.

One thing the spec does **not** prescribe and this design adds: the hardcoded
downloader corpus (D1). It does not contradict the spec — the spec is silent on
corpus sourcing — but it is a deliberate v1 simplification worth a reviewer's
sign-off and a `docs/future-work.md` entry.

---

## 6. Open questions for the human

1. **Corpus source (D1):** OK to ship the hardcoded URL list for v1, with a
   `future-work.md` entry for the FDA API paginator? Need ~10–20 real guidance
   PDF URLs — do you have a preferred set, or should I pick a representative
   spread (e.g., a few CDER, CBER, CDRH guidances)?
2. **`guidance_id` source:** Using `path.stem` as the id. Acceptable, or do we
   want a human-readable title / FDA docket number captured at download time
   into a manifest and used as the id? (Affects citation readability.)
3. **psycopg vs. SQLAlchemy (D6):** This proposal uses psycopg directly for the
   bulk write. The stack lists SQLAlchemy 2.0 — do you want ingest to go through
   the ORM/Core for consistency with the rest of the app, accepting the extra
   boilerplate?
4. **Connection acquisition:** Should ingest open its own short-lived
   connection from `settings.pg_dsn`, or is there a shared pool/`get_conn()`
   helper planned that it should use instead? (None exists yet in `src/rra`.)
5. **Re-ingest semantics:** Upsert keyed on `(guidance_id, chunk_index)`
   updates in place. If a re-chunk produces *fewer* chunks than before, stale
   high-index rows linger. Acceptable, or should `write_to_postgres` first
   `DELETE FROM corpus.chunks WHERE guidance_id=%s` within the txn?
