# Day 4 Design — Multi-agent orchestrator

**Status:** Draft for review  
**Author:** Claude (Opus model, design phase)  
**Date:** 2026-06-01  
**Next step:** Review → draft ADRs → implement in separate phase

---

## Overview

This document specifies the LangGraph state machine that replaces the single-shot Anthropic call in `api.py`. It covers state shape, agent contracts, the critic-revision loop, API contract preservation, source-diversity handling, ADR candidates, and checkpointing. No code is written here.

Constraints honored: ADR 0001 (LangGraph), 0003 (Anthropic SDK direct), 0006 (citation contract), ADR 0004 (psycopg3 pool). Any tension with active ADRs is flagged explicitly.

---

## A. State shape

### Why this is the hardest-to-reverse decision

The state TypedDict is the inter-agent contract. Every field added is inherited by tests, trace serialization, and the Postgres checkpointer schema. Removal is a breaking change.

### CriticNote (internal type, not in QueryResponse)

```python
class CriticNote(BaseModel):
    citation_key: str | None   # "guidance_id:chunk_index"; None for claim-level issues
    issue: str                 # what the critic found wrong
    severity: Literal["hard", "soft"]
    # hard = analyst must address; soft = suggestion
```

`CriticNote` is internal to the graph. It lives in `src/rra/graph.py` (or `src/rra/agents/types.py` if preferred), not in `schemas.py`. It is never serialized into `QueryResponse`.

### GraphState

| Field | Type | Default | Written by | Read by | Notes |
|---|---|---|---|---|---|
| `query` | `str` | required | api.py (init) | planner, analyst | never modified after init |
| `product_context` | `str` | `""` | api.py (init) | planner, analyst | never modified after init |
| `session_id` | `str` | required | api.py (init) | all nodes (for tracing) | UUIDv4 string |
| `trace_id` | `str \| None` | `None` | api.py (init) | api.py (output) | Langfuse trace ID, set before graph entry |
| `sub_questions` | `list[str]` | `[]` | planner | researcher | 2–4 strings |
| `outline` | `str` | `""` | planner | analyst | section headings for synthesis |
| `passages` | `list[RetrievedPassage]` | `[]` | researcher | analyst, critic, api.py | chunk-level deduped flat list |
| `draft` | `str` | `""` | analyst | critic, api.py | raw answer with inline `[guid:idx]` citations |
| `verdict` | `Literal["approve","revise","escalate"] \| None` | `None` | critic | router | replaced each critic pass |
| `critic_notes` | `list[CriticNote]` | `[]` | critic | analyst | replaced (not appended) each critic pass |
| `revision_count` | `int` | `0` | critic | critic (reads prior), router | incremented when critic emits "revise" |
| `cap_hit` | `bool` | `False` | router | api.py | True when max revisions exceeded |
| `token_usage` | `dict[str, int]` | `{}` | each agent | api.py (logging) | e.g. `{"planner_input": 450, "analyst_output": 320}`; accumulated |

**Field-by-field rationale for non-obvious choices:**

- **`outline` is a string, not `list[str]`** — The analyst uses it as prose context ("Draft a response organized as: 1. Applicable pathway…"). A structured list would constrain prompt format unnecessarily for v1.
- **`critic_notes` is replaced per pass, not appended** — The analyst should act on the *current* critique, not a history of all prior notes. The Langfuse trace captures history for debugging. Appending would require the analyst to filter "already addressed" notes, which adds complexity for no correctness gain.
- **`revision_count` is written by the critic (not the router)** — LangGraph routing functions cannot update state; they return a string. The critic increments `revision_count` when emitting "revise", so the router can read the already-incremented value. This makes the cap check in the router a simple comparison: `state.revision_count >= settings.max_critic_revisions`.
- **`token_usage` is a dict, not a running total** — Per-agent granularity enables Langfuse-level cost attribution without aggregation loss. api.py sums it post-graph for logging.
- **`trace_id` is written before graph entry** — The Langfuse trace is opened in api.py (unchanged behavior) and the trace ID is threaded into state so any node can annotate its span. Alternatively, nodes could call `get_langfuse()` directly; choosing thread-through to keep node functions pure.

