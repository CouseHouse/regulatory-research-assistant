"""Scrape the FDA guidance index and produce a candidate manifest with
cluster assignments.

NOT a production tool. Run-it-once-per-corpus utility.

What it does:
  1. Fetches https://www.fda.gov/files/api/datatables/static/search-for-guidance.json
  2. Filters to CDRH Final guidances with downloadable PDFs
  3. Applies CLUSTER_KEYWORDS to assign each candidate to one of six clusters
     (or 'unclassified' if no match)
  4. Device-specific filtering: pathway-classification entries whose titles
     match DEVICE_SPECIFIC_HINTS are reassigned to the pseudo-cluster
     'device-specific' and excluded by default (use --include-unclassified
     to keep them for inspection).
  5. Optionally HEAD-verifies each URL
  6. Writes data/corpus/manifest.candidates.json — the input to the review tool

What it doesn't do:
  - Replace your judgment. Output is a CANDIDATE list. Use the review tool
    (scripts/review_corpus.py) to approve/reject entries before ingest.
  - Survive FDA changing the schema. If parsing breaks, run with --debug
    and look at the first raw row.

Usage:
    python scripts/scrape_fda_corpus.py                       # full run, all clusters
    python scripts/scrape_fda_corpus.py --limit 30 --debug    # quick test
    python scripts/scrape_fda_corpus.py --clusters software-samd-ai cybersecurity
    python scripts/scrape_fda_corpus.py --include-unclassified  # keep no-match and device-specific too
    python scripts/scrape_fda_corpus.py --no-verify           # skip HEAD checks
    python scripts/scrape_fda_corpus.py --include-drafts      # include Draft guidances

After this finishes, run:
    python scripts/review_corpus.py
to approve/reject entries and produce the final data/corpus/manifest.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

# ─── Configuration ──────────────────────────────────────────────────────────

FDA_INDEX_URL = "https://www.fda.gov/files/api/datatables/static/search-for-guidance.json"

FDA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.fda.gov/regulatory-information/search-fda-guidance-documents",
}

FDA_PDF_URL_TEMPLATE = "https://www.fda.gov/media/{id}/download"

# Cluster taxonomy — each cluster maps to a set of title-keyword patterns.
# Order matters: clusters earlier in the list win ties.
#
# These are deliberately broad. False positives are fine because the review
# tool lets you reject them; false negatives (relevant docs not matched) are
# the bigger problem because you might miss them entirely.
CLUSTER_KEYWORDS: dict[str, list[str]] = {
    "software-samd-ai": [
        "software as a medical device",
        "samd",
        "software function",
        "artificial intelligence",
        "machine learning",
        "ai-enabled",
        "ai/ml",
        "ai/ml-based",
        "ai/ml action plan",
        "predetermined change control",
        "pccp",
        "good machine learning",
        "good machine learning practice",
        "gmlp",
        "lifecycle management",
        "clinical decision support",
        "digital health",
        "computer-assisted detection",
        "computer aided detection",
        "general principles of software",
        "off-the-shelf software",
        "off the shelf software",
        "principles for software validation",
        "transparency for machine learning",
        "device software function",
        "multiple function device",
    ],
    "cybersecurity": [
        "cybersecurity",
        "cyber security",
        "cyber device",
        "cyber devices",
        "section 524b",
        "off-the-shelf software",
    ],
    "modification-decisions": [
        "deciding when to submit",
        "changes to existing device",
        "change to an existing device",
        "software change",
        "special 510",
        "modifications to devices",
        "30-day notice",
        "135-day pma",
        "75-day humanitarian",
        "device modification",
        "real-time pma",
        "annual report",
        "pma supplement",
        "enforcement policy",
    ],
    "pathway-classification": [
        "510(k)",
        "510k",
        "de novo",
        "premarket notification",
        "substantial equivalence",
        "premarket approval",
        "humanitarian device exemption",
        "refuse to accept",
        "acceptance review",
        "q-submission",
        "pre-submission",
        "benefit-risk",
        "abbreviated 510",
        "format for traditional",
        "evaluating substantial equivalence",
        "breakthrough device",
        "safer technologies",
        "step program",
        "513(g)",
        "request for information",
        "investigational device exemption",
        "ide submission",
        "ide application",
    ],
    "design-controls-qms": [
        "design control",
        "quality system",
        "21 cfr 820",
        "human factors",
        "usability engineering",
        "iso 14971",
        "risk management",
        "postmarket surveillance",
        "section 522",
        "design considerations",
        "recall",
        "product enhancement",
        "manufacturing practice",
        "qmsr",
        "quality management system regulation",
        "iso 13485",
        "medical device reporting",
        " mdr ",
    ],
    "clinical-evidence": [
        "real-world evidence",
        "real-world data",
        "real world evidence",
        "real world data",
        "clinical evaluation",
        "patient-reported outcome",
        "patient reported outcome",
        "pivotal clinical",
        "clinical investigation",
        "clinical performance",
        "animal stud",
        "adaptive design",
        "acceptance of clinical data",
    ],
}

# Communication types we generally want to skip. These are auxiliary docs.
# Override with --include-auxiliary if you want them in the candidate list.
SKIP_COMMUNICATION_TYPES = {
    "Industry Letter",
    "Small Entity Compliance Guide",
}

# Device-type keywords that flag a pathway-classification entry as a
# single-device-class 510(k) submission guide rather than a cross-cutting
# topic guide. Entries that match are reassigned to the pseudo-cluster
# "device-specific" and excluded by default (use --include-unclassified to
# keep them for inspection).
DEVICE_SPECIFIC_HINTS: list[str] = [
    "stent",
    "catheter",
    "tampon",
    "condom",
    "pacemaker",
    "infusion pump",
    "glucose monitor",
    "wheelchair",
    "tonometer",
    "endoscope",
    "dialysis",
    "implant",
    "prosthet",
    "syringe",
    "needle",
    "valve",
    " lens",
    "hearing aid",
    "ventilator",
    "defibrillator",
    "x-ray",
    "ultrasound",
    " mri ",
    "brachytherapy",
    "bone anchor",
    "atherectomy",
    "keratome",
    "dental",
    "ophthalmic",
    "surgical mask",
    "biliary",
    "urological",
    "gynecological",
    "incontinence",
    "ablation",
    "morcellation",
]

# Title prefixes that reliably indicate a single-device-class guide.
DEVICE_SPECIFIC_PREFIXES: list[str] = [
    "premarket notification submissions for ",
    "premarket approval applications for ",
    "class ii special controls guidance document for ",
    "guidance for the submission of ",
]

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus" / "manifest.candidates.json"

REQUEST_TIMEOUT = 30.0
INTER_REQUEST_DELAY = 0.15

# ─── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class CandidateEntry:
    id: str
    title: str
    url: str
    cluster: str
    cluster_matches: list[str]  # all clusters whose keywords matched, for review context
    matched_keywords: list[str]  # the actual keywords that triggered the assignment
    issued: str | None = None
    fda_center: str | None = None
    status: str | None = None
    communication_type: str | None = None


@dataclass
class VerifiedEntry:
    id: str
    title: str
    url: str
    cluster: str
    cluster_matches: list[str]
    matched_keywords: list[str]
    issued: str | None = None
    fda_center: str | None = None
    status: str | None = None
    communication_type: str | None = None
    content_type: str | None = None
    content_length: int | None = None
    last_modified: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)


# ─── Fetching ───────────────────────────────────────────────────────────────


def fetch_fda_index() -> list[dict[str, Any]]:
    print(f"  → GET {FDA_INDEX_URL}", file=sys.stderr)

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=FDA_HEADERS, follow_redirects=True) as client:
        response = client.get(FDA_INDEX_URL)
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected response shape: got {type(data).__name__}, expected list. "
            f"FDA endpoint schema may have changed."
        )

    print(f"  ← {len(data)} rows received", file=sys.stderr)
    return data


# ─── Parsing ────────────────────────────────────────────────────────────────


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"')
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_pdf_id(html_fragment: str) -> str | None:
    match = re.search(r"/media/(\d+)/download", html_fragment)
    return match.group(1) if match else None


def assign_cluster(title: str) -> tuple[str, list[str], list[str]]:
    """Determine cluster, all matched clusters, and matched keywords for a title.

    Returns:
        (primary_cluster, all_matching_clusters, matched_keywords)

    The primary_cluster is the first cluster in CLUSTER_KEYWORDS order that
    matches. all_matching_clusters lists every cluster with at least one
    keyword match — useful in the review UI to surface ambiguous entries.
    """
    title_lower = title.lower()
    all_matches: list[str] = []
    matched_keywords: list[str] = []

    for cluster, keywords in CLUSTER_KEYWORDS.items():
        matches_in_this_cluster = [kw for kw in keywords if kw in title_lower]
        if matches_in_this_cluster:
            all_matches.append(cluster)
            matched_keywords.extend(matches_in_this_cluster)

    primary = all_matches[0] if all_matches else "unclassified"
    return primary, all_matches, matched_keywords


def is_device_specific(title: str) -> bool:
    """Return True if *title* refers to a single device class rather than a
    cross-cutting regulatory topic.

    Used to demote pathway-classification entries that are really just
    device-class-specific 510(k) submission guides (tampons, stents, etc.)
    to the pseudo-cluster 'device-specific'.
    """
    t = title.lower()
    return any(hint in t for hint in DEVICE_SPECIFIC_HINTS) or any(
        t.startswith(prefix) for prefix in DEVICE_SPECIFIC_PREFIXES
    )


def parse_row(
    row: dict[str, Any],
    *,
    include_drafts: bool = False,
    include_auxiliary: bool = False,
) -> CandidateEntry | None:
    """Convert one FDA row into a CandidateEntry, or None if it fails filters."""

    # CDRH only
    product_field = str(row.get("field_regulated_product_field", ""))
    if "Medical Devices" not in product_field:
        return None

    # Final only (unless including drafts)
    status = str(row.get("field_final_guidance_1", ""))
    if status != "Final" and not include_drafts:
        return None

    # Skip auxiliary doc types unless explicitly included
    comm_type = str(row.get("field_communication_type", ""))
    if not include_auxiliary and comm_type in SKIP_COMMUNICATION_TYPES:
        return None

    # Must have a downloadable PDF
    media_html = str(row.get("field_associated_media_2", ""))
    if not media_html:
        return None
    pdf_id = _extract_pdf_id(media_html)
    if not pdf_id:
        return None

    title_html = str(row.get("title", ""))
    title = _strip_html(title_html)
    if not title:
        return None

    pdf_url = FDA_PDF_URL_TEMPLATE.format(id=pdf_id)
    cluster, cluster_matches, matched_keywords = assign_cluster(title)

    # Demote single-device-class pathway guides to the "device-specific"
    # pseudo-cluster so they don't pollute the cross-cutting topic clusters.
    if cluster == "pathway-classification" and is_device_specific(title):
        cluster = "device-specific"

    issued = row.get("field_issue_datetime") or None
    fda_center = row.get("field_center") or None

    return CandidateEntry(
        id=pdf_id,
        title=title,
        url=pdf_url,
        cluster=cluster,
        cluster_matches=cluster_matches,
        matched_keywords=matched_keywords,
        issued=str(issued) if issued else None,
        fda_center=str(fda_center) if fda_center else None,
        status=status,
        communication_type=comm_type or None,
    )


# ─── Verification ───────────────────────────────────────────────────────────


def verify_url(client: httpx.Client, candidate: CandidateEntry) -> VerifiedEntry:
    verification: dict[str, Any] = {"ok": False, "reason": None}
    content_type: str | None = None
    content_length: int | None = None
    last_modified: str | None = None

    try:
        response = client.head(candidate.url, follow_redirects=True, timeout=REQUEST_TIMEOUT)
        status_code = response.status_code
        verification["status_code"] = status_code

        if status_code != 200:
            verification["reason"] = f"HTTP {status_code}"
        else:
            content_type = response.headers.get("content-type")
            cl = response.headers.get("content-length")
            content_length = int(cl) if cl and cl.isdigit() else None
            last_modified = response.headers.get("last-modified")

            if content_type and "pdf" in content_type.lower():
                verification["ok"] = True
            else:
                verification["reason"] = f"not a PDF (content-type: {content_type})"

    except httpx.HTTPError as e:
        verification["reason"] = f"network error: {type(e).__name__}: {e}"

    return VerifiedEntry(
        id=candidate.id,
        title=candidate.title,
        url=candidate.url,
        cluster=candidate.cluster,
        cluster_matches=candidate.cluster_matches,
        matched_keywords=candidate.matched_keywords,
        issued=candidate.issued,
        fda_center=candidate.fda_center,
        status=candidate.status,
        communication_type=candidate.communication_type,
        content_type=content_type,
        content_length=content_length,
        last_modified=last_modified,
        verification=verification,
    )


# ─── Pipeline ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=None, help="cap candidates (testing)")
    parser.add_argument(
        "--clusters",
        nargs="+",
        default=None,
        help=f"restrict to these clusters: {list(CLUSTER_KEYWORDS.keys())}",
    )
    parser.add_argument(
        "--include-unclassified",
        action="store_true",
        help="include entries that didn't match any cluster, and device-specific entries",
    )
    parser.add_argument("--include-drafts", action="store_true", help="include Draft guidances")
    parser.add_argument(
        "--include-auxiliary",
        action="store_true",
        help="include Industry Letters and Small Entity Compliance Guides",
    )
    parser.add_argument("--no-verify", action="store_true", help="skip HEAD verification")
    parser.add_argument("--debug", action="store_true", help="verbose output")
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_PATH, help=f"output (default: {OUTPUT_PATH})"
    )
    args = parser.parse_args()

    print("Fetching FDA guidance index...", file=sys.stderr)
    try:
        rows = fetch_fda_index()
    except Exception as e:
        print(f"  ✗ Failed: {e}", file=sys.stderr)
        return 1

    print(f"\nParsing {len(rows)} rows...", file=sys.stderr)
    candidates: list[CandidateEntry] = []
    for row in rows:
        c = parse_row(
            row,
            include_drafts=args.include_drafts,
            include_auxiliary=args.include_auxiliary,
        )
        if c is not None:
            candidates.append(c)
    print(f"  → {len(candidates)} CDRH finals with downloadable PDFs", file=sys.stderr)

    # Cluster filter — drop pseudo-clusters unless caller opts in
    _excluded_clusters = {"unclassified", "device-specific"}
    if not args.include_unclassified:
        before = len(candidates)
        candidates = [c for c in candidates if c.cluster not in _excluded_clusters]
        dropped = before - len(candidates)
        print(
            f"  → dropped {dropped} unclassified/device-specific entries",
            file=sys.stderr,
        )

    if args.clusters:
        before = len(candidates)
        candidates = [c for c in candidates if c.cluster in args.clusters]
        print(
            f"  → {len(candidates)} after --clusters filter (dropped {before - len(candidates)})",
            file=sys.stderr,
        )

    if args.limit:
        candidates = candidates[: args.limit]

    if args.debug:
        print("\nFirst 15 candidates:", file=sys.stderr)
        for c in candidates[:15]:
            print(f"    [{c.cluster}] {c.id}: {c.title[:75]}", file=sys.stderr)

    if not candidates:
        print("  ✗ No candidates. Loosen filters.", file=sys.stderr)
        return 1

    # Verify
    if args.no_verify:
        verified: list[VerifiedEntry] = [
            VerifiedEntry(
                id=c.id,
                title=c.title,
                url=c.url,
                cluster=c.cluster,
                cluster_matches=c.cluster_matches,
                matched_keywords=c.matched_keywords,
                issued=c.issued,
                fda_center=c.fda_center,
                status=c.status,
                communication_type=c.communication_type,
                verification={"ok": True, "reason": "skipped"},
            )
            for c in candidates
        ]
    else:
        print(f"\nVerifying {len(candidates)} URLs...", file=sys.stderr)
        verified = []
        with httpx.Client(headers=FDA_HEADERS) as client:
            for i, candidate in enumerate(candidates, 1):
                result = verify_url(client, candidate)
                verified.append(result)
                if args.debug or not result.verification["ok"]:
                    marker = "✓" if result.verification["ok"] else "✗"
                    reason = result.verification.get("reason", "")
                    suffix = f" — {reason}" if reason else ""
                    print(f"  {marker} [{i}/{len(candidates)}] {result.id}{suffix}", file=sys.stderr)
                time.sleep(INTER_REQUEST_DELAY)

    alive = [v for v in verified if v.verification["ok"]]
    dead = [v for v in verified if not v.verification["ok"]]

    print(f"\nResults: {len(alive)} verified, {len(dead)} failed", file=sys.stderr)

    # Write candidate manifest
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(v) for v in alive]
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(alive)} candidates to {args.output}", file=sys.stderr)

    # Cluster breakdown
    cluster_counts: dict[str, int] = {}
    for v in alive:
        cluster_counts[v.cluster] = cluster_counts.get(v.cluster, 0) + 1
    print("\nCluster breakdown:", file=sys.stderr)
    for cluster, count in sorted(cluster_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {cluster}", file=sys.stderr)

    print(
        "\nNext step:",
        f"  python scripts/review_corpus.py",
        "to approve/reject entries and produce data/corpus/manifest.json.",
        sep="\n",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
