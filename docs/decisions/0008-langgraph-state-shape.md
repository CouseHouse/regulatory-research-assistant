# 0008 — LangGraph state shape and agent contracts

**Status:** Active
**Date:** 2026-06-01
**Owner:** Kyle Couse

## Context

Day 4 introduces a four-node LangGraph state machine. The `GraphState` TypedDict is the inter-agent contract: every field added is inherited by tests, the Postgres checkpointer schema, and Langfuse traces. Several field-level choices are non-obvious and will be relitigated without recorded rationale: whether `critic_notes` accumulates or replaces, who increments `revision_count`, whether the researcher is a Python function or an LLM node, and how cap-out surfaces to the API caller.

## Decision

We define `GraphState` with the fields below, each owned by exactly one writer. The researcher is a Haiku-backed LLM agent whose primary work is query reformulation. Cap-out and escalation surface via an additive `warning: str | None` field in `QueryResponse`.

**State fields and ownership:**

| Field | Type | Writer | Reader(s) |
|---|---|---|---|
| `query` | `str` | api.py (init) | planner, analyst |
| `product_context` | `str` | api.py (init) | planner, analyst |
| `session_id` | `str` | api.py (init) | all nodes (tracing) |
| `trace_id` | `str \| None` | api.py (init) | api.py (output) |
| `sub_questions` | `list[str]` | planner | researcher |
| `outline` | `str` | planner | analyst |
| `passages` | `list[RetrievedPassage]` | researcher | analyst, critic, api.py |
| `draft` | `str` | analyst | critic, api.py |
| `verdict` | `Literal["approve","revise","escalate"] \| None` | critic | router |
| `critic_notes` | `list[CriticNote]` | critic | analyst |
| `revision_count` | `int` | critic | critic (reads prior), router |
| `cap_hit` | `bool` | critic node | api.py |
| `token_usage` | `dict[str, int]` | each agent (accumulated) | api.py (logging) |

**`QueryResponse` extension:** Add `warning: str | None = None`. Populated when `cap_hit` or verdict is `escalate`; `None` on normal exits. This is an additive optional field, permitted by the frozen-contract policy in `schemas.py`.

## Alternatives considered

- **Flat dict state (no typed fields)** — Rejected. LangGraph supports TypedDict and Pydantic; untyped dicts lose mypy coverage and make the inter-agent contract invisible.

- **`critic_notes` appends across passes** — Rejected. The analyst must act on the *current* critique, not a history of all prior notes. Accumulated notes would require the analyst to filter "already addressed" entries, adding complexity with no correctness gain. The Langfuse trace captures per-pass history for debugging.

- **`revision_count` written by the router** — Rejected. LangGraph routing functions return a destination string; they cannot update state. The critic writes the incremented count as part of its node output, making the cap check in the router a simple `>=` comparison.

- **`token_usage` as a running integer total** — Rejected. Per-agent granularity (`"analyst_input": 1200, "critic_output": 180`) enables cost attribution in Langfuse without aggregation loss. Summation is trivial at the api.py output layer.

- **Researcher as a Python-function node (no LLM call)** — Rejected. A plain function node does nothing beyond calling `search_corpus` in a loop, which forecloses the query-reformulation capability: expanding acronyms, adding regulatory synonyms, rephrasing sub-questions for the embedding space. Without reformulation, the researcher is inert boilerplate and the spec §4.2 cost model ("~3 Haiku calls per query") has no justification. The reformulation step is also a Day 7 eval lever: retrieval recall with vs. without reformulation.

- **`warning` embedded in `answer` text** — Rejected. Embedding a system-generated caveat in the answer text corrupts the analysis with metadata. An optional field keeps answer text clean and gives callers a machine-readable signal.

## Consequences

**Enables:**
- Clear ownership map: every field has exactly one writer, preventing accidental overwrites across nodes.
- Researcher query reformulation as a first-class retrieval improvement and future eval lever.
- Clean cap-out signal to API callers without polluting answer text.
- Postgres checkpointer can serialize/deserialize `GraphState` with no schema ambiguity.

**Constrains:**
- Adding a field to `GraphState` requires updating the checkpointer schema migration, the Langfuse trace shape, and any test that constructs a full state object.
- `QueryResponse.warning` is now part of the frozen contract; removing it requires a version bump.
- Researcher's Haiku call adds ~100ms latency vs. a Python-function node; acceptable for a 20–40s total query time.

**Reopen if:**
- A node needs to write a field owned by another node (indicates a missing state field or a wrong ownership assignment).
- The Postgres checkpointer cannot serialize a `GraphState` field type (e.g., nested Pydantic models) — resolve by flattening or JSON-encoding the field, and record the fix here.
- Query reformulation eval (Day 7) shows zero recall improvement — then the Haiku call cost is not justified and the researcher-as-Python-function alternative reopens.

## Related

- spec.md §3.1 (agent roles), §4.2 (model assignment), §5 (data flow)
- ADR 0001 (LangGraph) — framework commitment
- ADR 0003 (Anthropic SDK direct) — governs how each agent calls the model
- ADR 0006 (citation span addressing) — `RetrievedPassage` and `Citation` shapes that appear in state
- ADR 0009 (critic-loop policy) — the routing logic that reads `verdict` and `revision_count`
- `docs/day4-design.md` — full design rationale
