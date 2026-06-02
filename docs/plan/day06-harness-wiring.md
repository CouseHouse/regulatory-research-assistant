# Day 6 — Eval Harness Wiring Plan

**Status:** For human review before implementation.
**Scope:** Wire `run_agent`, rewrite `CitationValidityScorer`, wire both judges,
harden the gate, update `write_report`, add CI workflow. No golden-set changes.
**Stop condition:** `uv run python -m rra.evals.run` runs end-to-end; draft PR
fails CI on the planted bogus case.

---

## 0. Ground-truth orientation (verified from source, 2026-06-02)

### GraphState fields — `src/rra/graph.py:49–79`

| key | type | writer |
|-----|------|--------|
| `query` | `str` | api.py (initial) |
| `product_context` | `str` | api.py (initial) |
| `session_id` | `str` | api.py (initial) |
| `trace_id` | `str \| None` | api.py (initial); graph passes through |
| `sub_questions` | `list[str]` | planner |
| `outline` | `str` | planner |
| `passages` | `list[RetrievedPassage]` | researcher |
| `draft` | `str` | analyst |
| `verdict` | `Literal["approve","revise","escalate"] \| None` | critic |
| `critic_notes` | `list[CriticNote]` | critic |
| `revision_count` | `int` | critic |
| `cap_hit` | `bool` | critic |
| `token_usage` | `dict[str, int]` | every agent (merged) |

`run_graph(initial_state) -> dict[str, Any]` — returns the final GraphState as
a plain dict (graph.py:169).

**The Day-1 TODO guesses were wrong:**
- `result["final_answer"]` → **`result["draft"]`** (graph.py:70)
- `result["citations"]` → **no such key**. GraphState has no `citations` field.
  Citations are derived post-hoc by `_parse_citation_pairs(draft)` in api.py.
- `result.get("langfuse_trace_id")` → **`result.get("trace_id")`** (graph.py:60).
  The trace_id is set by api.py before graph entry and flows through as-is.

### `_parse_citation_pairs` — `src/rra/api.py:120–132`

```python
def _parse_citation_pairs(answer: str) -> list[tuple[str, int]]:
```

- Importable directly from `rra.api`.
- Returns `list[tuple[guidance_id: str, chunk_index: int]]`.
- Handles grouped brackets `[g:i, g:i]` as well as singletons.
- This is the **pre-resolution tap**: it reads `draft` before any DB lookup,
  so it captures exactly what the analyst emitted, including hallucinated keys.

### `check_citation` — `src/rra/mcp_server/tools.py:196–320`

```python
def check_citation(
    claim: str,
    guidance_id: str,
    chunk_index: int,
    quoted_text: str | None = None,
) -> CitationCheckResult:
```

- `quoted_text=None` → **key-existence mode**: queries `corpus.chunks WHERE
  guidance_id=… AND chunk_index=…`; returns `CitationCheckResult(verified=True)`
  if row exists, `CitationCheckResult(verified=False)` if `row is None`.
- Only DB connection failures raise `ToolError`; missing key → clean
  `verified=False` (tools.py:234–241).
- `CitationCheckResult.verified: bool` is the only field the eval reads.
- `claim` is trace context only — pass any non-empty string (e.g., `"eval"`).

### Confirmed bugs (scorers.py + run.py)

| location | bug | ADR ref |
|----------|-----|---------|
| `scorers.py:70–72` | zero-citation → `ScoreResult(score=0.0, passed=False)` | D1 |
| `run.py:191` (comment) | judge uses `os.environ["ANALYST_MODEL"]` | D3 |

### `config.py` model pattern

`Settings` (config.py:40) uses Pydantic BaseSettings. Field name
`foo_bar` maps to env var `FOO_BAR` automatically (case_sensitive=False,
extra="ignore"). Existing model fields:
- `judge_model: str = "claude-haiku-4-5"` (line 107)
- `analyst_model: str = "claude-sonnet-4-6"` (line 104)

**Gap:** `position_judge_model` does not exist in `config.py` yet. Must be
added before `PositionQualityScorer` can follow house pattern.

### CRITIC_FORCE_VERDICT — **`<UNSET>`** ✅

Shell is clean. No accidental force-verdict will pollute the eval run.

---

## A. `run_agent` — `src/rra/evals/run.py:51–68`

Replace the stub body. No new imports except from existing modules.

**GraphState keys to read:**

```
result["draft"]       → AgentResponse.answer_text
result["passages"]    → AgentResponse.retrieved_passages  (list[RetrievedPassage])
result.get("trace_id") → AgentResponse.raw_trace_id
```

**Citation extraction** — call `_parse_citation_pairs(result["draft"])` (imported
from `rra.api`). This returns `list[tuple[str, int]]`. Convert to the dicts
`AgentResponse.citations` expects: `[{"guidance_id": g, "chunk_index": i} ...]`.

