---
name: project-day4-decisions
description: Key engineering decisions made during Day 4 LangGraph orchestrator implementation
metadata:
  type: project
---

Day 4 (2026-06-01): Four-node LangGraph orchestrator built and wired.

**Why:** Replaces Day 3 single-shot Anthropic call with planner→researcher→analyst↔critic loop. ADRs 0008 and 0009 govern the design.

**How to apply:** When extending the graph (Day 5 MCP, Day 6 evals), understand these implementation choices:

- test_api.py mocking was updated to patch `rra.api.run_graph` — old patches (`rra.api.search_corpus`, `rra.api.Anthropic`) removed since api.py no longer imports those. All contract assertions preserved.
- `_graph` is a module-level singleton in `rra.graph`. Tests reset it via autouse fixture + `patch("rra.graph._get_checkpointer", return_value=MemorySaver())`.
- Cap-hit is set by the critic node (not a separate routing node), which simplifies the graph topology.
- `token_usage` uses an `Annotated` reducer (`_merge_token_usage`) in GraphState so agents don't need to read prior state.
- LangGraph `add_node` requires `# type: ignore[type-var]` because node functions accept `dict[str, Any]` not `GraphState` TypedDict (LangGraph runtime passes a plain dict regardless of typing).
- `_format_user_prompt` moved from `api.py` to `agents/analyst.py`, exported as `format_user_prompt`. If anything imports from api.py directly, update the import path.
- Planner system prompt has 3 few-shot examples to push past 1024-token cache threshold. May still miss threshold depending on encoding; monitor cache hit rate in Langfuse.