### What is NOT in state

- **`guidance_passage_map`** — The analyst and critic both need `{(guidance_id, chunk_index): RetrievedPassage}` for citation lookups. This is computed locally in each node from `state.passages`. Caching it in state would save two O(n) scans per query on a list of ≤25 passages — negligible and not worth the state bloat.
- **Intermediate Anthropic message objects** — Too large; Langfuse traces capture them.
- **`no_evidence` flag** — Researcher returning `passages = []` is handled by the analyst's prompt (emit a grounded refusal). No fast-path flag needed.

---

## B. The four agents

### B.1 Planner (Sonnet)

**Input from state:** `query`, `product_context`  
**Output to state:** `sub_questions: list[str]`, `outline: str`  
**Model:** `claude-sonnet-4-6` (ADR 0003; spec §4.2)

**System prompt sketch:**  
You are a regulatory research planning agent. Given a question about FDA medical device regulatory submissions, decompose it into 2–4 targeted retrieval sub-questions that together cover the user's intent. Each sub-question should target a distinct aspect (e.g., "what triggers a new 510(k)?", "what is the 'same intended use' standard?"). Produce an analysis outline of 2–4 sections that the synthesis agent will follow. Return structured output only.

**Output structure (Pydantic):**
```python
class PlannerOutput(BaseModel):
    sub_questions: list[str] = Field(min_length=1, max_length=4)
    outline: str
```

**Prompt caching:** The system prompt is stable and long enough to cache. Apply `cache_control: {"type": "ephemeral"}` to the system content block. Planner system prompt ≈ 500 tokens; Sonnet cache threshold is 1024 tokens minimum, so this marginally does not cache unless the system prompt is padded with few-shot examples. Plan: add 2–3 few-shot examples to the system prompt to push it past threshold. Defer to implementation.

**Failure modes:**

| Failure | Behavior |
|---|---|
| Returns 0 sub-questions (malformed output) | Default to `[query]` — treat the raw query as the sole sub-question |
| Returns >4 sub-questions | Truncate to first 4; log warning |
| Blank outline | Analyst proceeds with no structural constraint; logs warning |
| Anthropic API error / timeout | Propagate as HTTP 503; no retry in v1 |

---

### B.2 Researcher (Haiku)

**Input from state:** `sub_questions`, `query` (for context if needed)  
**Output to state:** `passages: list[RetrievedPassage]`  
**Model:** `claude-haiku-4-5` (ADR 0003; spec §4.2)

**Day 4 constraint:** Researcher calls `search_corpus()` directly (Python import), not via MCP. Day 5 replaces the direct call with an MCP `search_corpus` tool invocation.

**Processing logic (not a prompt — this is deterministic):**  
For each sub-question in `state.sub_questions`, call `search_corpus(sub_question, k=settings.rerank_top_k)`. Aggregate results, deduplicate at chunk level (same `(guidance_id, chunk_index)` → keep the copy with the higher rerank score). Return as a flat `list[RetrievedPassage]` sorted by score descending.

**Why Haiku?** The researcher's primary work is **query reformulation**: it receives the planner's sub-questions and rephrases each into an optimized retrieval query before calling `search_corpus`. Reformulation tasks include expanding acronyms (e.g., "PMA" → "Premarket Approval"), adding regulatory synonyms, and rephrasing for the embedding space. This is genuine language work that justifies the model call, keeps the "four agents" architecture honest, and preserves spec §4.2's cost model (~3 Haiku calls per query). It also creates a Day 7 eval lever: retrieval recall with vs. without reformulation.

**System prompt sketch:**  
You are a regulatory retrieval agent. For each sub-question, rewrite it as an optimized search query for an FDA guidance document corpus: expand acronyms, add relevant regulatory synonyms, and rephrase for semantic search. Return the reformulated query and then call search_corpus with it. Deduplicate results across sub-questions by (guidance_id, chunk_index), keeping the higher-scoring copy.

**Failure modes:**