**`retrieved_passages` format** — `RetrievedPassage` is a Pydantic model
(`rra.schemas`). Scorers consume it as `dict`; call `.model_dump()` on each, or
adjust `AgentResponse.retrieved_passages` type to `list[RetrievedPassage]` and
update scorers to use attribute access. **Recommendation:** use `.model_dump()`
at the boundary; keep scorers as dicts for loose coupling.

**`AgentResponse` docstring / type annotation** — the existing comment says
`citations` holds `{"guidance_id", "span", "char_start", "char_end"}`. This is
stale; update to `{"guidance_id": str, "chunk_index": int}` to match what
`_parse_citation_pairs` returns.

**Initial state to pass to `run_graph`:**
```python
{
    "query": case.query,
    "product_context": case.product_context,
    "session_id": str(uuid.uuid4()),   # unique per eval case
}
```
`trace_id` defaults to `None` inside `run_graph` — no need to pass it.

**Langfuse in eval context** — `settings.langfuse_enabled` may be True in local
runs; that is fine (traces appear in Langfuse). In CI it will be False
(no keys in CI env). No special handling needed — `run_graph` already guards.

---

## B. `CitationValidityScorer` rewrite — `src/rra/evals/scorers.py:57–92`

### B1. Rename `make_corpus_lookup` → `make_resolver` (`run.py:73`)

The corpus-lookup pattern (load full text, substring-match a "span") is
replaced by key-existence resolution. The new factory:

```python
def make_resolver():
    """Returns resolves(guidance_id, chunk_index) -> bool.
    Calls check_citation in key-existence mode (quoted_text=None).
    Raises ToolError on DB failure; caller should let it propagate
    (a DB failure means the eval cannot run, not that a citation is invalid).
    """
    from rra.mcp_server.tools import check_citation
    def resolves(guidance_id: str, chunk_index: int) -> bool:
        result = check_citation("eval", guidance_id, chunk_index, quoted_text=None)
        return result.verified
    return resolves
```

### B2. Rewrite `CitationValidityScorer`

```python
class CitationValidityScorer:
    name = "citation_validity"
    threshold = 0.95
    gate = True

    def __init__(self, resolves):
        # resolves(guidance_id, chunk_index) -> bool
        self._resolves = resolves
```

**`score()` logic:**

1. If `not response.citations`: return the **N/A sentinel** (see B3), never 0.0.
2. For each `c` in `response.citations` (each is `{"guidance_id": str, "chunk_index": int}`):
   call `self._resolves(c["guidance_id"], c["chunk_index"])`.
3. `score = valid / total`. Return `ScoreResult(passed = score >= self.threshold, ...)`.

**No `corpus_lookup(guidance_id) → text` anywhere.** The old substring-match
approach (`c["span"] in text`) is completely removed.

### B3. N/A sentinel in `ScoreResult`

ADR 0012 D1: zero-citation cases must be **excluded from the mean**, never 0.0.

Options:
- (a) Add `score: float | None` — `None` means N/A. Runner checks `v is not None` before averaging.
- (b) Add a separate `excluded: bool` field.

**Recommendation (a):** change `score: float` to `score: float | None` in the
`ScoreResult` dataclass. The runner's aggregation already filters with
`[v for v in vals if v is not None]`. The `passed` field should also be
set to a sentinel — safest is `passed=False` so a zero-citation case never
counts as a gate pass, but the runner must skip it in the gate check too.

Actually: zero-citation cases should be neither pass nor fail for the gate — they
are excluded. The cleanest rule: in gate evaluation, a case is included only
when `result.score is not None`. In the mean, same filter.

### B4. Runner aggregation update (`run.py`)

After B3, the aggregation in `write_report` and `all_passed` must be updated:

```python
# Mean excludes None scores
vals = [s.score for s in r.scores if s.scorer == scorer.name and s.score is not None]
mean = sum(vals) / len(vals) if vals else None  # None if all N/A

# Gate: only non-None scores count; but see D. below for the full gate
gate_passed = all(
    s.passed
    for r in runs if r.response is not None
    for s in r.scores
    if s.scorer == scorer_name and s.score is not None and scorer.gate
)
```

The zero-citation count line: compute once and embed in report header:
```
N={zero_citation_count} of {total} answers had zero citations (excluded from citation_validity mean).
```

---

## C. The two judges

Both scorers currently `raise NotImplementedError`. Wire them identically:
**strict `json.loads` → one retry → case ERROR on second failure**.

### C1. Judge client

The Anthropic SDK is already a dependency. Create a thin wrapper (or inline it)
that makes one call and returns the text:

```python
import anthropic
_client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

def _judge_call(model: str, prompt: str) -> str:
    msg = _client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
```

