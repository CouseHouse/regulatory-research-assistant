# Day 5 Design — MCP Server and `check_citation`

**Status:** Draft — for review before ADRs and implementation
**Date:** 2026-06-01

## Confirmed corpus schema (ground truth for this design)

`corpus.chunks` is the only table. Relevant columns:

| Column | Type | Notes |
|---|---|---|
| `guidance_id` | text | Document identifier |
| `guidance_title` | text | Human-readable title |
| `section` | text | **EMPTY** — 0/2726 rows populated |
| `chunk_index` | int | Stable key; `UNIQUE(guidance_id, chunk_index)` |
| `text` | text | Stored chunk text; retains PDF artifacts |
| `char_start` | int NOT NULL | Document-relative character offset of chunk start |
| `char_end` | int NOT NULL | Document-relative character offset of chunk end |
| `embedding` | vector(1024) | Pre-computed at ingest |
| `metadata` | jsonb | Populated at ingest; content varies |
| `created_at` | timestamptz | Ingest timestamp; NOT publication date |

`char_start`/`char_end` are document-relative — chunks from the same `guidance_id` ordered by `chunk_index` reassemble the source document.

---

## A. Tool Contracts

All four tools are implemented as plain Python functions in `src/rra/mcp_server/tools.py`. `src/rra/mcp_server/server.py` registers them as MCP handlers. Agent code imports and calls them directly in-process (see Section C).

---

### A.1 `search_corpus`

**Input/output models:**

```python
class SearchFilters(BaseModel):
    guidance_ids: list[str] | None = None

class SearchCorpusInput(BaseModel):
    query: str
    k: int = Field(default=5, ge=1, le=50)
    filters: SearchFilters | None = None

class SearchCorpusResult(BaseModel):
    passages: list[RetrievedPassage]  # from rra.schemas
```

**Signature:** `search_corpus(query: str, k: int = 5, filters: SearchFilters | None = None) -> SearchCorpusResult`

**Tool description (what an MCP client reads):**
> Retrieve the most relevant passages from the FDA guidance document corpus for a natural-language query. Returns passages ranked by semantic similarity and Voyage rerank score; each passage carries a `guidance_id` and `chunk_index` that form a stable citation address. Use this tool to gather source evidence before drafting or verifying a regulatory claim.

**Underlying implementation:** delegates to `rra.retrieval.search_corpus()` unchanged. The tool wrapper adds Pydantic input validation and a consistent error surface.

**Error behavior:** `ToolError(code="EMBEDDING_ERROR", retryable=True)` if the Voyage API call fails; `ToolError(code="DB_ERROR", retryable=True)` on Postgres failure.

---

### A.2 `fetch_guidance`

**Input/output models:**

```python
class FetchGuidanceInput(BaseModel):
    guidance_id: str

class FetchGuidanceResult(BaseModel):
    guidance_id: str
    guidance_title: str
    text: str        # raw reassembled document text; no cleaning applied
    chunk_count: int
```

**Signature:** `fetch_guidance(guidance_id: str) -> FetchGuidanceResult`

**Assembly:**
```sql
SELECT chunk_index, guidance_title, text
FROM corpus.chunks
WHERE guidance_id = $1
ORDER BY chunk_index
```
Join chunk texts with `"\n\n"`. `chunk_count` is the row count.

**Section parameter: dropped.** The `section` column is empty in 0/2726 rows; a section filter would silently return empty text for every call. Section-filtering is documented here as a Day 7 candidate once ingest populates the field.

**Text cleaning: not applied.** Stored chunk texts retain embedded PDF newlines and mid-sentence "Contains Nonbinding Recommendations" boilerplate headers (~74% of chunks). Read-time cleaning would mask the scale of the problem that Day 6 evals must measure. The correct fix is at ingest (Day 7 per `docs/plan/day07.md`). Applying cleaning here would hide the corpus quality issue and potentially inflate `citation_validity` before the root cause is addressed.

**Tool description:**
> Retrieve the full text of an FDA guidance document by its `guidance_id`, assembled from stored chunks in their original order. Text is returned as stored — per-chunk PDF artifacts (embedded newlines, boilerplate headers) are present and will be addressed in the Day 7 ingest fix. Use this tool when you need complete document context beyond the top retrieved passages.

**Error behavior:** `ToolError(code="NOT_FOUND", retryable=False)` if no chunks exist for the given `guidance_id`. `ToolError(code="DB_ERROR", retryable=True)` on connection failure.

