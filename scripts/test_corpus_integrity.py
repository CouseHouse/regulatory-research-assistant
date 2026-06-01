"""Stage 1 — Corpus integrity check.

Mechanical verification that the corpus ingested correctly. No retrieval,
no LLM — just SQL and arithmetic against what's in Postgres vs what the
manifest says should be there.

Checks:
  1. Doc count: chunks reference the expected number of distinct guidances
  2. Manifest alignment: every approved manifest doc has chunks; no orphans
  3. Chunk sanity: no empty chunks, no absurd lengths, token counts present
  4. Embedding integrity: every chunk has a 1024-dim embedding, no nulls
  5. Cluster metadata: every chunk has a cluster; distribution matches manifest
  6. Idempotency markers: no duplicate (guidance_id, chunk_index) pairs

Usage:
    python scripts/test_corpus_integrity.py
    python scripts/test_corpus_integrity.py --manifest data/corpus/manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import psycopg

# Reuse the app's DSN resolution rather than hardcoding
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rra.config import settings  # noqa: E402

MANIFEST_PATH = Path("data/corpus/manifest.json")


def check(label: str, passed: bool, detail: str = "") -> bool:
    marker = "✓" if passed else "✗"
    line = f"  {marker} {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    manifest_ids = {str(e["id"]) for e in manifest}
    manifest_clusters = Counter(e.get("cluster", "none") for e in manifest)

    print(f"Manifest: {len(manifest)} approved docs at {args.manifest}\n")

    dsn = settings.database_url
    all_passed = True

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # ── Check 1: doc count ──
        cur.execute("SELECT count(DISTINCT guidance_id) FROM corpus.chunks;")
        db_doc_count = cur.fetchone()[0]
        all_passed &= check(
            "Doc count matches manifest",
            db_doc_count == len(manifest),
            f"DB has {db_doc_count}, manifest has {len(manifest)}",
        )

        # ── Check 2: manifest alignment ──
        cur.execute("SELECT DISTINCT guidance_id FROM corpus.chunks;")
        db_ids = {row[0] for row in cur.fetchall()}
        missing = manifest_ids - db_ids
        orphans = db_ids - manifest_ids
        all_passed &= check(
            "Every manifest doc has chunks",
            not missing,
            f"missing: {sorted(missing)[:10]}" if missing else "all present",
        )
        all_passed &= check(
            "No orphan docs in DB",
            not orphans,
            f"orphans: {sorted(orphans)[:10]}" if orphans else "none",
        )

        # ── Check 3: chunk sanity ──
        cur.execute("SELECT count(*) FROM corpus.chunks WHERE text IS NULL OR length(trim(text)) = 0;")
        empty_chunks = cur.fetchone()[0]
        all_passed &= check("No empty chunks", empty_chunks == 0, f"{empty_chunks} empty")

        cur.execute("SELECT count(*) FROM corpus.chunks WHERE length(text) > 20000;")
        huge_chunks = cur.fetchone()[0]
        all_passed &= check(
            "No absurdly large chunks (>20k chars)",
            huge_chunks == 0,
            f"{huge_chunks} oversized",
        )

        cur.execute("SELECT count(*) FROM corpus.chunks WHERE token_count IS NULL;")
        null_tokens = cur.fetchone()[0]
        all_passed &= check("All chunks have token_count", null_tokens == 0, f"{null_tokens} null")

        cur.execute("SELECT avg(length(text))::int, avg(token_count)::int FROM corpus.chunks;")
        avg_chars, avg_tokens = cur.fetchone()
        print(f"      avg chunk: {avg_chars} chars, {avg_tokens} tokens")

        # ── Check 4: embedding integrity ──
        cur.execute("SELECT count(*) FROM corpus.chunks WHERE embedding IS NULL;")
        null_emb = cur.fetchone()[0]
        all_passed &= check("All chunks have embeddings", null_emb == 0, f"{null_emb} null")

        # vector dim check — pgvector stores dim in the type, but verify one row
        cur.execute("SELECT vector_dims(embedding) FROM corpus.chunks LIMIT 1;")
        row = cur.fetchone()
        if row:
            dim = row[0]
            all_passed &= check("Embedding dim is 1024", dim == 1024, f"got {dim}")

        # ── Check 5: cluster metadata ──
        cur.execute("SELECT count(*) FROM corpus.chunks WHERE metadata->>'cluster' IS NULL;")
        null_cluster = cur.fetchone()[0]
        all_passed &= check("All chunks have a cluster", null_cluster == 0, f"{null_cluster} null")

        cur.execute("""
            SELECT metadata->>'cluster', count(DISTINCT guidance_id)
            FROM corpus.chunks GROUP BY 1 ORDER BY 2 DESC;
        """)
        print("\n      Cluster distribution (docs):")
        db_cluster_docs = {}
        for cluster, ndocs in cur.fetchall():
            db_cluster_docs[cluster] = ndocs
            manifest_n = manifest_clusters.get(cluster, 0)
            flag = "" if ndocs == manifest_n else f"  ⚠ manifest says {manifest_n}"
            print(f"        {ndocs:3d}  {cluster}{flag}")

        # ── Check 6: idempotency ──
        cur.execute("""
            SELECT count(*) FROM (
                SELECT guidance_id, chunk_index, count(*) AS n
                FROM corpus.chunks GROUP BY 1, 2 HAVING count(*) > 1
            ) dups;
        """)
        dup_pairs = cur.fetchone()[0]
        all_passed &= check(
            "No duplicate (guidance_id, chunk_index)",
            dup_pairs == 0,
            f"{dup_pairs} duplicated pairs",
        )

        # ── Summary stats ──
        cur.execute("SELECT count(*) FROM corpus.chunks;")
        total_chunks = cur.fetchone()[0]
        print(f"\n  Total chunks: {total_chunks}")
        print(f"  Chunks per doc: {total_chunks / max(db_doc_count, 1):.1f} avg")

    print("\n" + ("─" * 40))
    if all_passed:
        print("  STAGE 1 PASSED — corpus integrity verified")
        print("  Proceed to Stage 2 (retrieval quality)")
        return 0
    else:
        print("  STAGE 1 FAILED — fix integrity issues before retrieval testing")
        return 1


if __name__ == "__main__":
    sys.exit(main())