This lives in a new `src/rra/evals/judge.py` (or inline in `scorers.py` if you
prefer fewer files — **judge.py is the recommendation** for testability).

### C2. JSON parse + retry contract

Both scorers apply the same pattern:

```python
for attempt in range(2):
    raw = _judge_call(self._model, prompt)
    try:
        parsed = json.loads(raw)
        break
    except json.JSONDecodeError:
        if attempt == 1:
            return ScoreResult(
                self.name, None, False,
                {"reason": "judge returned non-JSON after 2 attempts", "raw": raw[:200]}
            )
# use parsed
```

Second failure → `ScoreResult(score=None, passed=False, detail={"reason": "ERROR: ..."})`
The runner counts these as errors in the report header and the gate check (see D).

### C3. `KeyFactCoverageScorer` — model env var

Model comes from `settings.judge_model` (already `"claude-haiku-4-5"`).
In `main()`, construct as:
```python
KeyFactCoverageScorer(judge_client=_judge_call, model=settings.judge_model)
```

Score: `sum(present) / len(facts)`. No normalization needed (already 0–1).

### C4. `PositionQualityScorer` — model env var

**ADR 0012 D3:** uses `POSITION_JUDGE_MODEL`, not `ANALYST_MODEL`.

Step 1 — add to `config.py` following house pattern:
```python
position_judge_model: str = "claude-sonnet-4-6"
```
(env var: `POSITION_JUDGE_MODEL`)

In `main()`:
```python
PositionQualityScorer(judge_client=_judge_call, model=settings.position_judge_model)
```

Score: `parsed["score"]` is 1–5 integer. Normalize to 0–1 for reporting
(`score / 5.0`), but compare against threshold on the raw scale (`threshold=4.0`).
**Clarification needed:** the existing `threshold = 4.0` in the class is on the
1–5 scale. The `passed` check should use the raw score, not the normalized one.
Set `score` field in `ScoreResult` to the raw 1–5 value; document the scale in
the report.

---

## D. Gate hardening — `run.py`

### D1. The `all([])`-is-True footgun

Current (run.py:105–112): if `scorers == []` (the Day-1 placeholder), the
generator is empty and `all([])` is `True` — the harness reports green when it
has run no scorers. Must be eliminated.

Replacement logic:

```python
gate_scorer_names = {s.name for s in scorers if s.gate}
gate_runs = [
    r for r in runs
    if r.response is not None
    for s in r.scores
    if s.scorer in gate_scorer_names and s.score is not None
]
scored_count = sum(1 for r in runs if r.response is not None)
error_count = sum(1 for r in runs if r.error is not None)

if error_count > 0:
    all_passed = False
elif scored_count < len(cases):
    all_passed = False   # some cases didn't even get a response
elif not gate_runs:
    all_passed = False   # no gate scorer produced a result — broken harness
else:
    all_passed = all(s.passed for r in runs for s in r.scores
                     if s.scorer in gate_scorer_names and s.score is not None)
```

This kills three failure modes:
- Empty gate run → False (not True)
- Any error → False
- Fewer responses than cases → False

### D2. `enforce_gates` path

When `enforce_gates=False` (`--no-gate` CLI flag), skip the gate entirely and
return `True`. Logic is unchanged — just don't run the check above.

---

## E. `write_report` additions — `src/rra/evals/run.py:117–169`

### E1. Locked baseline label in header

Every report must declare the measurement mode. Add to the header section:

```markdown
Baseline label: **key-existence only** (ADR 0010 Day 6 — chunk address
resolution, not quote faithfulness). Do not compare Day 6 numbers to Day 7+
without re-reading ADR 0010 and ADR 0012 P2.
```

This appears as a static string, not a runtime value. No env var needed.

### E2. Zero-citation count line

Compute before the aggregate table:
```python
zero_citation_count = sum(
    1 for r in runs
    if r.response is not None
    and not r.response.citations
)
```

Emit in the report header (before the aggregate table):
```
**Zero-citation answers:** N={zero_citation_count} of {len(runs)} (excluded from citation_validity mean per ADR 0012 D1).
```

### E3. Error count line (complements D)

```
**Errors:** {error_count} of {len(runs)} cases failed to produce a response.
```

Already present as part of the `Cases: N  Errors: N` line (run.py:126) — confirm
it matches the hardened gate's `error_count` rather than re-computing.

---

## F. CI — `.github/workflows/evals.yml`

### F1. Job structure

```yaml
name: eval-gate
on: [pull_request]
jobs:
  citation-gate:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: rra
          POSTGRES_PASSWORD: rra_dev_password
          POSTGRES_DB: rra
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run python -m rra.db.migrate   # or however migrations are applied
      - run: uv run python tests/evals/load_ci_fixture.py  # loads key fixture
      - run: uv run python -m rra.evals.run --fixture ci --no-llm-judges
        env:
          DATABASE_URL: postgresql://rra:rra_dev_password@localhost:5432/rra
```

