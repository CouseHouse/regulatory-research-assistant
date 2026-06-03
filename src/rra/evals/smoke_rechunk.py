"""$0 text-only faithfulness smoke — measures the same baseline quotes against
any corpus table, so dirty vs clean is a real apples-to-apples delta.

No Voyage API calls. check_citation reads text only (ADR 0010 §0.6 / ADR 0014).

Workflow:
    # Lock quotes once (analyst API — the only paid step):
    uv run python -m rra.evals.smoke_rechunk --save-baseline

    # Build clean scratch table ($0):
    uv run rra-ingest --rechunk-scratch

    # Measure dirty corpus — the real before number on these exact quotes ($0):
    uv run python -m rra.evals.smoke_rechunk --table chunks

    # Measure clean scratch table — the after number ($0):
    uv run python -m rra.evals.smoke_rechunk --table chunks_rechunk

    Delta (after − before) is the honest measure of Levers A+B, same quote set,
    both arms.

Report interpretation (plan §3 decision table):
    best-chunk ≫ before and ≈ doc-level  → Levers A+B solved straddle → re-embed
    best-chunk low, doc-level high        → cleaning works; straddle persists → tune B or Lever D
    doc-level low                         → cleaning insufficient → tune A regexes
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from rra.config import settings
from rra.mcp_server.tools import match_quote

BASELINE_PATH = (
    Path(__file__).resolve().parents[3] / "evals" / "baseline-quotes.json"
)
_VALID_TABLES = {"chunks", "chunks_rechunk"}


# ─── Step 0: lock baseline quotes ────────────────────────────────────────────

def save_baseline(limit: int | None = None) -> None:
    """Run eval harness (agent mode) and save per-quote data to baseline-quotes.json.

    Costs analyst API (one golden run per case). No re-embed, no Voyage.
    Subsequent smoke runs load from baseline-quotes.json at $0.
    """
    from rra.evals.dataset import load_golden
    from rra.evals.run import run_agent

    cases = load_golden()
    if limit is not None:
        cases = cases[:limit]

    quotes: list[dict] = []
    for case in cases:
        print(f"  running {case.id}...", flush=True)
        try:
            response = run_agent(case)
        except Exception as e:
            print(f"  ERROR on {case.id}: {e}", file=sys.stderr)
            continue
        for c in response.citations:
            qt = c.get("quoted_text") or ""
            if qt.strip():
                quotes.append(
                    {
                        "case_id": case.id,
                        "guidance_id": c["guidance_id"],
                        "chunk_index": c["chunk_index"],
                        "quoted_text": qt,
                    }
                )

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(quotes, indent=2))
    print(f"\nSaved {len(quotes)} quotes to {BASELINE_PATH}")


# ─── Step 2: $0 smoke ────────────────────────────────────────────────────────

def run_smoke(table: str = "chunks_rechunk") -> None:
    """Load baseline quotes and run text-only matching against corpus.<table>.

    table must be one of: chunks (live dirty corpus) or chunks_rechunk (clean
    scratch). Using the same baseline-quotes.json for both arms makes the
    before/after delta honest — same quotes, different substrate.
    """
    if table not in _VALID_TABLES:
        print(
            f"ERROR: --table must be one of {sorted(_VALID_TABLES)}, got {table!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    qualified_table = f"corpus.{table}"

    if not BASELINE_PATH.exists():
        print(
            f"ERROR: {BASELINE_PATH} not found.\n"
            "Run --save-baseline first to lock the analyst quotes.",
            file=sys.stderr,
        )
        sys.exit(1)

    quotes: list[dict] = json.loads(BASELINE_PATH.read_text())
    total = len(quotes)
    tau = settings.citation_match_threshold

    # Load all chunks grouped by guidance_id from the requested table.
    try:
        with psycopg.connect(settings.pg_dsn) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT guidance_id, chunk_index, text, char_start"  # noqa: S608
                    f" FROM {qualified_table}"
                    f" ORDER BY guidance_id, chunk_index"
                )
                rows = cur.fetchall()
    except Exception as e:
        print(f"ERROR reading {qualified_table}: {e}", file=sys.stderr)
        if table == "chunks_rechunk":
            print(
                "Scratch table not found. Run: uv run rra-ingest --rechunk-scratch",
                file=sys.stderr,
            )
        sys.exit(1)

    by_guid: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_guid[row["guidance_id"]].append(row)

    total_chunks = sum(len(v) for v in by_guid.values())
    print(f"\nTable: {qualified_table}")
    print(f"Chunks: {total_chunks} across {len(by_guid)} docs")
    print(f"Baseline quotes: {total}")
    print(f"τ = {tau}  (config: citation_match_threshold)\n")

    best_chunk_verified = 0
    doc_level_verified = 0
    best_chunk_scores: list[float] = []
    doc_level_scores: list[float] = []
    missing_guid = 0
    per_quote: list[dict] = []

    for q in quotes:
        guid = q["guidance_id"]
        quoted = q["quoted_text"]

        if guid not in by_guid:
            missing_guid += 1
            per_quote.append(
                {"case_id": q["case_id"], "guid": guid, "table": table, "status": "missing_guid"}
            )
            continue

        chunks = by_guid[guid]

        # Best-chunk: take the highest coverage over all chunks of this guid.
        # sim=None means Step-2 substring hit (perfect score); treat as 1.0.
        best_score = 0.0
        best_v = False
        for chunk in chunks:
            v, sim, _ = match_quote(quoted, chunk["text"], chunk["char_start"])
            score = 1.0 if sim is None else sim
            if score > best_score:
                best_score = score
                best_v = v

        best_chunk_scores.append(best_score)
        if best_v:
            best_chunk_verified += 1

        # Doc-level: match against all chunk texts concatenated.
        # _normalize in match_quote collapses \n\n separators to spaces, so
        # quotes straddling a chunk boundary (due to the join) are findable.
        doc_text = "\n\n".join(c["text"] for c in chunks)
        doc_v, doc_sim, _ = match_quote(quoted, doc_text, 0)
        doc_score = 1.0 if doc_sim is None else (doc_sim or 0.0)
        doc_level_scores.append(doc_score)
        if doc_v:
            doc_level_verified += 1

        per_quote.append(
            {
                "case_id": q["case_id"],
                "guid": guid,
                "table": table,
                "best_chunk_score": round(best_score, 3),
                "best_chunk_verified": best_v,
                "doc_level_score": round(doc_score, 3),
                "doc_level_verified": doc_v,
            }
        )

    assessed = total - missing_guid

    # ── Report ────────────────────────────────────────────────────────────────
    print("=" * 60)
    print(f"BEST-CHUNK:  {best_chunk_verified}/{assessed} verified at τ={tau}")
    print(f"DOC-LEVEL:   {doc_level_verified}/{assessed} verified at τ={tau}")
    print(f"(run --table chunks for dirty baseline, --table chunks_rechunk for clean)")
    print()

    def _band_dist(scores: list[float], label: str) -> None:
        if not scores:
            print(f"{label}: (no data)")
            return
        b = {"≥0.95": 0, "0.85–0.95": 0, "0.70–0.85": 0, "<0.70": 0}
        for s in scores:
            if s >= 0.95:
                b["≥0.95"] += 1
            elif s >= 0.85:
                b["0.85–0.95"] += 1
            elif s >= 0.70:
                b["0.70–0.85"] += 1
            else:
                b["<0.70"] += 1
        dist = "  ".join(f"{k}: {v}" for k, v in b.items())
        print(f"{label}: {dist}")

    _band_dist(best_chunk_scores, "Best-chunk dist")
    _band_dist(doc_level_scores,  "Doc-level  dist")

    if missing_guid:
        print(f"\nWARNING: {missing_guid} quotes skipped (guid not in {qualified_table})")

    gap = doc_level_verified - best_chunk_verified
    print()
    print(f"Gap (doc−chunk): {gap}")
    if assessed > 0 and table == "chunks_rechunk":
        if best_chunk_verified >= 40:
            print("→ Levers A+B resolved straddle. Ready to re-embed (pending your review).")
        elif gap > 5:
            print(
                "→ Cleaning works but boundary straddle is real. "
                "Consider Lever D or tune overlap/separators."
            )
        elif doc_level_verified < 30:
            print(
                "→ Doc-level low — cleaning insufficient. "
                "Tune Lever A regexes and re-run."
            )
        else:
            print("→ Review distribution before deciding next action.")

    # Save per-quote detail keyed by table so both arms can coexist.
    detail_path = BASELINE_PATH.parent / f"smoke-{table}-detail.json"
    detail_path.write_text(json.dumps(per_quote, indent=2))
    print(f"\nPer-quote detail → {detail_path}")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="$0 text-only faithfulness smoke; run against either corpus table"
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help=(
            "Run eval harness (agent mode) to lock the analyst quotes to "
            "evals/baseline-quotes.json. Costs analyst API. Run once before smoking."
        ),
    )
    parser.add_argument(
        "--table",
        default="chunks_rechunk",
        choices=sorted(_VALID_TABLES),
        help=(
            "corpus.<table> to query (default: chunks_rechunk). "
            "Use 'chunks' for the dirty live corpus (before number); "
            "use 'chunks_rechunk' for the clean scratch table (after number)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of golden cases for --save-baseline (default: all 30)",
    )
    args = parser.parse_args()

    if args.save_baseline:
        save_baseline(limit=args.limit)
    else:
        run_smoke(table=args.table)


if __name__ == "__main__":
    main()