| Failure | Behavior |
|---|---|
| All sub-questions return 0 passages | `passages = []`; analyst sees empty context; emits refusal |
| One sub-question returns 0 passages | Others fill the pool; logs at INFO level |
| Voyage API error | Propagate as 503 |
| Post-dedup count is 0 | Same as all-zero case |

---

### B.3 Analyst (Sonnet)

**Input from state:** `query`, `product_context`, `outline`, `passages`, `critic_notes`, `revision_count`  
**Output to state:** `draft: str`  
**Model:** `claude-sonnet-4-6` (spec §4.2)

**System prompt sketch:**  
You are a regulatory research synthesis agent. Using only the provided FDA guidance passages, write a structured regulatory analysis. Follow the outline provided. Cite every factual claim with exactly one `[guidance_id:chunk_index]` bracket immediately after the claim — copy guidance_id and chunk_index exactly as shown in the passage tags. Do not invent citations. Do not cite passages that do not support the specific claim. When revising based on critic feedback, address each noted issue precisely without regressing on correct sections.

**Prompt construction (moved from api.py):**  
The existing `_format_user_prompt` in `api.py` is reusable. It will move to `agents/analyst.py` or a shared `src/rra/prompts.py`. The passage XML format stays identical so `_resolve_citations` continues to work.

**On revisions:** When `critic_notes` is non-empty, the analyst is given:
1. The previous draft
2. The critic notes as a structured list
3. An instruction to edit in place (not fully rewrite)

This is cheaper than full re-synthesis (preserves correct sections, targets only flagged citations) and less likely to introduce new errors.

**Prompt caching:** Analyst system prompt is the longest and most stable across all calls. Apply cache control. The system prompt + outline will likely exceed the 1024-token caching threshold. The passages section of the user message changes per query, so it is not cacheable — only the system message benefits.

**Failure modes:**

| Failure | Behavior |
|---|---|
| `passages` is empty | Emit a grounded refusal: "The corpus does not contain evidence to answer this question." No citations. |
| No citations in draft | Critic will flag; normal revision path |
| Malformed citations `[X:Y:Z]` | `_resolve_citations` silently skips unparseable brackets |

---

### B.4 Critic (Sonnet)

**Input from state:** `draft`, `passages`, `query`, `revision_count`  
**Output to state:** `verdict: Literal["approve","revise","escalate"]`, `critic_notes: list[CriticNote]`, `revision_count` (incremented if "revise")  
**Model:** `claude-sonnet-4-6` (spec §4.2)

**Day 4 constraint:** Critic performs a context-match check (did the analyst cite a passage that was actually provided, and does that passage plausibly support the claim?) without calling the `check_citation` MCP tool. Day 5 wires the MCP tool, enabling verified text-level checking.

**System prompt sketch:**  
You are a citation verification agent. Given a draft regulatory analysis and the source passages, verify that every inline citation `[guidance_id:chunk_index]` (a) refers to a passage that was actually provided and (b) the cited passage genuinely supports the specific claim it is attached to. Return structured JSON with a verdict and issue list. Use "approve" if all citations are valid and grounded. Use "revise" if specific citations are fixable (wrong passage cited, claim overclaimed). Use "escalate" if the question cannot be grounded in the available corpus at all — this signals a corpus coverage gap and exits immediately; do not use it for individual bad citations.

**Escalate vs. revise distinction (ADR 0009):** `escalate` fires whenever the critic determines the corpus lacks evidence for the question — including on the first pass. It is not a "give up after N tries" signal; it is a "this question is unanswerable from this corpus" signal. `revise` is for fixable citation errors where the evidence exists but was cited incorrectly or overclaimed.

**Output structure:**
```python
class CriticOutput(BaseModel):
    verdict: Literal["approve", "revise", "escalate"]
    notes: list[CriticNote]
    # revision_count handled by the node function, not emitted by LLM
```

**Prompt caching:** Critic system prompt is long and stable. Same caching guidance as analyst.

**Failure modes:**

