"""Review corpus candidates from the scraper. Approve/reject/skip via keyboard.

Workflow:
  1. scripts/scrape_fda_corpus.py produces data/corpus/manifest.candidates.json
  2. This tool: review each candidate, mark as approve/reject/skip
  3. On export: writes data/corpus/manifest.json with only approved entries

Decisions are saved continuously to manifest.candidates.json (each entry
gets a `review` field). Safe to quit and resume.

Keys:
  j / ↓     next candidate
  k / ↑     previous candidate
  a         approve
  r         reject
  s         skip (default state)
  t         tab through cluster_matches (reassign cluster)
  o         open PDF URL in browser (xdg-open)
  /         search title (filter view)
  c         show only one cluster (cycle)
  u         show only unreviewed
  e         export approved to manifest.json
  q         quit (auto-saves)
  ?         help

Usage:
  python scripts/review_corpus.py
  python scripts/review_corpus.py --candidates data/corpus/manifest.candidates.json
"""

from __future__ import annotations

import argparse
import curses
import json
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

CANDIDATES_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus" / "manifest.candidates.json"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus" / "manifest.json"


class Review(str, Enum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


@dataclass
class Entry:
    raw: dict[str, Any]  # the original dict from the JSON file
    review: Review = Review.UNREVIEWED
    cluster_override: str | None = None  # if user reassigned cluster

    @property
    def id(self) -> str:
        return str(self.raw["id"])

    @property
    def title(self) -> str:
        return str(self.raw["title"])

    @property
    def cluster(self) -> str:
        return self.cluster_override or str(self.raw.get("cluster", "unclassified"))

    @property
    def cluster_matches(self) -> list[str]:
        return list(self.raw.get("cluster_matches", []))

    @property
    def matched_keywords(self) -> list[str]:
        return list(self.raw.get("matched_keywords", []))

    @property
    def issued(self) -> str:
        return str(self.raw.get("issued") or "?")

    @property
    def communication_type(self) -> str:
        return str(self.raw.get("communication_type") or "?")

    @property
    def url(self) -> str:
        return str(self.raw["url"])

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.raw)
        d["review"] = self.review.value
        if self.cluster_override:
            d["cluster"] = self.cluster_override
            d["cluster_overridden"] = True
        return d


# ─── Persistence ────────────────────────────────────────────────────────────


def load_candidates(path: Path) -> list[Entry]:
    if not path.exists():
        raise FileNotFoundError(
            f"Candidates file not found at {path}.\n"
            f"Run `python scripts/scrape_fda_corpus.py` first."
        )

    raw_entries = json.loads(path.read_text())
    entries: list[Entry] = []
    for raw in raw_entries:
        review_str = raw.pop("review", Review.UNREVIEWED.value)
        try:
            review = Review(review_str)
        except ValueError:
            review = Review.UNREVIEWED
        cluster_override = raw.get("cluster_overridden") and raw.get("cluster") or None
        entries.append(Entry(raw=raw, review=review, cluster_override=cluster_override))
    return entries


def save_candidates(entries: list[Entry], path: Path) -> None:
    payload = [e.to_dict() for e in entries]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def export_manifest(entries: list[Entry], path: Path) -> int:
    """Write the final manifest.json with only approved entries.
    Strips review metadata. Returns count."""
    approved = [e for e in entries if e.review == Review.APPROVED]
    payload = []
    for e in approved:
        d = dict(e.raw)
        # Drop review-tool-only fields
        d.pop("review", None)
        d.pop("cluster_overridden", None)
        d.pop("cluster_matches", None)
        d.pop("matched_keywords", None)
        d.pop("verification", None)
        # Apply cluster override if any
        if e.cluster_override:
            d["cluster"] = e.cluster_override
        payload.append(d)

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return len(approved)


# ─── TUI ────────────────────────────────────────────────────────────────────


@dataclass
class State:
    entries: list[Entry]
    candidates_path: Path
    manifest_path: Path
    cursor: int = 0  # index into filtered_indices
    filtered_indices: list[int] = field(default_factory=list)
    cluster_filter: str | None = None  # None = all clusters
    only_unreviewed: bool = False
    status_message: str = ""

    def refilter(self) -> None:
        """Recompute filtered_indices based on current filters."""
        self.filtered_indices = []
        for i, e in enumerate(self.entries):
            if self.cluster_filter and e.cluster != self.cluster_filter:
                continue
            if self.only_unreviewed and e.review != Review.UNREVIEWED:
                continue
            self.filtered_indices.append(i)
        if self.cursor >= len(self.filtered_indices):
            self.cursor = max(0, len(self.filtered_indices) - 1)

    @property
    def current_entry(self) -> Entry | None:
        if not self.filtered_indices:
            return None
        return self.entries[self.filtered_indices[self.cursor]]

    def counts(self) -> dict[str, int]:
        c = {r.value: 0 for r in Review}
        for e in self.entries:
            c[e.review.value] += 1
        return c

    def cluster_list(self) -> list[str]:
        return sorted({e.cluster for e in self.entries})


