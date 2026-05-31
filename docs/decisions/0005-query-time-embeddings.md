# 0005 — Query-time embeddings: singleton Voyage client + `input_type="query"`

**Status:** Active
**Date:** 2026-05-31
**Owner:** Butters

## Context

The Day 3 query path must embed the user's query at request time. Two forces are
in play, both surfaced by Day 2. First, ingest's `_embed_batch` builds a new
`voyageai.Client` per call (~50ms setup) — acceptable for a batch job, wasteful
on a per-request hot path (Day 2 dev-log open question). Second, **Voyage 3 is an
asymmetric embedding model**: it is trained to embed queries and documents
differently via `input_type`, and the corpus was ingested with
`input_type="document"` (`ingest.py`). A query embedded with the wrong (or no)
`input_type` lands in a subtly different region of the space and silently
degrades recall — there is no error, just worse neighbors.

## Decision

The query path uses a **process-lifetime singleton `voyageai.Client`** and embeds
queries with **`input_type="query"`** (documents remain `input_type="document"`
at ingest).

## Alternatives considered

- **New `voyageai.Client` per query (ingest status quo)** — Rejected. ~50ms
  client-construction overhead on every request; fine for the weekly batch, not
  for per-request latency.
- **Symmetric embedding — same `input_type` (or `None`) on both sides** —
  Rejected, and this is the trap to guard against. Voyage 3 is trained
  asymmetrically; collapsing query and document to one `input_type` "for
  simplicity" produces *no error and lower recall*. This looks like a harmless
  cleanup and is not one. Locked here precisely so a future refactor doesn't
  "simplify" it back.
- **Embed queries with `input_type="document"` to "match ingest"** — Rejected.
  The specific instance of the symmetric trap that feels most justified
  ("be consistent with the corpus"). Consistency of the *string* is the wrong
  goal; consistency of the *trained query↔document pairing* is the right one,
  and that pairing requires the two sides to differ.
- **Cache query embeddings** — Rejected for v1. Query text is diverse; hit rate
  is low. Response/embedding caching is out of scope (spec §8) and future-work.
- **Local / self-hosted query embedder** — Rejected. The corpus was embedded
  with `voyage-3`; queries MUST be embedded by the same model to share the
  space. A different embedder is a non-starter without re-embedding the corpus.

## Consequences

**Enables:**
- Low-latency query embedding on a warmed client.
- Correct asymmetric retrieval against the `input_type="document"` corpus.
- Resolves the Day 2 deferral; the singleton helper is shared by `retrieval.py`
  and any future query-time embedder.

**Constrains:**
- The client is constructed once; api key / model are read at startup. Changing
  `embedding_model` already requires a corpus re-ingest (noted in `config.py`)
  and now also a process restart.
- The query/document `input_type` split must be honored at **every** embedding
  call site. A third producer (e.g., a future embedding-based rerank or a
  dedup pass) must consciously pick the correct `input_type`.

**Reopen if:**
- Voyage ships a successor model where `input_type` is a documented no-op (then
  dropping the split is genuinely safe — verify against that model's docs first).
- `embedding_model` is changed to a model with different asymmetry semantics —
  re-verify the correct `input_type` values *before* re-ingest, do not assume
  `"query"`/`"document"` carry over.
- Latency profiling shows query embedding is a negligible fraction of request
  time — then client-per-call simplicity could return (low priority; the
  asymmetry decision is independent and stays regardless).

## Related

- spec.md §4.5 (Voyage 3 embeddings)
- ADR 0003 (Anthropic SDK direct) — note: embeddings are explicitly a Voyage
  concern per §4.5; 0003's "Anthropic-only model layer" governs *generation*,
  not embeddings, so Voyage is not a violation of it
- ADR 0004 (data-access layer) — the other half of the Day 3 query path
- `ingest.py` `_embed_batch` (the per-call client + `input_type="document"`)
- Day 2 dev-log open question: "singleton client for the query path"
