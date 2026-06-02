# 0010 — `check_citation` matching contract

**Status:** Active
**Date:** 2026-06-01
**Owner:** Kyle Couse

## Context

`check_citation` is the system's definition of "verified." Three consumers depend on that definition being precise and stable:

1. **`citation_validity` gate** (spec §6.2, ≥ 0.95 threshold) — if matching is too loose, the gate measures nothing; if too strict, legitimate quotes fail and the number is misleading in the other direction.
2. **Critic's `revise` signal** — a `verified=False` result is evidence the critic uses to decide whether to request revision. A wrong matching algorithm produces spurious `revise` verdicts, burning tokens and potentially driving the loop to `cap_hit` on a draft that was correct.
3. **Day 6 τ-calibration** — the threshold τ is the tuning lever between those failure modes; the ADR records what it controls and where to change it.

The corpus has two documented quality issues that any matching algorithm must account for: embedded PDF newlines within sentences (approximately 74% of chunks), and mid-sentence "Contains Nonbinding Recommendations" boilerplate headers inserted at chunk boundaries. These mean a model quoting source text verbatim may produce a quote that does not appear as a literal substring of the stored chunk text.

Without a recorded decision, the choices made here — coverage ratio rather than `SequenceMatcher.ratio()`, normalization before matching, returning `source_text` unconditionally, and treating NOT_FOUND as a clean result not an error — will be relitigated during every future eval cycle.

## Decision

### Signature

```
check_citation(claim, guidance_id, chunk_index, quoted_text?) → CitationCheckResult
```

- `claim` is present for Langfuse trace context and forward-compatibility only. It is **NOT used in verification.** Verification is deterministic. Claim-to-passage support is the critic LLM's judgment, not this tool's.
- `chunk_index` is the ADR-0006 stable citation address. Resolution is via `UNIQUE(guidance_id, chunk_index)` — the query returns 0 or 1 row with no ambiguity.
- `quoted_text` is optional. When absent, the tool operates in key-existence mode. When present, the three-step matching algorithm runs.

### Return type

```python
class CitationCheckResult(BaseModel):
    verified: bool
    source_text: str           # stored chunk text; returned UNCONDITIONALLY
    matched_doc_span: list[int] | None  # [char_start, char_end] document-relative; None when span cannot be pinpointed
    similarity_score: float | None      # coverage ratio; None on direct substring hit or when quoted_text is None
```

`source_text` is returned unconditionally — including on `verified=False` and on NOT_FOUND (as an empty string). On `verified=False`, the critic needs the actual chunk text to assess what the source says. On NOT_FOUND, the empty string is an honest signal. Returning `source_text` only on success would withhold the signal the critic needs to write a useful revision note.

`similarity_score` is returned on `verified=False` as well as on success via the fallback path. A score of 0.82 when τ=0.85 is a different calibration signal from a score of 0.08; discarding it on failure would make Day 6 τ-calibration blind to near-miss quotes.

### Key-existence mode (`quoted_text` is None)

Return `CitationCheckResult(verified=True, source_text=chunk.text, matched_doc_span=None, similarity_score=None)` when the chunk key resolves. No matching is attempted. This is the operative mode for Day 5: `quoted_text` is unavailable at the critic's point in the graph (see Consequences).

### Three-step matching algorithm (`quoted_text` supplied)

**Step 1 — Whitespace normalization.**

```python
def _normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

norm_quoted = _normalize(quoted_text)
norm_chunk  = _normalize(chunk.text)
```

Normalization eliminates PDF-embedded newlines and collapses all whitespace to single spaces. After normalization, a model quote honestly derived from the source text should appear as a substring.

**Step 2 — Normalized substring check (primary path).**

Test `norm_quoted in norm_chunk`. On success, recover document-level character offsets by running a whitespace-flexible regex against the **stored** (non-normalized) chunk text:

```python
words = re.escape(norm_quoted).split(r'\ ')
pattern = re.compile(r'\s+'.join(words))
m = pattern.search(chunk.text)
if m:
    doc_start = chunk.char_start + m.start()
    doc_end   = chunk.char_start + m.end()
```

The regex is necessary because normalized character positions do not map to stored-text positions — normalization collapses whitespace sequences and shifts all subsequent offsets. The regex `r'\s+'.join(words)` locates the quote words in the stored text with any whitespace between them, yielding `m.start()` and `m.end()` in the stored chunk's coordinate space. Adding `chunk.char_start` makes these document-relative. This is the span the ADR-0006 design anticipates, and it is only possible because `char_start`/`char_end` are NOT NULL in the confirmed corpus schema.