def draw(stdscr: Any, state: State) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 12 or w < 60:
        stdscr.addstr(0, 0, "Window too small. Resize to at least 60x12.")
        stdscr.refresh()
        return

    # ─── Header ─────────────────────────────────────────────────
    counts = state.counts()
    total = sum(counts.values())
    header = (
        f"Corpus Review  │  {counts['approved']} ✓  "
        f"{counts['rejected']} ✗  "
        f"{counts['skipped']} ⊘  "
        f"{counts['unreviewed']} ?  │  {total} total"
    )
    filter_bits = []
    if state.cluster_filter:
        filter_bits.append(f"cluster={state.cluster_filter}")
    if state.only_unreviewed:
        filter_bits.append("unreviewed only")
    filter_line = (
        f"  filtered to {len(state.filtered_indices)}: {', '.join(filter_bits)}"
        if filter_bits
        else f"  showing all {len(state.filtered_indices)}"
    )

    stdscr.addstr(0, 0, header[: w - 1], curses.A_BOLD)
    stdscr.addstr(1, 0, filter_line[: w - 1])
    stdscr.addstr(2, 0, "─" * (w - 1))

    if not state.filtered_indices:
        stdscr.addstr(4, 2, "No entries match current filters.")
        stdscr.addstr(h - 1, 0, "[q]uit  [c]luster filter  [u]nreviewed toggle")
        stdscr.refresh()
        return

    # ─── Main entry view ───────────────────────────────────────
    e = state.current_entry
    assert e is not None
    y = 4

    review_marker = {
        Review.APPROVED: "✓ APPROVED",
        Review.REJECTED: "✗ REJECTED",
        Review.SKIPPED: "⊘ SKIPPED",
        Review.UNREVIEWED: "? UNREVIEWED",
    }[e.review]

    progress = f"  [{state.cursor + 1}/{len(state.filtered_indices)}]  {review_marker}"
    stdscr.addstr(y, 0, progress[: w - 1], curses.A_DIM)
    y += 2

    # Title wraps
    title_label = "  Title:    "
    stdscr.addstr(y, 0, title_label, curses.A_BOLD)
    title_indent = len(title_label)
    title_width = w - title_indent - 1
    title_lines = wrap_text(e.title, title_width)
    for j, line in enumerate(title_lines[:3]):
        stdscr.addstr(y + j, title_indent if j == 0 else title_indent, line)
    y += min(len(title_lines), 3) + 1

    stdscr.addstr(y, 0, f"  ID:       {e.id}"); y += 1
    stdscr.addstr(y, 0, f"  Cluster:  {e.cluster}"); y += 1
    if len(e.cluster_matches) > 1:
        stdscr.addstr(y, 0, f"            (also matched: {', '.join(c for c in e.cluster_matches if c != e.cluster)})", curses.A_DIM)
        y += 1
    if e.matched_keywords:
        kw_str = ', '.join(e.matched_keywords[:8])
        if len(e.matched_keywords) > 8:
            kw_str += f", ... +{len(e.matched_keywords) - 8} more"
        stdscr.addstr(y, 0, f"            keywords: {kw_str[: w - 24]}", curses.A_DIM)
        y += 1
    stdscr.addstr(y, 0, f"  Issued:   {e.issued}"); y += 1
    stdscr.addstr(y, 0, f"  Type:     {e.communication_type}"); y += 1
    stdscr.addstr(y, 0, f"  URL:      {e.url[: w - 14]}", curses.A_DIM); y += 1

    # ─── Footer ─────────────────────────────────────────────────
    footer = (
        "[a]pprove  [r]eject  [s]kip  [j/k]nav  [o]pen URL  "
        "[c]luster  [u]nreviewed  [e]xport  [q]uit  [?]help"
    )
    stdscr.addstr(h - 1, 0, footer[: w - 1], curses.A_REVERSE)

    if state.status_message:
        msg_y = h - 2
        stdscr.addstr(msg_y, 0, state.status_message[: w - 1])

    stdscr.refresh()


