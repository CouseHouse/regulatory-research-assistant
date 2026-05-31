-- Runs once, on the very first `docker compose up` when the postgres-data
-- volume is empty. To re-run: `docker compose down -v` then `up` again.
--
-- This file sets up the bare minimum schema. Application migrations
-- (LangGraph checkpointer tables, etc.) are created at runtime by the
-- libraries themselves.

-- pgvector extension — gives us the `vector` column type and indexes
CREATE EXTENSION IF NOT EXISTS vector;

-- Schemas keep things organized
CREATE SCHEMA IF NOT EXISTS corpus;     -- chunks + embeddings
CREATE SCHEMA IF NOT EXISTS app;        -- application state (sessions, audit)
CREATE SCHEMA IF NOT EXISTS langgraph;  -- LangGraph checkpointer writes here

-- Corpus table — populated by `python -m rra.ingest`
-- Embedding dimension 1024 matches Voyage 3. Change if you switch models.
-- IMPORTANT: _ensure_schema() in src/rra/ingest.py must mirror this DDL.
-- Update both together or a schema/code drift will crash ingest.
CREATE TABLE IF NOT EXISTS corpus.chunks (
    id              BIGSERIAL PRIMARY KEY,
    guidance_id     TEXT NOT NULL,
    guidance_title  TEXT NOT NULL,
    section         TEXT,
    chunk_index     INT NOT NULL,
    text            TEXT NOT NULL,
    char_start      INT NOT NULL,
    char_end        INT NOT NULL,
    token_count     INT NOT NULL,
    embedding       vector(1024) NOT NULL,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (guidance_id, chunk_index)
);

-- Lookups by guidance_id are frequent (fetch_guidance tool, check_citation tool)
CREATE INDEX IF NOT EXISTS chunks_guidance_id_idx
    ON corpus.chunks (guidance_id);

-- HNSW index for vector search. Build it AFTER ingesting the corpus,
-- not before — building on an empty table is fine but rebuilding after
-- bulk insert is faster. The ingest script will (re)create this.
-- Cosine distance matches Voyage's recommended similarity metric.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON corpus.chunks
    USING hnsw (embedding vector_cosine_ops);

-- Audit log: every query and its final response, for offline analysis
CREATE TABLE IF NOT EXISTS app.query_audit (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    query           TEXT NOT NULL,
    product_context TEXT,
    response        JSONB,
    langfuse_trace_id TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    token_count     INT
);

CREATE INDEX IF NOT EXISTS query_audit_session_idx
    ON app.query_audit (session_id);
