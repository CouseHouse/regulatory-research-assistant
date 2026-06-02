"""Load CI fixture data into a Postgres instance for the citation-gate workflow.

Run AFTER applying init-db/01-init.sql (schema must already exist).

What this inserts:
  - corpus.chunks rows for guidance_id="184856", chunk_index=1 and chunk_index=2
    These are the valid keys cited by ci-valid-001 in ci_key_fixture.jsonl.
    check_citation must return verified=True for both so the gate can distinguish
    "fixture infrastructure broken" from "gate correctly failing on bogus key".

What is NOT inserted:
  - chunk_index=99999 — the deliberately invalid key in ci-bogus-001.
    check_citation returns verified=False → citation_validity=0.0 → gate FAILS.

The dummy embedding (1024 zeros) satisfies the NOT NULL constraint; the CI eval
path uses check_citation key-existence mode which queries only guidance_id and
chunk_index, never the embedding.
"""
from __future__ import annotations

import os

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

DATABASE_URL = os.environ["DATABASE_URL"]

_VALID_CHUNKS = [
    {
        "guidance_id": "184856",
        "guidance_title": "AI-Enabled Device Software Functions: Lifecycle Management [CI fixture]",
        "section": "CI",
        "chunk_index": 1,
        "text": "CI fixture chunk 1 for guidance 184856. Not real content.",
        "char_start": 0,
        "char_end": 52,
        "token_count": 10,
    },
    {
        "guidance_id": "184856",
        "guidance_title": "AI-Enabled Device Software Functions: Lifecycle Management [CI fixture]",
        "section": "CI",
        "chunk_index": 2,
        "text": "CI fixture chunk 2 for guidance 184856. Not real content.",
        "char_start": 53,
        "char_end": 106,
        "token_count": 10,
    },
]

_INSERT_SQL = """
    INSERT INTO corpus.chunks
        (guidance_id, guidance_title, section, chunk_index, text,
         char_start, char_end, token_count, embedding)
    VALUES
        (%(guidance_id)s, %(guidance_title)s, %(section)s, %(chunk_index)s, %(text)s,
         %(char_start)s, %(char_end)s, %(token_count)s, %(embedding)s)
    ON CONFLICT (guidance_id, chunk_index) DO NOTHING
"""

_dummy_embedding = np.zeros(1024, dtype=np.float32)


def main() -> None:
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            for chunk in _VALID_CHUNKS:
                cur.execute(_INSERT_SQL, {**chunk, "embedding": _dummy_embedding})
    print(
        f"Loaded {len(_VALID_CHUNKS)} CI fixture chunks "
        f"(guidance_id=184856, chunk_index=1 and 2)."
    )


if __name__ == "__main__":
    main()
