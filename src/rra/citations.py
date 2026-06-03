"""Inline-citation parsing, shared by the API resolver and the eval runner.

There is exactly ONE parser (`parse_answer`) so the product surface (api.py) and
the measurement surface (evals/run.py) can never drift — the single
highest-probability regression vector for quote-faithfulness (ADR 0013; Day-7
plan §7-#5). It extracts, per inline citation, an `(guidance_id, chunk_index,
quoted_text | None)` triple and returns the prose with the `<q>…</q>` envelopes
stripped (what the user sees).

Design invariant — address-parse and quote-parse are DECOUPLED: the supporting
quote is an *optional* capture group, so a missing or malformed `<q>` yields
`quoted_text=None` but can NEVER drop the citation address. A quote problem
costs us a quote, never a citation (ADR 0013).
"""
from __future__ import annotations

import re

# One regex parses BOTH the citation address and its optional supporting quote.
#   group 1 = guidance_id   (any chars except ':' and ']', matching the API
#                            resolver and the critic regex byte-for-byte)
#   group 2 = chunk_index   (digits)
#   group 3 = quote         (optional; None when no <q> envelope follows)
# The bracket `[guid:idx]` is matched identically to the other two address
# parsers, so adding the quote envelope changes nothing about address parsing.
# `.*?` is NON-greedy and closes the quote at the FIRST `</q>`: with several
# cited claims this keeps each quote bound to its own bracket (a greedy match
# would swallow two quotes and the prose between them). A `</q>` occurring
# literally inside quoted content would truncate THAT quote to its prefix —
# benign: the citation address is untouched and FDA prose does not contain
# `</q>`. re.DOTALL lets a quote span PDF-embedded newlines.
_CITE_Q_RE = re.compile(r"\[([^:\]]+):(\d+)\](?:\s*<q>(.*?)</q>)?", re.DOTALL)

# Strip the `<q>…</q>` envelope (and any whitespace preceding it) from the prose
# returned to the user. The `[guid:idx]` marker is kept (unchanged behaviour).
_Q_ENVELOPE_RE = re.compile(r"\s*<q>.*?</q>", re.DOTALL)

# Defensive: remove any lone `<q>`/`</q>` tag left behind by malformed markup,
# so an unbalanced envelope can never leak a raw marker into the user answer.
_STRAY_Q_RE = re.compile(r"</?q>")


def parse_answer(draft: str) -> tuple[str, list[tuple[str, int, str | None]]]:
    """Parse inline citations and their optional supporting quotes from *draft*.

    Returns ``(clean_prose, triples)``:

    - ``clean_prose`` — *draft* with every ``<q>…</q>`` envelope removed (the
      ``[guid:idx]`` markers kept). This is the user-facing answer.
    - ``triples`` — ``[(guidance_id, chunk_index, quoted_text | None), …]`` in
      order of appearance, one per inline citation.

    ``quoted_text`` is ``None`` whenever the analyst emitted no usable quote:
    no ``<q>`` at all, an empty ``<q></q>``, or a whitespace-only ``<q>  </q>``.
    All three are the honest "no quote" signal and are counted as such
    downstream — never scored faithful-by-emptiness (ADR 0013; plan §7-#3).
    A present quote is returned with its envelope whitespace stripped; the
    span's internal characters are left verbatim.
    """
    triples: list[tuple[str, int, str | None]] = []
    for m in _CITE_Q_RE.finditer(draft):
        raw_quote = m.group(3)
        quote = raw_quote.strip() if raw_quote else ""
        triples.append((m.group(1), int(m.group(2)), quote or None))

    clean_prose = _STRAY_Q_RE.sub("", _Q_ENVELOPE_RE.sub("", draft))
    return clean_prose, triples