**No `ANTHROPIC_API_KEY` needed.** `CitationValidityScorer` calls `check_citation`
which hits Postgres only. The `--no-llm-judges` flag (or equivalent) skips
`KeyFactCoverageScorer` and `PositionQualityScorer`. Implementation: when the
flag is set, construct only `[CitationValidityScorer(make_resolver())]`.

### F2. CI-only fixture (ADR 0012 D4)

**New file: `evals/fixtures/ci_key_fixture.jsonl`** — NOT in `golden.jsonl`.

Format: one JSON object per line, each a `(guidance_id, chunk_index)` pair with
a synthetic query. Include one deliberately invalid case (known-nonexistent
`chunk_index`) to prove the gate bites:

```jsonl
{"id": "ci-valid-001", "query": "synthetic", "product_context": "", "expected_facts": [], "expected_guidance_ids": [], "difficulty": "easy", "_ci_citations": [["184856", 1], ["184856", 2]]}
{"id": "ci-bogus-001", "query": "synthetic bogus", "product_context": "", "expected_facts": [], "expected_guidance_ids": [], "difficulty": "easy", "_ci_citations": [["184856", 99999]]}
```

The runner, when given `--fixture ci`, reads `_ci_citations` directly and
short-circuits `run_agent` — it builds an `AgentResponse` with those citation
pairs pre-loaded, skipping the graph entirely. This means **zero API cost and
zero embeddings** for CI.

**`ci-bogus-001` explanation:** chunk_index 99999 does not exist in 184856
(confirmed: 184856 has 97 chunks). `check_citation("eval", "184856", 99999)`
will return `verified=False`. `CitationValidityScorer` will score it 0.0,
`passed=False`, which fails the gate.

### F3. `--fixture` CLI flag

Add to `main()`:
```python
parser.add_argument("--fixture", choices=["golden", "ci"], default="golden")
parser.add_argument("--no-llm-judges", action="store_true")
```

When `--fixture ci`: load from `evals/fixtures/ci_key_fixture.jsonl`, use
pre-loaded citations (no graph invocation). When `--no-llm-judges`: omit
`KeyFactCoverageScorer` and `PositionQualityScorer` from the scorers list.

---

## Drift summary

Items that are **wrong right now** and must be fixed before the code runs:

| # | location | current | correct | ADR ref |
|---|----------|---------|---------|---------|
| 1 | `run.py:57` comment | `result["final_answer"]` | `result["draft"]` | graph.py:70 |
| 2 | `run.py:58` comment | `result["citations"]` | `_parse_citation_pairs(result["draft"])` | api.py:120 |
| 3 | `run.py:64` comment | `result.get("langfuse_trace_id")` | `result.get("trace_id")` | graph.py:60 |
| 4 | `scorers.py:70–72` | zero-citations → `0.0, False` | `score=None` N/A sentinel | ADR 0012 D1 |
| 5 | `run.py:191` comment | `os.environ["ANALYST_MODEL"]` | `settings.position_judge_model` | ADR 0012 D3 |
| 6 | `scorers.py:42` | `citations: list[dict]` docstring says `span, char_start, char_end` | should be `guidance_id, chunk_index` | api.py:120 |
| 7 | `config.py` | no `position_judge_model` field | add `position_judge_model: str = "claude-sonnet-4-6"` | ADR 0012 D3 |
| 8 | `run.py:105–112` | `all([])` is True — broken harness reports green | gate must fail on empty, error, or under-count | ADR 0012 D2 |
| 9 | `run.py:73` | `make_corpus_lookup` raises `NotImplementedError` | replace with `make_resolver` (backed by `check_citation`) | ADR 0012 D1 |

---

## Implementation order (suggested)

1. `config.py` — add `position_judge_model` (1-liner, unblocks everything).
2. `ScoreResult` — change `score` to `float | None`.
3. `AgentResponse` — update docstring; citations type stays `list[dict]`.
4. `run_agent` — wire `run_graph`; parse citations via `_parse_citation_pairs`.
5. `make_resolver` — replace `make_corpus_lookup` in `run.py`.
6. `CitationValidityScorer` — rewrite `__init__` and `score`.
7. `judge.py` — thin judge wrapper.
8. `KeyFactCoverageScorer.score` — wire judge + retry.
9. `PositionQualityScorer.score` — wire judge + retry.
10. `main()` — wire all three scorers; add `--fixture` and `--no-llm-judges` flags.
11. Gate hardening in `run_eval`.
12. `write_report` — baseline label + zero-citation count.
13. `evals/fixtures/ci_key_fixture.jsonl` — planted bogus case.
14. `.github/workflows/evals.yml`.
15. Run `uv run python -m rra.evals.run`; check report.
16. Open draft PR; confirm CI fails on bogus case.