---

### A.3 `check_citation`

See Section B for the full matching design. Summary:

**Input/output models:**

```python
class CheckCitationInput(BaseModel):
    claim: str
    guidance_id: str
    chunk_index: int
    quoted_text: str | None = None

class CitationCheckResult(BaseModel):
    verified: bool
    source_text: str                   # stored chunk text, returned unconditionally
    matched_doc_span: list[int] | None  # [char_start, char_end] document-level; None when span cannot be located
    similarity_score: float | None     # coverage ratio from SequenceMatcher; None on direct substring hit or no quoted_text
```

**Signature:** `check_citation(claim: str, guidance_id: str, chunk_index: int, quoted_text: str | None = None) -> CitationCheckResult`

**Tool description:**
> Verify that a citation `[guidance_id:chunk_index]` addresses a real corpus chunk, and when `quoted_text` is supplied, that the quote faithfully appears in that chunk using whitespace-normalized substring matching with a configurable fuzzy fallback. Returns the stored chunk text unconditionally so the caller can independently assess whether the passage supports the claim. This tool checks factual presence only — whether the passage supports the claim is the critic's judgment, not this tool's.

**Error behavior:** `CitationCheckResult(verified=False, source_text="", ...)` when the chunk does not exist — this is a valid citation-check result (the citation is simply wrong), not an infrastructure error. `ToolError(code="DB_ERROR", retryable=True)` only on connection failure.

---

### A.4 `list_recent_guidances`

**Input/output models:**

```python
class ListRecentGuidancesInput(BaseModel):
    since_date: str  # ISO 8601: "YYYY-MM-DD"

class GuidanceRecord(BaseModel):
    guidance_id: str
    guidance_title: str
    ingest_date: datetime

class ListRecentGuidancesResult(BaseModel):
    guidances: list[GuidanceRecord]
```

**Signature:** `list_recent_guidances(since_date: str) -> ListRecentGuidancesResult`

**Query:**
```sql
SELECT guidance_id, guidance_title, MIN(created_at) AS ingest_date
FROM corpus.chunks
GROUP BY guidance_id, guidance_title
HAVING MIN(created_at) >= $1
ORDER BY ingest_date DESC
```

**Date source note:** `created_at` on `corpus.chunks` is the ingest timestamp, not the FDA publication date. The `metadata` jsonb column may carry publication date in future but is not queried here. `ingest_date` is therefore a proxy — "recently ingested" does not imply "recently published." This is documented in the tool description; a publication-date field is a future-ingest candidate.

**Tool description:**
> List FDA guidance documents whose chunks were first ingested since a given date (ISO 8601: YYYY-MM-DD), returning `guidance_id` and title for each. Note: ingest date is used as a proxy for document currency; FDA publication date may differ and is not yet captured in corpus metadata. Use this as a building block for currency checks before fetching specific documents.

**Error behavior:** `ToolError(code="INVALID_INPUT", retryable=False)` if `since_date` is not a parseable ISO date. `ToolError(code="DB_ERROR", retryable=True)` on connection failure.

---

## B. `check_citation` — full design

### B.1 Signature (ADR-0006 aligned)

```
check_citation(claim, guidance_id, chunk_index, quoted_text?) → CitationCheckResult
```

`claim` is included for Langfuse trace context and forward-compatibility with an embedding-similarity path, but this design does **not** use it for verification. Verification is purely deterministic. Claim-to-passage support is the critic LLM's judgment. This keeps `citation_validity` measuring grounding, not the tool's semantic opinion.

### B.2 Resolution via UNIQUE key

```sql
SELECT text, char_start, char_end
FROM corpus.chunks
WHERE guidance_id = $1 AND chunk_index = $2
```

Due to `UNIQUE(guidance_id, chunk_index)` this returns 0 or 1 row. Zero rows means the citation key does not exist in the corpus: return `CitationCheckResult(verified=False, source_text="", matched_doc_span=None, similarity_score=None)`. This is a valid, clean result — the citation is wrong. It is NOT a `ToolError`.

### B.3 Matching strategy (when `quoted_text` is supplied)

#### When `quoted_text` is None

Return `CitationCheckResult(verified=True, source_text=chunk.text, matched_doc_span=None, similarity_score=None)`. This is the "key-only" check: the citation key exists in the corpus. No matching is attempted. The critic receives `source_text` and makes the support judgment.