| Failure | Behavior |
|---|---|
| Always emits "revise" (adversarial/confused) | Hard cap at `max_critic_revisions = 2` (router enforces) |
| Emits "revise" with empty notes | Analyst receives no actionable feedback; logs warning; treated as soft loop |
| Emits "escalate" on first pass | Immediate exit; return current draft with escalation flag |
| Malformed JSON output | Fallback: treat as "approve", log at error level (avoids infinite loop) |

---

## C. The critic loop

### Graph topology

```
START
  │
  ▼
planner ──► researcher ──► analyst ──► critic
                               ▲           │
                               │           ▼
                               │      route_after_critic()
                               │           │
                    revision_count < 2     │  approve / escalate / cap_hit
                    verdict == "revise"    │
                               │           ▼
                               └───────  END
```

### Routing function

```
route_after_critic(state: GraphState) -> str:
    if state.verdict in ("approve", "escalate"):
        return END
    if state.revision_count >= settings.max_critic_revisions:
        # cap_hit written by the routing node or a helper before returning
        return END
    return "analyst"
```

Note: `cap_hit = True` must be written to state at cap-out. Since routing functions cannot update state in LangGraph, the cap-hit flag is set by a thin wrapping node that calls the critic, increments revision_count, and sets cap_hit before the routing decision. Alternatively, a `CriticOutput` that includes `cap_hit` is handled at the node level. Implementation detail for the coding phase.

### Termination conditions

| Condition | Action |
|---|---|
| `verdict == "approve"` | Exit graph immediately |
| `verdict == "escalate"` | Exit graph immediately on **any pass** (including first); signals corpus coverage gap, not a fixable citation error |
| `verdict == "revise"` and `revision_count < 2` | Increment `revision_count`, route back to analyst |
| `verdict == "revise"` and `revision_count >= 2` | Set `cap_hit = True`, exit graph |

`max_critic_revisions = 2` is configurable via `settings.max_critic_revisions` (flows through `config.py` per project rule). Default: 2 (spec §3).

### Edit-in-place vs. re-synthesize

**Recommendation: edit-in-place.**

The analyst is given the prior draft + critic notes + an instruction to revise targeted sections only. This is cheaper (analyst does not re-read all passages from scratch), less likely to regress correct sections, and more controllable for the eval harness (we can measure "did the critic note get addressed?" independently).

