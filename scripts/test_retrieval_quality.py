"""Stage 2 — Retrieval quality test.

Tests whether search_corpus returns the RIGHT passages for known queries.
This is the test that predicts project success: if retrieval is weak, every
agent built on top inherits the weakness.

Method: a set of probe queries, each annotated with the guidance_id(s) we
EXPECT to appear in the top-k. We measure:
  - recall@k: did the expected guidance appear in the top-k results?
  - rank: where did it land (1 = top)?
  - cluster hit: did the top result come from the expected cluster?

The probes are derived from the three device scenarios (CardioWatch AI,
InfusePro, NeuroPath DTx) plus direct concept lookups. Expected guidance
IDs are based on the curated corpus; adjust EXPECTED if your final
manifest differs.

This is a DIAGNOSTIC, not a pass/fail gate. Read the output and judge.
A recall@5 below ~0.7 means retrieval needs work (chunk size, query
rephrasing, hybrid search) before building agents on top.

Usage:
    python scripts/test_retrieval_quality.py
    python scripts/test_retrieval_quality.py --k 10 --verbose
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rra.retrieval import search_corpus  # noqa: E402


@dataclass
class Probe:
    query: str
    expected_guidance_ids: list[str]  # any of these in top-k = hit
    expected_cluster: str
    difficulty: str  # easy | medium | hard
    note: str = ""


# Probes derived from device scenarios + concept lookups.
# expected_guidance_ids reflect the curated corpus; adjust to your manifest.
PROBES: list[Probe] = [
    # ── Easy: direct concept lookups ──
    Probe(
        query="When does a change to an existing device require a new 510(k)?",
        expected_guidance_ids=["99812"],
        expected_cluster="modification-decisions",
        difficulty="easy",
        note="The canonical 'deciding when to submit' doc",
    ),
    Probe(
        query="How does FDA evaluate substantial equivalence in a 510(k)?",
        expected_guidance_ids=["82395"],
        expected_cluster="pathway-classification",
        difficulty="easy",
    ),
    Probe(
        query="What is the De Novo classification process for novel devices?",
        expected_guidance_ids=["72674"],
        expected_cluster="pathway-classification",
        difficulty="easy",
    ),
    Probe(
        query="What are the premarket cybersecurity requirements for medical devices?",
        expected_guidance_ids=["119933"],
        expected_cluster="cybersecurity",
        difficulty="easy",
    ),
    Probe(
        query="What is a Predetermined Change Control Plan for AI-enabled devices?",
        expected_guidance_ids=["166704"],
        expected_cluster="software-samd-ai",
        difficulty="easy",
    ),

    # ── Medium: device-scenario, single primary doc ──
    Probe(
        query="We have a cleared ECG analysis device. We want to add an ML model "
              "that retrains on post-market data. What submission framework applies?",
        expected_guidance_ids=["166704", "184856"],
        expected_cluster="software-samd-ai",
        difficulty="medium",
        note="CardioWatch: should surface AI PCCP and/or AI lifecycle draft",
    ),
    Probe(
        query="Our cleared infusion pump is adding wireless remote dose control "
              "via a smartphone app. Do we need a new submission for this software change?",
        expected_guidance_ids=["99785", "99812"],
        expected_cluster="modification-decisions",
        difficulty="medium",
        note="InfusePro: software-change-to-cleared-device decision",
    ),
    Probe(
        query="A novel digital therapeutic has no predicate device. "
              "What classification pathway should it use?",
        expected_guidance_ids=["72674"],
        expected_cluster="pathway-classification",
        difficulty="medium",
        note="NeuroPath: De Novo for no-predicate novel device",
    ),
    Probe(
        query="What clinical evidence does FDA accept to support a device submission?",
        expected_guidance_ids=["111346"],
        expected_cluster="clinical-evidence",
        difficulty="medium",
    ),
    Probe(
        query="How should human factors and usability be addressed for a device "
              "intended for home use?",
        expected_guidance_ids=["80481", "84830", "163694"],
        expected_cluster="design-controls-qms",
        difficulty="medium",
    ),

    # ── Hard: cross-cluster synthesis or genuine ambiguity ──
    Probe(
        query="Our AI model's performance drifts so we retrain it monthly without "
              "changing the architecture. Does each retraining require notification to FDA?",
        expected_guidance_ids=["166704", "184856", "180978"],
        expected_cluster="software-samd-ai",
        difficulty="hard",
        note="Genuine ambiguity — PCCP framework; correct answer hedges",
    ),
    Probe(
        query="Adding wireless connectivity to a previously standalone device — "
              "what cybersecurity documentation and what change submission apply together?",
        expected_guidance_ids=["119933", "99785"],
        expected_cluster="cybersecurity",
        difficulty="hard",
        note="Cross-cluster: cybersecurity + modification",
    ),
    Probe(
        query="When is a Special 510(k) appropriate instead of a Traditional 510(k) "
              "for a device modification?",
        expected_guidance_ids=["116418"],
        expected_cluster="modification-decisions",
        difficulty="hard",
        note="Specific program eligibility",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=5, help="top-k to retrieve")
    parser.add_argument("--verbose", action="store_true", help="show all returned passages")
    args = parser.parse_args()

    print(f"Retrieval quality test — {len(PROBES)} probes, k={args.k}\n")

    hits = 0
    cluster_hits = 0
    rank_sum = 0
    by_difficulty: dict[str, list[bool]] = {"easy": [], "medium": [], "hard": []}

    for i, probe in enumerate(PROBES, 1):
        try:
            passages = search_corpus(probe.query, k=args.k, filters=None)
        except Exception as e:
            print(f"  [{i}] ✗ ERROR: {e}")
            print(f"       query: {probe.query[:70]}")
            by_difficulty[probe.difficulty].append(False)
            continue

        returned_ids = [p.guidance_id for p in passages]
        # Find the best rank among expected ids
        hit_rank = None
        for rank, gid in enumerate(returned_ids, 1):
            if gid in probe.expected_guidance_ids:
                hit_rank = rank
                break

        is_hit = hit_rank is not None
        top_cluster = passages[0].cluster if passages and hasattr(passages[0], "cluster") else "?"
        is_cluster_hit = top_cluster == probe.expected_cluster

        hits += is_hit
        cluster_hits += is_cluster_hit
        if hit_rank:
            rank_sum += hit_rank
        by_difficulty[probe.difficulty].append(is_hit)

        marker = "✓" if is_hit else "✗"
        rank_str = f"rank {hit_rank}" if hit_rank else "MISS"
        print(f"  [{i}] {marker} [{probe.difficulty}] {rank_str}")
        print(f"       Q: {probe.query[:72]}")
        print(f"       expected one of {probe.expected_guidance_ids} ({probe.expected_cluster})")
        print(f"       got: {returned_ids}")
        if probe.note:
            print(f"       note: {probe.note}")
        if args.verbose:
            for rank, p in enumerate(passages, 1):
                print(f"         {rank}. [{p.guidance_id}] score={getattr(p, 'score', '?')} "
                      f"{getattr(p, 'guidance_title', '')[:50]}")
        print()

    n = len(PROBES)
    print("─" * 50)
    print(f"  recall@{args.k}:        {hits}/{n} = {hits/n:.2f}")
    print(f"  top-cluster hit:   {cluster_hits}/{n} = {cluster_hits/n:.2f}")
    if hits:
        print(f"  mean rank of hit:  {rank_sum/hits:.2f}")
    print()
    print("  By difficulty:")
    for diff in ["easy", "medium", "hard"]:
        results = by_difficulty[diff]
        if results:
            h = sum(results)
            print(f"    {diff:7s}: {h}/{len(results)} = {h/len(results):.2f}")

    print()
    print("  Interpretation:")
    print("    recall@5 > 0.85 overall, easy > 0.95 → retrieval is solid")
    print("    recall@5 0.7-0.85 → workable; note which probes miss")
    print("    recall@5 < 0.7 → retrieval needs work before building agents")
    print("    Hard-band misses are expected and often acceptable (ambiguous Qs)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