#### Step 1 — Normalize both sides

```python
# pseudocode
def _normalize(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

norm_quoted = _normalize(quoted_text)
norm_chunk  = _normalize(chunk.text)
```

This addresses the two documented corpus problems: embedded PDF newlines within sentences, and mid-sentence boilerplate headers. After normalization, a model's quote should appear as a substring if it was honestly derived from the text.

#### Step 2 — Normalized substring check (primary)

```python
# pseudocode
if norm_quoted in norm_chunk:
    # Recover offsets in the STORED (non-normalized) chunk text via
    # a whitespace-flexible regex, so we can return accurate document-level spans.
    words = re.escape(norm_quoted).split(r'\ ')
    pattern = re.compile(r'\s+'.join(words))
    m = pattern.search(chunk.text)
    if m:
        doc_start = chunk.char_start + m.start()
        doc_end   = chunk.char_start + m.end()
        return CitationCheckResult(
            verified=True,
            source_text=chunk.text,
            matched_doc_span=[doc_start, doc_end],
            similarity_score=None,
        )
    # Substring found in normalized space but regex failed on stored text
    # (edge case: normalization changed boundaries). Verified but no span.
    return CitationCheckResult(
        verified=True, source_text=chunk.text,
        matched_doc_span=None, similarity_score=None,
    )
```

**Why the whitespace-flexible regex on stored text:** The normalized chunk's character positions do not map to the stored chunk's character positions (normalization collapses sequences of whitespace into a single space, shifting all subsequent offsets). The regex `r'\s+'.join(words)` finds the quoted words in the stored text with any whitespace between them, giving us `m.start()` and `m.end()` in the stored chunk's coordinate space. Adding `chunk.char_start` makes these document-relative — the precise span the ADR-0006 design anticipates. This is the concrete improvement over Day 4: `char_start`/`char_end` are NOT NULL and the data supports it; Day 4 had no mechanism to return character spans.

#### Step 3 — SequenceMatcher coverage-ratio fallback

When normalization alone is insufficient (more extreme whitespace variation, minor OCR artifacts):

```python
# pseudocode
from difflib import SequenceMatcher

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

**Why coverage ratio, not `SequenceMatcher.ratio()`:** `ratio()` computes `2M / (len(a) + len(b))`. A 50-character quote compared against a 2,000-character chunk yields a ratio of at most ~0.05 even on a perfect match, because the denominator is dominated by the chunk length. The coverage ratio (`longest_match.size / len(quoted)`) instead asks: "what fraction of the quoted text appears as a contiguous block in the chunk?" This is the correct question. A threshold of 0.85 means "at least 85% of the quote characters appear consecutively in the chunk."

When the coverage fallback fires, `matched_doc_span` is None — we know the quote is substantially present but cannot locate the exact offset without the regex path. This is honest and avoids returning a wrong span.

#### Step 4 — No match

```python
# pseudocode
return CitationCheckResult(
    verified=False,
    source_text=chunk.text,  # return chunk text even on failure for critic's reference
    matched_doc_span=None,
    similarity_score=coverage,  # transparency: how close was the quote?
)
```

`source_text` is returned even on `verified=False` so the critic can see what the chunk actually contains. `similarity_score` is returned for transparency — a score of 0.45 when τ=0.85 tells evals something different from a score of 0.10.

### B.4 Threshold τ

```python
# config.py addition
citation_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
```

**Default 0.85** is conservative: we want high confidence that the quote faithfully represents the source before marking it verified. Day 6 evals should measure the distribution of `similarity_score` across the corpus to calibrate. If the normalized substring check handles the majority of cases (likely), τ matters only at the margin. If difflib fires frequently, τ is load-bearing and calibration matters.

**τ is the "real vs. theater" lever.** Too low: `verified=True` is nearly meaningless (nearly any text will have 60% of its characters in a 512-token chunk). Too high: legitimate quotes with minor boilerplate interference are incorrectly rejected. The field is config-driven so Day 6 calibration does not require code changes.

### B.5 Deterministic verification only — confirmed

`check_citation` answers: "is this quote in the text?" It does NOT answer: "does this text support this claim?" The separation is correct and should be maintained. Reasons:

1. **eval integrity:** `citation_validity` (spec §6.2, ≥ 0.95 gate) is a deterministic scorer. If verification requires an LLM judgment, the gate measures LLM agreement, not grounding. The gate is only meaningful if verification is deterministic.
2. **cost:** One `check_citation` call per citation is a DB query. Adding an embedding-similarity computation doubles the cost per call; adding an LLM call makes it prohibitive for a critic with multiple citations.
3. **separation of concerns:** The critic LLM receives `source_text` and makes the support judgment with the full benefit of its reasoning capability. Embedding similarity in the tool is a weaker, cheaper proxy that would only introduce a second opinion the critic would then override anyway.

Verdict: this is the right design. Do not add claim-support judgment to `check_citation`.

---

## C. In-process transport

**Recommendation:** tool functions are plain Python called in-process by agents. The MCP server registers the same functions over stdio/HTTP for external clients. There is no subprocess-per-query.

**File structure:**

```
src/rra/mcp_server/
    __init__.py       (currently 0-byte stub)
    tools.py          (four tool functions; pure Python; importable standalone)
    server.py         (MCP server setup; registers tools.py functions as MCP handlers)
