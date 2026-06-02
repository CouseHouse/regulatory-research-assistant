# 0001 — LangGraph for orchestration

**Status:** Active
**Date:** 2025-05-28
**Owner:** Kyle Couse

## Context

The system is a multi-agent RAG pipeline with at minimum a planner, researcher, analyst, and critic. We need an orchestrator that handles conditional routing, durable state, and human-in-the-loop interruption — and that ports to AWS Bedrock AgentCore or Azure AI Foundry without rewrites.

## Decision

We use **LangGraph** as the orchestrator. `langchain-anthropic` is a thin model adapter where convenient. Roles that need prompt caching, citations, or extended thinking call the Anthropic SDK directly.

## Alternatives considered

- **CrewAI** — Faster to prototype but the role-based abstraction hides control flow; debugging state transitions is harder.
- **AutoGen** — Stronger research feel, weaker production tooling at v1 time. Microsoft's positioning has shifted multiple times in 2024-2025.
- **Raw LangChain (no LangGraph)** — Lacks conditional routing primitives the critic-revision loop needs.
- **LangChain across all model calls (for provider portability)** — Discussed and explicitly rejected. Would lose Anthropic prompt caching, native tool use ergonomics, Citations API, and extended thinking. See ADR 0003.
- **Custom Python state machine** — Wastes time on infrastructure the project doesn't differentiate on.

## Consequences

**Enables:**
- Explicit graph inspection for debugging
- Postgres-backed checkpoints for durable state and human-in-the-loop
- Native deployability to AgentCore and Foundry

**Constrains:**
- We're on LangGraph's release cadence and API stability
- Some power users prefer raw control flow; we're choosing a framework

**Reopen if:**
- LangGraph's persistence model breaks under our load
- A customer-mandated cloud doesn't support LangGraph deployment shapes

## Related

- spec.md §4.1
- ADR 0003 (Anthropic SDK direct)
