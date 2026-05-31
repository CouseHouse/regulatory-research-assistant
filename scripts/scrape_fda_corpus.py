"""Scrape the FDA guidance documents index to build a corpus manifest.

NOT a production tool. Run-it-once-per-project utility. Lives in scripts/ to
keep it out of the application package and out of the strict-mypy net.

Pipeline:
  1. Fetch https://www.fda.gov/files/api/datatables/static/search-for-guidance.json
     (the JSON endpoint that backs the FDA guidance database web UI)
  2. Filter by:
       - field_regulated_product_field contains "Medical Devices"
         (CDRH; we don't want food / dietary / animal guidances)
       - field_final_guidance_1 == "Final" (skip drafts unless --include-drafts)
       - field_associated_media_2 contains a /media/{id}/download link
         (skip entries that have no downloadable PDF)
       - title matches at least one TOPIC_KEYWORDS set, OR --no-topic-filter
  3. Verify each candidate URL with a HEAD request — alive? returns PDF?
  4. Write data/corpus/manifest.json — input to src/rra/ingest.py

What it doesn't do:
  - Replace your judgment. Output is a CANDIDATE list. Review it.
  - Survive FDA changing the schema. If parsing breaks, run with --debug and
    look at the first raw row to see the new shape.

Usage:
    python scripts/scrape_fda_corpus.py                       # default run
    python scripts/scrape_fda_corpus.py --limit 30 --debug    # quick test
    python scripts/scrape_fda_corpus.py --topics ai-ml cybersecurity
    python scripts/scrape_fda_corpus.py --no-topic-filter     # all CDRH finals
    python scripts/scrape_fda_corpus.py --include-drafts
    python scripts/scrape_fda_corpus.py --no-verify           # skip HEAD checks
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

# Headers the endpoint expects. Without X-Requested-With it returns HTML.
FDA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.fda.gov/regulatory-information/search-fda-guidance-documents",
}

# The PDF URL pattern. Used to build PDF URLs we'll feed to ingest.py.
FDA_PDF_URL_TEMPLATE = "https://www.fda.gov/media/{id}/download"

# Topic taxonomy — keyword sets matching the Option 1 curation criteria.
# Deliberately broad; YOU narrow the list in the review step.
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "ai-ml": [
        "artificial intelligence",
        "machine learning",
        "ai-enabled",
        "ai/ml",
        "predetermined change control",
        "pccp",
    ],
    "samd": [
        "software as a medical device",
        "samd",
        "software functions",
        "digital health",
        "clinical decision support",
    ],
    "cybersecurity": [
        "cybersecurity",
        "cyber security",
    ],
    "510k": [
        "510(k)",
        "510k",
        "premarket notification",
        "substantial equivalence",
        "special 510",
        "abbreviated 510",
    ],
    "design-controls": [
        "design controls",
        "quality system",
        "21 cfr 820",
        "design considerations",
        "human factors",
    ],
    "software-validation": [
        "software validation",
        "off-the-shelf software",
        "general principles of software",
        "principles for software validation",
    ],
    "denovo": [
        "de novo",
    ],
    "modifications": [
        "modifications to existing",
        "deciding when to submit",
        "change to an existing device",
        "software change",
    ],
    "clinical-evidence": [
        "real-world evidence",
        "real-world data",
        "clinical evaluation",
        "patient-reported outcome",
    ],
}

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus" / "manifest.json"

REQUEST_TIMEOUT = 30.0
INTER_REQUEST_DELAY = 0.15  # seconds between HEAD requests; be polite

# ─── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class CandidateEntry:
    id: str
    title: str
    url: str
    topics: list[str]
    issued: str | None = None
    fda_center: str | None = None
    status: str | None = None
    communication_type: str | None = None


@dataclass
class VerifiedEntry:
    id: str
    title: str
    url: str
    topics: list[str]
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
    """Fetch the FDA guidance index. Returns the raw row list."""
    print(f"  → GET {FDA_INDEX_URL}", file=sys.stderr)

    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=FDA_HEADERS, follow_redirects=True) as client:
        response = client.get(FDA_INDEX_URL)
        response.raise_for_status()

        # The endpoint returns a bare JSON array of row dicts.
        data = response.json()

    if not isinstance(data, list):
        raise RuntimeError(
            f"Unexpected response shape from FDA index: got {type(data).__name__}, "
            f"expected list. The endpoint schema may have changed."
        )

    print(f"  ← {len(data)} rows received", file=sys.stderr)
    return data


# ─── Parsing ────────────────────────────────────────────────────────────────


def _strip_html(s: str) -> str:
    """Strip HTML tags and decode common entities."""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&#039;", "'").replace("&quot;", '"')
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_pdf_id(html_fragment: str) -> str | None:
    """Pull a numeric PDF id out of an HTML fragment containing
    /media/{id}/download."""
    match = re.search(r"/media/(\d+)/download", html_fragment)
    return match.group(1) if match else None


def parse_row(row: dict[str, Any], *, include_drafts: bool = False) -> CandidateEntry | None:
    """Convert one FDA row into a CandidateEntry, or None if it fails any
    structural filter."""

    # 1. CDRH only — drop food, dietary supplements, animal products, etc.
    product_field = str(row.get("field_regulated_product_field", ""))
    if "Medical Devices" not in product_field:
        return None

    # 2. Final guidance only (unless including drafts)
    status = str(row.get("field_final_guidance_1", ""))
    if status != "Final" and not include_drafts:
        return None

    # 3. Must have a downloadable PDF
    media_html = str(row.get("field_associated_media_2", ""))
    if not media_html:
        return None
    pdf_id = _extract_pdf_id(media_html)
    if not pdf_id:
        return None

    # 4. Extract the title (from the title field's anchor text)
    title_html = str(row.get("title", ""))
    title = _strip_html(title_html)
    if not title:
        return None

    pdf_url = FDA_PDF_URL_TEMPLATE.format(id=pdf_id)
    topics = match_topics(title)

    issued = row.get("field_issue_datetime") or None
    fda_center = row.get("field_center") or None
    communication_type = row.get("field_communication_type") or None

    return CandidateEntry(
        id=pdf_id,
        title=title,
        url=pdf_url,
        topics=topics,
        issued=str(issued) if issued else None,
        fda_center=str(fda_center) if fda_center else None,
        status=status,
        communication_type=str(communication_type) if communication_type else None,
    )


def match_topics(title: str) -> list[str]:
    """Return the list of topics whose keywords appear in the title."""
    title_lower = title.lower()
    return [
        topic
        for topic, keywords in TOPIC_KEYWORDS.items()
        if any(kw in title_lower for kw in keywords)
    ]


# ─── Verification ───────────────────────────────────────────────────────────


def verify_url(client: httpx.Client, candidate: CandidateEntry) -> VerifiedEntry:
    """HEAD the PDF URL; return a VerifiedEntry with the result."""
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
        topics=candidate.topics,
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
    parser.add_argument("--limit", type=int, default=None, help="cap candidates (for testing)")
    parser.add_argument(
        "--topics", nargs="+", default=None, help=f"restrict to: {list(TOPIC_KEYWORDS.keys())}"
    )
    parser.add_argument(
        "--no-topic-filter",
        action="store_true",
        help="include all CDRH finals regardless of title topic match",
    )
    parser.add_argument("--include-drafts", action="store_true", help="include Draft guidances")
    parser.add_argument("--no-verify", action="store_true", help="skip URL verification (faster)")
    parser.add_argument("--debug", action="store_true", help="verbose output")
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_PATH, help=f"output (default: {OUTPUT_PATH})"
    )
    args = parser.parse_args()

    # 1. Fetch
    print("Fetching FDA guidance index...", file=sys.stderr)
    try:
        rows = fetch_fda_index()
    except Exception as e:
        print(f"  ✗ Failed to fetch index: {e}", file=sys.stderr)
        return 1

    if args.debug and rows:
        print("\nFirst raw row (for schema reference):", file=sys.stderr)
        print(json.dumps(rows[0], indent=2)[:1500], file=sys.stderr)
        print("...", file=sys.stderr)

    # 2. Parse and apply structural filters
    print(f"\nParsing {len(rows)} rows...", file=sys.stderr)
    candidates: list[CandidateEntry] = []
    for row in rows:
        c = parse_row(row, include_drafts=args.include_drafts)
        if c is not None:
            candidates.append(c)
    print(f"  → {len(candidates)} CDRH finals with downloadable PDFs", file=sys.stderr)

    # 3. Topic filter
    if not args.no_topic_filter:
        before = len(candidates)
        if args.topics:
            # User-specified topics — keep entries matching ANY of them
            candidates = [c for c in candidates if any(t in args.topics for t in c.topics)]
        else:
            # Default — keep entries matching ANY known topic
            candidates = [c for c in candidates if c.topics]
        print(
            f"  → {len(candidates)} after topic filter (dropped {before - len(candidates)})",
            file=sys.stderr,
        )

    if args.limit:
        candidates = candidates[: args.limit]
        print(f"  → limited to {len(candidates)}", file=sys.stderr)

    if args.debug:
        print("\nFirst 15 candidates:", file=sys.stderr)
        for c in candidates[:15]:
            topics_str = ",".join(c.topics) if c.topics else "(no topic)"
            print(f"    • [{topics_str}] {c.id}: {c.title[:80]}", file=sys.stderr)

    if not candidates:
        print("  ✗ No candidates. Try --no-topic-filter to see all CDRH finals.", file=sys.stderr)
        return 1

    # 4. Verify (or skip)
    if args.no_verify:
        verified: list[VerifiedEntry] = [
            VerifiedEntry(
                id=c.id,
                title=c.title,
                url=c.url,
                topics=c.topics,
                issued=c.issued,
                fda_center=c.fda_center,
                status=c.status,
                communication_type=c.communication_type,
                verification={"ok": True, "reason": "skipped"},
            )
            for c in candidates
        ]
    else:
        print(f"\nVerifying {len(candidates)} URLs (HEAD requests)...", file=sys.stderr)
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

    # 5. Report
    alive = [v for v in verified if v.verification["ok"]]
    dead = [v for v in verified if not v.verification["ok"]]

    print(f"\nResults: {len(alive)} verified, {len(dead)} failed", file=sys.stderr)
    if dead:
        print("\nFailed URLs (will not be written to manifest):", file=sys.stderr)
        for d in dead:
            print(f"  ✗ {d.id} — {d.verification.get('reason')} — {d.title[:60]}", file=sys.stderr)

    # 6. Write output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(v) for v in alive]
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nWrote {len(alive)} entries to {args.output}", file=sys.stderr)

    # 7. Topic breakdown
    if alive:
        topic_counts: dict[str, int] = {}
        for v in alive:
            for t in v.topics or ["(no-topic)"]:
                topic_counts[t] = topic_counts.get(t, 0) + 1
        print("\nTopic breakdown:", file=sys.stderr)
        for t, n in sorted(topic_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:4d}  {t}", file=sys.stderr)

    # 8. Next steps
    print(
        "\nNext steps:",
        f"  1. Review {args.output} — drop entries you don't want",
        "  2. The 'verification' field is debug info; ingest.py should ignore it",
        "  3. Update ingest.py to read this manifest instead of _CORPUS_URLS",
        "  4. Smoke test: uv run python -m rra.ingest --limit 5",
        sep="\n",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