```

**Agent import pattern:**

```python
# researcher.py
from rra.mcp_server.tools import search_corpus

# critic.py
from rra.mcp_server.tools import check_citation
```

**MCP server pattern (server.py):**

```python
# pseudocode — using FastMCP or the Python MCP SDK
@mcp.tool()
def search_corpus(...): ...   # same underlying function; registered as MCP handler
```

**Flagged tension with day05.md:** The plan states "Critic agent updated to call check_citation via MCP, not directly." This correctly names the dependency (agent depends on the tool abstraction) but must NOT be implemented as spawning an MCP subprocess per critic call. Subprocess-per-query adds ~50–100ms IPC overhead, makes the critic node fragile on startup sequencing, and complicates testing. The correct interpretation: "via MCP" means "via the tool function defined for MCP consumption," called in-process. The MCP transport is an exposure layer for external clients (Claude Desktop), not the agent communication mechanism.

**No tension with ADR 0003.** ADR 0003 governs model calls (Anthropic SDK direct, no LangChain abstraction). Tool function calls are Python function calls, not model calls. ADR 0003 is orthogonal.

**Testing benefit:** Tests can call tool functions as plain Python without starting an MCP server. Protocol-layer integration tests (Claude Desktop compatibility) start the server separately. No test coupling to the MCP transport.

---

## D. Wiring

### D.1 researcher.py

**Current state (Day 4):**
- Line 24: `from rra.retrieval import search_corpus`
- Line 125: `passages = search_corpus(reformulated, k=settings.rerank_top_k)`

**Day 5 change:** Change the import on line 24 to `from rra.mcp_server.tools import search_corpus`. The call site on line 125 is unchanged. The tool function delegates to `rra.retrieval.search_corpus` internally; `passages` still resolves to `list[RetrievedPassage]` (unwrap from `SearchCorpusResult` inside the tool function or at the call site).

The existing Langfuse retriever span wrapping the call in researcher.py is unchanged. The tool function may emit its own internal span (not required for Day 5), but the outer span remains the researcher node's responsibility.

This is a shallow change — one import line, nothing else. The goal is to route through Pydantic validation and make the tool-layer dependency explicit.

### D.2 critic.py

**Current state (Day 4):** No `check_citation` call. The critic verifies citations entirely in-context — it checks whether each `[guid:idx]` inline citation matches a passage it was given, then uses the passage text to assess support.

**Day 5 change: pre-validate citations in Python before the LLM call.**

```python
# pseudocode — inserted before the client.messages.create() call in run_critic()

from rra.mcp_server.tools import check_citation
from rra.mcp_server.tools import CitationCheckResult, ToolError

# 1. Parse inline citations from draft
citation_pattern = re.compile(r'\[([^:\]]+):(\d+)\]')
inline_citations = list({
    (m.group(1), int(m.group(2)))
    for m in citation_pattern.finditer(draft)
})

# 2. Call check_citation in-process for each cited chunk
check_results: dict[str, CitationCheckResult | ToolError] = {}
for guidance_id, chunk_index in inline_citations:
    key = f"{guidance_id}:{chunk_index}"
    try:
        check_results[key] = check_citation(
            claim=query,          # full query as trace context
            guidance_id=guidance_id,
            chunk_index=chunk_index,
            quoted_text=None,     # quoted_text not extractable from draft at this layer
        )
    except Exception as exc:
        check_results[key] = ToolError(
            code="DB_ERROR", message=str(exc),
            tool="check_citation", retryable=True,
        )