If the regex does not match (edge case where normalization changed word boundaries), return `verified=True, matched_doc_span=None` — the quote is present but cannot be pinpointed.

**Step 3 — SequenceMatcher coverage-ratio fallback.**

When Step 2 does not find the substring — more extreme whitespace variation, minor OCR artifacts:

```python
matcher = SequenceMatcher(None, norm_quoted, norm_chunk, autojunk=False)
longest = matcher.find_longest_match(0, len(norm_quoted), 0, len(norm_chunk))
coverage = longest.size / len(norm_quoted) if norm_quoted else 0.0

if coverage >= settings.citation_match_threshold:
    return CitationCheckResult(
        verified=True,
        source_text=chunk.text,
        matched_doc_span=None,  # cannot pinpoint from longest-match alone
        similarity_score=coverage,
    )
```

The metric is the **coverage ratio**: `longest_match.size / len(norm_quoted)`. This asks "what fraction of the quoted text appears as a contiguous block in the chunk?" A threshold of 0.85 means at least 85% of the quote's characters are present consecutively in the chunk.

`SequenceMatcher.ratio()` is explicitly rejected as the metric. `ratio()` computes `2M / (len(a) + len(b))`; a 50-character quote against a 2,000-character chunk yields at most ~0.05 even on a perfect match, because the denominator is dominated by the chunk length. The coverage ratio's denominator is the quote length, which is the correct normalization for the question being asked.

When the fallback fires, `matched_doc_span` is None — we know the quote is substantially present but cannot locate the exact offset without the regex path. This is honest.

**Step 4 — No match.**

```python
return CitationCheckResult(
    verified=False,
    source_text=chunk.text,
    matched_doc_span=None,
    similarity_score=coverage,
)
```

### Threshold τ

```python
# src/rra/config.py
citation_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
```

τ = 0.85 is conservative. It is config-driven so Day 6 eval calibration does not require a code change. Day 6 should measure the distribution of `similarity_score` values to determine whether 0.85 is load-bearing or incidental (if the substring check handles the vast majority of cases, τ matters only at the margin).

### NOT_FOUND is a clean result, not an error

When `SELECT ... WHERE guidance_id = $1 AND chunk_index = $2` returns zero rows, return:

```python
CitationCheckResult(verified=False, source_text="", matched_doc_span=None, similarity_score=None)
```

This is a valid citation-check result — the citation is simply wrong. It is NOT a `ToolError`. Raising a `ToolError` on NOT_FOUND would conflate "citation key does not exist in the corpus" with "infrastructure failure," causing the critic to treat a definitively wrong citation as inconclusive.

### ToolError retryability and critic handling

```python
class ToolError(BaseModel):
    code: Literal["NOT_FOUND", "INVALID_INPUT", "DB_ERROR", "EMBEDDING_ERROR", "UNKNOWN"]
    message: str
    tool: str
    retryable: bool
```

`NOT_FOUND` and `INVALID_INPUT` have `retryable=False`. `DB_ERROR`, `EMBEDDING_ERROR`, and `UNKNOWN` have `retryable=True`.

The critic's invariant: **a transient infrastructure failure must not cause the critic to issue a `revise` verdict it would not otherwise issue.** Specifically:

| Result | Critic action |
|---|---|
| `CitationCheckResult(verified=True)` | Key and source confirmed; LLM assesses support |
| `CitationCheckResult(verified=False)` | Definitive evidence citation is wrong → evidence for `revise` note |
| `ToolError(retryable=False)` | Treat same as `verified=False` — key malformed or provably absent |
| `ToolError(retryable=True)` | **Inconclusive** — infrastructure failure; do NOT treat as evidence for `revise`; surface as inconclusive in `<citation_checks>` context |

Blurring the retryable/non-retryable distinction produces false `revise` verdicts that drive unnecessary revision loops, burning tokens and potentially reaching `cap_hit` on a draft that was correct. This invariant is the reason `retryable` exists as an explicit field on `ToolError` rather than being inferred at call sites.

## Alternatives considered

- **`SequenceMatcher.ratio()` as the similarity metric** — Rejected. For a short quote against a full chunk, `ratio()` computes `2M / (len(quote) + len(chunk))`; the denominator is dominated by chunk length. A 50-character perfect match in a 2,000-character chunk yields ratio ≈ 0.05, well below any useful threshold. The metric is structurally wrong for this use case.

