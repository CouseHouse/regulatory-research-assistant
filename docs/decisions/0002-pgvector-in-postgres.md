# 0002 — pgvector in Postgres for vector storage

**Status:** Active
**Date:** 2025-05-28
**Owner:** Butters

## Context

The system needs a vector store for ~50k embedded chunks of FDA guidance. It also needs application state storage (LangGraph checkpoints, audit log, ingest manifest). Adding a separate SaaS for vectors is operationally cheap but adds auth boundaries, monitoring surface, and cost.

## Decision

We use **pgvector inside the same Postgres instance** that holds application state. HNSW indexing with cosine distance.

## Alternatives considered

- **Pinecone** — Fast to set up, but adds SaaS dependency, separate auth boundary, ongoing cost at a scale that doesn't need it.
- **OpenSearch** — Right answer at much higher scale or when hybrid (BM25 + vector) retrieval is required from day one. At 50k chunks, premature.
- **Azure AI Search** — Natural pick if deploying to Foundry, but portability matters more than cloud-specific stores.
- **Qdrant / Weaviate / Chroma** — Each is a separate service to operate; pgvector wins on operational simplicity.

## Consequences

**Enables:**
- Single backup story
- Single auth boundary
- LangGraph state and corpus chunks queryable in the same SQL transaction
- Trivial to demo and self-host

**Constrains:**
- Pure-vector retrieval only in v1 (hybrid BM25+vector is future-work item 2)
- Postgres has to scale; if it's the bottleneck, we can't shard vectors independently

**Reopen if:**
- Corpus grows past ~1M chunks (re-evaluate at that scale)
- Recall@10 stalls below 0.75 after chunking tuning AND hybrid retrieval is implemented
- Customer cloud is Azure and they prefer AI Search for operational reasons

## Related

- spec.md §4.3
- future-work.md item 2 (hybrid retrieval)