# 3. Build <citation_checks> XML block and inject into user_content alongside <passages>
```

**Why `quoted_text=None` at this layer:** The draft contains `[guidance_id:chunk_index]` inline markers, not extracted prose quotes. Pulling quoted_text from surrounding draft sentences is non-trivial heuristic work that should not live in the critic node. The `Citation.quoted_text` field in `schemas.py` is resolved at the API layer from the analyst's structured output, not from the draft. For Day 5, the key-only check (does the chunk exist? what does it contain?) is the correct and achievable scope. Quoted_text verification from the critic's perspective is a Day 7 candidate.

**What the LLM critic receives (additions to `user_content`):**

```xml
<citation_checks>
  <check citation_key="fda-abc:4" verified="true" inconclusive="false">
    <source_text>...stored chunk text...</source_text>
  </check>
  <check citation_key="fda-xyz:11" verified="false" inconclusive="false">
    <source_text></source_text>
    <reason>chunk not found in corpus</reason>
  </check>
  <check citation_key="fda-qrs:7" verified="unknown" inconclusive="true">
    <reason>tool error: DB_ERROR (transient)</reason>
  </check>
</citation_checks>
```

The LLM critic prompt is updated to instruct: "For any citation marked `inconclusive=true` due to a tool error, treat the check as if it was not run — do not penalize the draft for that citation."

**ADR 0009 preservation:** The verdict *mechanism* — routing, `revision_count` increment, `cap_hit` computation, and `submit_verdict` schema — is unchanged. What changes is the critic's *evidence and instructions*: it now receives deterministic citation-check results and is told not to penalize inconclusive checks. This augments the inputs to the verdict; it does not leave the critic node untouched.

**Day 5 value over Day 4:** The LLM critic now receives the definitive chunk text from the DB for each citation — not just what was in its context window. It can catch:
- Citations to chunks that exist in the corpus but were not retrieved into the passage set (the analyst hallucinated a `chunk_index` it wasn't shown)
- `verified=False` on any chunk that genuinely does not exist in the corpus

**Langfuse:** Each `check_citation` call emits a child span under the critic span. This makes MCP tool calls visible in traces (Day 5 stop condition: "Langfuse trace shows MCP tool calls as nested spans under the critic agent").

---

## E. Error contract

```python
class ToolError(BaseModel):
    code: Literal["NOT_FOUND", "INVALID_INPUT", "DB_ERROR", "EMBEDDING_ERROR", "UNKNOWN"]
    message: str
    tool: str       # tool name, for log correlation
    retryable: bool # False for NOT_FOUND/INVALID_INPUT; True for DB_ERROR/EMBEDDING_ERROR
