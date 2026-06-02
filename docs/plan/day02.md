# Day 2 — Ingestion

## Goal

Populate `corpus.chunks` with ~40-50k embedded chunks from FDA guidance PDFs. The system can now answer questions about real data; nothing intelligent yet, but the foundation is real.

## Deliverables

- `src/rra/ingest.py` (~200 lines):
  - `download_guidances(limit: int | None) -> list[Path]`
  - `parse_pdf(path: Path) -> ParsedGuidance | None` (None for scanned)
  - `chunk_text(text: str, guidance_id: str) -> list[Chunk]`
  - `embed_chunks(chunks: list[Chunk]) -> list[EmbeddedChunk]`
  - `write_to_postgres(chunks: list[EmbeddedChunk]) -> int`
  - `main()` entry point: `uv run python -m rra.ingest [--limit N]`
- `tests/test_ingest.py`: one test per function minimum, fixtures for PDFs, mocked Voyage API
- `docs/ingest-design.md`: design proposal from the planning phase (delete after merge if not portfolio-worthy)

## Design constraints (from spec)

- Recursive character splitter, 512 tokens, 50 overlap (spec §4.4)
- Voyage 3 embeddings, 1024 dimensions, cosine similarity (spec §4.5)
- Voyage batch endpoint, max 128 per batch
- Use `tenacity` for retries, NOT a custom loop
- Drop HNSW index before bulk insert, rebuild after (faster)
- All config via `rra.config.settings`; no `os.getenv` anywhere

## Decisions to make in the planning phase

1. PDF parser: `pypdf` (in deps) vs `pdfplumber` vs `unstructured`? Default to pypdf unless tests show it mangles FDA documents.
2. Scanned PDF detection: text length < 500 chars after parsing → skip. Worth more sophisticated detection? Probably not for v1.
3. Chunk metadata: what JSONB fields go in `metadata`? At minimum: source URL, publication date, document type if available.
4. Transaction scope for bulk insert: one big transaction, or batched? Trade-off is rollback granularity vs. memory.
5. Re-runnability: idempotent on guidance_id, or fresh-only? Idempotent is more work but safer.

## Stop conditions

- `uv run python -m rra.ingest --limit 10` populates 10 docs end-to-end with no errors
- Full run: `uv run python -m rra.ingest` ingests target ~200 guidances
- `SELECT count(*) FROM corpus.chunks;` returns > 40000
- `uv run pytest tests/test_ingest.py` passes
- `uv run mypy src/rra/ingest.py` clean
- Scanned-PDF skip list logged

## Known gotchas

- FDA guidance URLs occasionally redirect; configure tenacity to retry on 5xx but not 4xx
- Voyage batch limit is 128 — chunks list needs `itertools.batched` or equivalent
- Some FDA PDFs are 100MB+ scanned image PDFs; the text-length check catches these without OCR
- Postgres `executemany` is slow; use `psycopg.copy` with binary or rows for bulk insert
- HNSW index build time on 50k vectors is a few minutes — log progress

## Definition of done

End-of-day dev-log entry shows: chunk count, parse success rate, scan-skip count, average ingestion time per doc, total Voyage cost spent.