def wrap_text(text: str, width: int) -> list[str]:
    """Naive word wrap. Good enough for titles."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def show_help(stdscr: Any) -> None:
    stdscr.erase()
    text = [
        "Corpus Review — Help",
        "",
        "Navigation:",
        "  j or ↓        next entry",
        "  k or ↑        previous entry",
        "  g             jump to first",
        "  G             jump to last",
        "",
        "Decision:",
        "  a             approve",
        "  r             reject",
        "  s             skip (you'll come back later)",
        "  t             cycle through alternate cluster_matches",
        "",
        "View filters:",
        "  c             cycle cluster filter (all → cluster1 → cluster2 → all)",
        "  u             toggle 'show only unreviewed'",
        "",
        "Other:",
        "  o             open PDF URL in browser (xdg-open)",
        "  e             export approved entries to manifest.json",
        "  q             quit (auto-saves candidates with review state)",
        "  ?             this help",
        "",
        "Press any key to return.",
    ]
    for i, line in enumerate(text):
        stdscr.addstr(i, 2, line)
    stdscr.refresh()
    stdscr.getch()


def run_tui(stdscr: Any, state: State) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    state.refilter()

    while True:
        draw(stdscr, state)
        state.status_message = ""
        key = stdscr.getch()

        if key in (ord("q"), 27):  # q or Esc
            save_candidates(state.entries, state.candidates_path)
            return

        if key == ord("?"):
            show_help(stdscr)
            continue

        if key in (ord("j"), curses.KEY_DOWN):
            if state.cursor < len(state.filtered_indices) - 1:
                state.cursor += 1
        elif key in (ord("k"), curses.KEY_UP):
            if state.cursor > 0:
                state.cursor -= 1
        elif key == ord("g"):
            state.cursor = 0
        elif key == ord("G"):
            state.cursor = max(0, len(state.filtered_indices) - 1)

        elif key == ord("a"):
            if state.current_entry:
                state.current_entry.review = Review.APPROVED
                save_candidates(state.entries, state.candidates_path)
                advance(state)
        elif key == ord("r"):
            if state.current_entry:
                state.current_entry.review = Review.REJECTED
                save_candidates(state.entries, state.candidates_path)
                advance(state)
        elif key == ord("s"):
            if state.current_entry:
                state.current_entry.review = Review.SKIPPED
                save_candidates(state.entries, state.candidates_path)
                advance(state)

        elif key == ord("t"):
            e = state.current_entry
            if e and len(e.cluster_matches) > 1:
                # Rotate through cluster_matches
                current_cluster = e.cluster
                ordering = e.cluster_matches
                try:
                    next_idx = (ordering.index(current_cluster) + 1) % len(ordering)
                except ValueError:
                    next_idx = 0
                e.cluster_override = ordering[next_idx]
                save_candidates(state.entries, state.candidates_path)
                state.status_message = f"Cluster reassigned to: {e.cluster_override}"

        elif key == ord("o"):
            if state.current_entry:
                try:
                    subprocess.Popen(
                        ["xdg-open", state.current_entry.url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    state.status_message = f"Opening {state.current_entry.url}"
                except FileNotFoundError:
                    state.status_message = "xdg-open not available; URL not opened"

        elif key == ord("c"):
            # Cycle: None → cluster1 → cluster2 → ... → None
            clusters = state.cluster_list()
            if state.cluster_filter is None:
                state.cluster_filter = clusters[0]
            else:
                try:
                    idx = clusters.index(state.cluster_filter)
                    state.cluster_filter = clusters[idx + 1] if idx + 1 < len(clusters) else None
                except ValueError:
                    state.cluster_filter = None
            state.cursor = 0
            state.refilter()

        elif key == ord("u"):
            state.only_unreviewed = not state.only_unreviewed
            state.cursor = 0
            state.refilter()

        elif key == ord("e"):
            count = export_manifest(state.entries, state.manifest_path)
            state.status_message = f"Exported {count} approved entries to {state.manifest_path}"


def advance(state: State) -> None:
    """Move cursor forward after a decision, but stay in bounds."""
    if state.cursor < len(state.filtered_indices) - 1:
        state.cursor += 1


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", type=Path, default=CANDIDATES_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--export-only", action="store_true",
                        help="skip TUI; just export approved entries to manifest.json")
    args = parser.parse_args()

    try:
        entries = load_candidates(args.candidates)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"Loaded {len(entries)} candidates from {args.candidates}", file=sys.stderr)

    if args.export_only:
        count = export_manifest(entries, args.manifest)
        print(f"Exported {count} approved entries to {args.manifest}", file=sys.stderr)
        return 0

    state = State(
        entries=entries,
        candidates_path=args.candidates,
        manifest_path=args.manifest,
    )

    curses.wrapper(run_tui, state)

    # Auto-export on exit if user has approvals
    counts = state.counts()
    if counts["approved"] > 0:
        prompt = (
            f"\n{counts['approved']} approved, {counts['rejected']} rejected, "
            f"{counts['skipped']} skipped, {counts['unreviewed']} unreviewed.\n"
            f"Export approved entries to {args.manifest}? [y/N] "
        )
        try:
            response = input(prompt).strip().lower()
            if response == "y":
                count = export_manifest(entries, args.manifest)
                print(f"Exported {count} entries to {args.manifest}")
        except (EOFError, KeyboardInterrupt):
            print("\nSkipped export. Run with --export-only later.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