```

**Retryability semantics:**

| `code` | `retryable` | Meaning |
|---|---|---|
| `NOT_FOUND` | False | Permanent: the key does not exist, retrying won't help |
| `INVALID_INPUT` | False | Permanent: the caller sent malformed input |
| `DB_ERROR` | True | Transient: connection pool exhausted, timeout, etc. |
| `EMBEDDING_ERROR` | True | Transient: Voyage API failure |
| `UNKNOWN` | True | Conservative default: assume transient |

**Critic handling rule:**

| Result type | Critic action |
|---|---|
| `CitationCheckResult(verified=True)` | Citation key and source confirmed; LLM assesses support |
| `CitationCheckResult(verified=False)` | Citation genuinely wrong → evidence for `revise` note |
| `ToolError(retryable=False)` | Treat same as `verified=False` — key malformed or provably absent |
| `ToolError(retryable=True)` | **Inconclusive** — infrastructure failure; do NOT use as evidence for `revise`; inform LLM as inconclusive |

The key invariant: **a transient infrastructure failure must not cause the critic to issue a `revise` verdict it would not otherwise issue.** This is the distinction between "citation invalid" (clean signal: the source evidence is wrong) and "tool failed" (noise: we don't know). Blurring these would produce false `revise` verdicts that loop the graph unnecessarily, burning tokens and potentially reaching cap_hit on a draft that was actually fine.

**At the MCP server layer:** the server catches Python exceptions from tool functions and returns MCP protocol errors. Agent code calling in-process wraps in try/except and converts to `ToolError`. This way the error contract is consistent whether the caller is an in-process agent or an external MCP client.

---

## F. ADR candidates

### F.1 `check_citation` matching contract — ADR required before implementation

**Why an ADR:** The matching algorithm defines what "verified" means system-wide. Three downstream consumers depend on it:
1. The `citation_validity` scorer (spec §6.2, ≥ 0.95 gate) — if matching is too loose, the gate measures nothing
2. The critic's `revise` signal — if matching is too strict, legitimate quotes fail and the critic over-requests revision
3. Day 6 eval calibration — τ is the tuning lever; the ADR records what it controls and how to change it

This is not an implementation detail; it is a definition of correctness. The decision to use coverage ratio rather than `SequenceMatcher.ratio()`, to normalize before matching, and to return `source_text` unconditionally are each consequential choices that will be relitigated without a record. Write this ADR before touching `tools.py`.

**Key decisions to record:** the three-step algorithm (normalize → coverage ratio → SequenceMatcher), τ default and config location, "claim is for trace context only," "source_text returned unconditionally including on failure," and "NOT_FOUND is CitationCheckResult not ToolError."

### F.2 MCP in-process transport — ADR required before implementation

**Why an ADR:** The question "do agents call tools via MCP subprocess or via in-process function call?" is architectural and not answered obviously by day05.md. The plan text "call check_citation via MCP" could be (incorrectly) read as subprocess. Without a recorded decision, a future contributor may refactor to subprocess-per-query and introduce the latency and fragility problems.

**Key decisions to record:** tools.py as importable Python; server.py as thin MCP wrapper; agents import from tools.py directly; no subprocess-per-query; testing without MCP server startup.

### F.3 Tool error retryability — fold into F.1 or F.2

The `retryable` field in `ToolError` and the critic's inconclusive-handling rule are narrow but load-bearing: they determine whether infrastructure failures cause spurious revision loops. This can be covered as a consequence in the matching ADR (F.1) or the transport ADR (F.2). Either is fine; it should not be omitted.

**Not ADR candidates:** `fetch_guidance` raw-text return (consequence of the Day 7 ingest decision, already documented in `docs/plan/day07.md`). `list_recent_guidances` ingest-date proxy (minor; documented inline in the tool description).

---

## Resolved decisions

**1. Critic wiring — Python-layer pre-validation (decided).** `check_citation` is called deterministically in Python before the LLM call; results are injected as `<citation_checks>` context. The alternative (LLM-callable tool alongside `submit_verdict`) was rejected: an LLM-callable check would make verification nondeterministic across runs, poisoning the Day 6 `citation_validity` gate. Deterministic pre-validation is what makes that gate meaningful.

**2. `similarity_score` on `verified=False` — keep it (decided).** Day 6 τ-calibration requires seeing the coverage-ratio distribution of citations that *failed* the threshold. A `verified=False` with score 0.82 versus 0.08 is the calibration signal that determines whether τ=0.85 is too strict. The nullable-field cost is trivial — `similarity_score` is already nullable for the substring-hit case.

**3. `quoted_text` extraction — deferred to Day 7 (decided, and the reason is structural, not a choice).** Confirmed via code inspection: `quoted_text` is resolved by `_resolve_citations()` at the API/server layer *after* the graph runs. `schemas.py` documents this: "char_start, char_end, and quoted_text are resolved server-side." The analyst emits only inline `[guid:idx]` markers into the draft; structured `Citation` objects with `quoted_text` are built downstream of the critic. This is not a scheduling preference — there is no `quoted_text` available at the point in the graph where the critic runs.

**Consequence to state plainly:** In Day 5, `check_citation` runs in **key-existence mode only** (`quoted_text=None` for every call). The critic verifies that each cited `guidance_id:chunk_index` resolves to a real chunk (catches hallucinated indices) and receives authoritative chunk text. The normalized-matching engine (sections B.3/B.4) is **built and unit-tested** but **not exercised by the live Day 5 pipeline**, because no `quoted_text` exists at the critic's point in the graph.

**Architecture question for Day 7 (named here, not solved):** Citation verification currently cannot use the matching engine because `_resolve_citations()` — which produces `quoted_text` — runs after the critic. To make normalized matching run in-pipeline, one of the following must change: (a) resolution moves before the critic, so `quoted_text` is available when `check_citation` is called, or (b) a verification pass runs after resolution, outside the LangGraph critic node. This is a Day 7 design question; the answer has implications for the graph shape (ADR 0008) and the critic's role.