- **Embedding-similarity verification** — Rejected. Embedding similarity introduces an LLM-equivalent call (Voyage API) per citation, increasing cost and making verification non-deterministic across embedding-model versions. The `citation_validity` gate (spec §6.2) is meaningful only if verification is deterministic; embedding similarity would make the gate measure semantic proximity rather than textual grounding.

- **Claim-based verification (does the passage support the claim?)** — Rejected for this tool. The critic LLM receives `source_text` and makes that judgment with full reasoning capability. Embedding a weaker judgment inside the tool would add cost and produce a second opinion the critic would then override. The separation is correct: `check_citation` answers "is this quote in the text?" and the critic answers "does this text support this claim?"

- **Raising `ToolError(NOT_FOUND)` when the chunk key is absent** — Rejected. NOT_FOUND is a definitive, clean signal that the citation address does not exist in the corpus. Encoding it as an error conflates citation wrongness with infrastructure failure and forces callers to parse error codes to determine whether a citation is valid. A `CitationCheckResult(verified=False)` is semantically correct.

- **Returning `source_text=None` on `verified=False`** — Rejected. The critic needs the actual chunk text to write a useful revision note when a citation is wrong. Withholding it forces a second DB round-trip (or leaves the critic blind to what the source actually says). The cost of always returning `source_text` is one extra field in the response; the benefit is a better-informed critic.

## Consequences

**Enables:**
- Deterministic verification: `citation_validity` (spec §6.2) measures textual grounding, not LLM agreement. The gate is meaningful.
- The critic receives authoritative chunk text for every citation — including citations to chunks that exist in the corpus but were not retrieved into the passage set (hallucinated `chunk_index` values the analyst was not shown). This is a concrete improvement over Day 4, where the critic verified only against its context window.
- τ is tunable from Day 6 evals without a code change.
- `similarity_score` on `verified=False` gives Day 6 τ-calibration the distribution it needs to determine whether the default is too strict or too loose.

**Activation timing — key constraint:**
The matching engine (Steps 1–3 above) is **built and unit-tested in Day 5** but is **not exercised by the live pipeline in Day 5**. `quoted_text` is resolved by `_resolve_citations()` at the API layer, after the LangGraph graph completes. The analyst emits only inline `[guidance_id:chunk_index]` markers into the draft; structured `Citation` objects with `quoted_text` are built downstream of the critic. There is no `quoted_text` available at the point in the graph where the critic calls `check_citation`. In Day 5, every call uses key-existence mode (`quoted_text=None`).

**Day 6 baseline interpretation:**
The Day 6 `citation_validity` first run measures key-existence only — whether cited `guidance_id:chunk_index` pairs resolve to real corpus chunks. Quote-faithfulness measurement (the full three-step algorithm) follows the Day 7 activation, when the resolution-vs-verification ordering is resolved. Day 6 baseline numbers should be labeled accordingly; comparing them against post-Day-7 numbers without this note would suggest a false improvement.

**Day 7 architecture question (named, not solved):**
To activate normalized matching in-pipeline, one of the following must change: (a) `_resolve_citations()` moves before the critic node, making `quoted_text` available when `check_citation` is called, or (b) a verification pass runs after resolution, outside the LangGraph critic node. The answer has implications for the graph shape (ADR 0008) and the critic's role. This is a Day 7 design question.

**Constrains:**
- The SequenceMatcher fallback cannot return `matched_doc_span` — the longest-match position in normalized space does not map directly to stored-text offsets. Citations verified via the fallback path will have `matched_doc_span=None`. This is an honest limitation; returning a wrong span would be worse.
- τ = 0.85 is an initial default, not a calibrated value. If corpus artifacts are more severe than anticipated, legitimate quotes may fail Step 2 and score below 0.85 on Step 3, producing false `verified=False` results. Day 6 calibration is the intended correction path.

## Related

- ADR 0006 (citation span addressing) — `guidance_id:chunk_index` as the stable address this tool resolves
- ADR 0008 (LangGraph state shape) — `citations` field this tool's results inform
- ADR 0009 (critic-loop policy) — `revise` verdict the critic issues based on this tool's output
- ADR 0011 (MCP in-process transport) — how agents invoke this tool
- spec.md §6.2 (citation_validity gate, ≥ 0.95 threshold)
- docs/plan/day5-design.md sections B, E, F.1, F.3