Full re-synthesis is only warranted if the structure of the draft is fundamentally wrong — which the `escalate` verdict handles (the analyst and critic both get the message that the corpus doesn't cover the claim). When verdict is "revise", the issues are by definition fixable, so targeted edits are appropriate.

### Cap-out behavior

When `cap_hit = True`, return the current `state.draft` as-is. Do not discard it; the draft is likely mostly correct and the cap may represent a minor disputed citation. Indicate the cap was hit via an optional `warning` field in `QueryResponse`.

This requires a small backward-compatible schema extension (see Section D).

---

## D. Contract preservation

### QueryResponse is unchanged, with one additive extension

The five fields specified in ADR 0006 — `guidance_id`, `chunk_index`, `char_start`, `char_end`, `quoted_text` — are untouched. `Citation` and `RetrievedPassage` are untouched.

**Additive extension proposed:** Add `warning: str | None = None` to `QueryResponse`. This field:
- Is `None` for normal responses (no breaking change for existing callers)
- Is set to a human-readable string when `cap_hit = True` or `verdict == "escalate"` at graph exit
- Is explicitly permitted by the comment in `schemas.py`: "Later days add features via optional fields only"

Example values:
- `"Analysis reached maximum revision limit. Citations may not be fully verified."`
- `"Query could not be fully grounded in available guidance. Answer is best-effort."`

This is a minor schema evolution. It should be noted in the commit that updates `schemas.py`.

### What changes in api.py

**Remove:**
- The `search_corpus()` call (researcher does this now)
- The early-return on empty passages (graph handles this)
- The system_prompt and user_prompt construction (moves to analyst node)
- The `anthropic_client` instantiation and `messages.create()` call
- The `gen_cm` / generation span wrapper (replaced by graph-level Langfuse tracing)

**Add:**
- Import of `run_graph` (or `graph.invoke`) from `src/rra/graph.py`
- Graph invocation: `final_state = run_graph({query, product_context, session_id, trace_id})`
- Read `final_state["draft"]` and `final_state["passages"]` for response assembly
- Pass `final_state["cap_hit"]` and `final_state.get("verdict")` to set `warning`

**Stay unchanged:**
- `_verify_api_key` — no change
- `_parse_citation_pairs` — no change (still parses `[guid:idx]` from the final draft)
- `_resolve_citations` — no change (receives `state.draft` and `state.passages`)
- The `POST /query` endpoint signature — `QueryRequest` in, `QueryResponse` out
- Langfuse trace setup (trace opened in api.py, trace_id threaded into state)
- The `QueryResponse` assembly pattern

**Move (not remove):**
- `_format_user_prompt` → `src/rra/agents/analyst.py` (or a shared `src/rra/prompts.py`)

The response assembly in api.py after Day 4 looks like:
```
final_state = run_graph(initial_state)
citations = _resolve_citations(final_state["draft"], final_state["passages"])
return QueryResponse(
    answer=final_state["draft"],
    citations=citations,
    passages=final_state["passages"],
    trace_id=final_state["trace_id"],
    warning=_build_warning(final_state),  # None if normal
)
```

This preserves the contract exactly. Existing tests against `/query` continue to pass with zero schema changes (the new `warning` field defaults to `None`).

---

## E. Source-diversity question (from dev-log)

### The question

The Day 3 smoke tests showed: retrieval returns multiple chunks from the same guidance document for some queries. The multi-agent design adds a planner that decomposes into sub-questions. Should the researcher deduplicate results at the **document level** (per-source cap) or pass all chunk-level results?

### Recommendation: chunk-level dedup only, no per-document cap

**Reasoning:**

1. **The planner's sub-question decomposition already creates source diversity.** Each sub-question targets a distinct aspect of the original query. A sub-question like "what constitutes a major change to a 510(k) device?" will surface different documents than "what is the RTA standard for 510(k) submissions?" The natural diversity of well-formed sub-questions reduces same-document clustering at the output level.

2. **The dev-log supports this.** The Day 3 smoke test found: "The diversity issue from the first query does NOT appear on synthesis-type queries. The reranker surfaced diverse sources when the query naturally spanned topics." Multi-sub-question search amplifies this effect.

3. **Deduping by document loses valid information.** If two chunks from the same guidance genuinely address different sub-questions, both should reach the analyst. A per-document cap would force the researcher to choose between them, risking recall loss on narrow topics where one guidance is the definitive source.

4. **The reranker already filters redundancy.** Each sub-question's top-k reranked results have already been filtered for relevance. A second per-document filter is a quality lever, not a correctness requirement.

**What "chunk-level dedup" means concretely:**

After aggregating search results across all sub-questions, if the same `(guidance_id, chunk_index)` tuple appears from multiple sub-question searches, keep the copy with the **higher rerank score**. Sort the final flat list by score descending.

**When to add per-document capping (a future-work flag):**

If evals show that a single guidance is consistently crowding out others in the passages list (e.g., 4 of 5 analyst-visible passages are from one document), add a `max_chunks_per_guidance: int` setting (default: uncapped). Wire it into the researcher's dedup step. This is future-work §12 (MMR/per-source cap) from the dev-log; the multi-sub-question architecture defers the need.

---

## F. ADRs needed

### ADR-worthy: yes, two candidates

**Candidate 1: LangGraph state design and agent contracts**

Title suggestion: `0008-langgraph-state-shape.md`

ADR-worthy because:
- The `GraphState` TypedDict is the inter-agent contract for all of Day 4, Day 5 (MCP), and Day 6 (evals). Any field added, renamed, or removed has blast radius across all nodes, the checkpointer schema, and the Langfuse trace shape.
- Specific decisions embedded in the state design are non-obvious and contested: why `critic_notes` replaces (not appends), why `revision_count` is written by the critic not the router, why the researcher may be a Python-function node rather than an LLM node, why `token_usage` is a dict. These are the kinds of decisions that a future contributor would relitigate without the rationale.
- The decision to make `QueryResponse.warning` an optional additive field (vs. embedding the warning in `answer`) is a contract evolution that should be recorded.

**Candidate 2: Critic-loop policy**

Title suggestion: `0009-critic-loop-policy.md`

ADR-worthy because:
- The policy choices (hard cap 2, edit-in-place on revise, escalate on ungroundable claims, return best-effort with flag on cap-out) are individually defensible but not obvious from the spec's one-liner. Future contributors need to understand *why* these choices were made before changing them.
- Eval-driven reopening is a real possibility: spec §3.1 says "Reopen if: Eval shows critic adds <2% citation_validity over single-agent baseline." When that eval result comes in, the ADR is the right place to record whether the critic is cut or modified.
- The "escalate vs. revise" distinction is a regulatory UX decision with user-facing implications — "you asked about something we can't ground" vs. "we found evidence but some citations need fixing" — that deserves documentation beyond a code comment.

### Not ADR-worthy (address in implementation)

- Source diversity / chunk-level dedup (Section E): captured in the researcher node's docstring and a `# future-work` comment. Not architecturally contested.
- Prompt caching per-agent: operational optimization, revisable without ceremony.
- Researcher as Python-function vs. LLM node: implementation detail; if the answer changes during coding, update the dev-log. Elevate to ADR only if it's still contested after Day 4.
- Token budget enforcement: not a contested architectural question; a config value and a per-call `max_tokens`.

---

## G. Checkpointing

### What exists

The `langgraph` schema already exists in the Postgres DB (Day 4 plan confirms this). LangGraph's `PostgresSaver` writes state after every node execution to this schema, keyed by `thread_id` (which we use as `session_id`).

### Is it needed for v1?

**Yes — wire it in Day 4 as specified.** Reasoning:

1. **It's listed as a Day 4 deliverable:** "LangGraph Postgres checkpointer wired to app DB." Deferring it means Day 4 is incomplete by the project's own definition.
2. **The operational cost is low.** The schema exists. LangGraph's wiring is ~10 lines in `graph.py`. Connection pool (ADR 0004) is already shared.
3. **The latency overhead is acceptable.** ~50ms per checkpoint write × 4 nodes = ~200ms added to a 20–40 second query. This rounds to noise.
4. **It enables the human-in-the-loop story** that spec §4.1 uses to justify LangGraph over alternatives. Without it, the LangGraph choice looks like complexity without payoff.
5. **Session_id = thread_id** is the right co-keying strategy: traces in Langfuse and state checkpoints in Postgres share the same session identifier, which makes debugging straightforward (one ID, two lookups).

### Design note on sync vs. async

Day 3 api.py is synchronous (`def query(...)`, not `async def`). `PostgresSaver` has a sync interface. No async migration is required for Day 4. If future load testing shows the thread pool is a bottleneck, the migration path is: switch to `AsyncPostgresSaver` + `async def query(...)` + `psycopg3`'s native async — no driver swap needed (ADR 0004 anticipates this). Not a Day 4 concern.

### What checkpointing does NOT solve

- **Resume after crash in v1:** The HTTP client has no resume mechanism; a crashed mid-graph query results in a 500 and the client retries. Checkpointing still adds value (Langfuse trace and state are recoverable for debugging), but transparent resume is future-work.
- **Long-lived sessions:** v1 is stateless from the API client's perspective. Each `POST /query` is a new session. The checkpointer is not used for multi-turn conversation state in v1.

---

## Open questions for implementation phase

1. **`QueryResponse.warning` field** — Approved as an additive optional field. Decide signal text at implementation: one string covering both `cap_hit` and `escalate` cases, or two distinct strings?

2. **Analyst prompt location** — `agents/analyst.py` (co-located with logic) or `src/rra/prompts.py` (all prompts in one place for eval iteration)?

3. **`token_usage` accumulation** — Not required by spec. Keep for dev observability or cut for v1 simplicity?
