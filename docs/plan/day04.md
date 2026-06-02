# Day 4 — Multi-agent orchestrator

## Goal

LangGraph state machine with 4 agents (planner, researcher, analyst, critic), bounded critic loop. The API contract from day 3 is unchanged.

**This is the day Opus matters most for the design phase.** State machine shape is the hardest reversible decision in the project.

## Deliverables

- `src/rra/graph.py`: state definition, nodes, conditional edges, checkpoint config
- `src/rra/agents/planner.py`: query decomposition → sub-questions + outline
- `src/rra/agents/researcher.py`: takes sub-questions, returns ranked passages (wraps day-3 retrieval)
- `src/rra/agents/analyst.py`: synthesizes draft with inline citations
- `src/rra/agents/critic.py`: verifies citations, emits `approve|revise|escalate` + notes
- `src/rra/api.py` updates: replace single-shot logic with graph invocation, preserve response shape
- LangGraph Postgres checkpointer wired to app DB (`langgraph` schema already exists)
- `tests/test_graph.py`: tests for happy path, critic-revision path, critic-escalation path, max-revisions cap

## Design constraints (from spec)

- Planner-worker-critic pattern with bounded revision (spec §3.1)
- Hard cap at 2 critic revisions (spec §3, configurable via `max_critic_revisions`)
- Planner/analyst/critic → Sonnet; researcher → Haiku (spec §4.2)
- All agents emit traces visible in Langfuse
- Critic outputs structured (Pydantic), not free text

## Decisions to make in the planning phase

1. State shape: what flows between nodes? At minimum: `query`, `product_context`, `sub_questions`, `outline`, `passages`, `draft`, `critic_notes`, `revision_count`.
2. Critic revision implementation: edit-in-place on the draft, or full re-synthesis? Edit-in-place is cheaper and more controllable.
3. Token-budget enforcement: hard cap from `settings.max_tokens_per_query` — where to check it?
4. Prompt caching: which agents have stable system prompts worth caching? Critic and analyst likely yes (long instructions); planner less so.
5. Failure modes: what happens if researcher returns zero passages? If `check_citation` fails on day 5 (not yet wired)?

## Stop conditions

- One full query produces a clean Langfuse trace showing all 4 agents in sequence
- Critic loop terminates correctly — test forcing a contradiction (mock the critic to always say `revise`); verify cap-out behavior
- API response shape unchanged from day 3 (existing tests still pass)
- mypy clean, ruff clean

## Don't do yet

- MCP tools (day 5) — researcher and critic still call retrieval functions directly today
- The eval harness (day 6)
- Performance tuning — just make it work

## Definition of done

Dev-log entry shows: a Langfuse trace URL screenshot or summary, one revision-loop example, total token cost for a typical query, any surprises about LangGraph state management.
