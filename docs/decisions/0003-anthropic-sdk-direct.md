# 0003 — Anthropic SDK direct for model calls; not framework-portable

**Status:** Active
**Date:** 2025-05-30
**Owner:** Butters

## Context

A "make it portable" argument was raised: route all model calls through LangChain so we could swap Anthropic for OpenAI or Gemini later. The trade-off is between optionality and feature depth.

## Decision

We use the **Anthropic SDK directly** for model calls. `langchain-anthropic` is used only as a thin adapter where LangGraph requires it. We do NOT abstract the model layer across providers.

## Alternatives considered

- **LangChain across all model calls** — Loses Anthropic prompt caching (a 50-90% cost reduction on the critic-revision loop), native tool use ergonomics, Citations API, and extended thinking. The portability is a Stack Overflow benefit; the loss is real money and real reliability.
- **LiteLLM as a thin shim** — Lighter than LangChain, but still flattens to lowest-common-denominator semantics. Worth revisiting if a customer actually requires multi-provider, but speculative for v1.
- **Abstract at the role layer** — Define `Planner`, `Researcher`, etc. as classes with a `run()` interface; Anthropic-only implementation in v1. Cleaner than LangChain everywhere. Worth doing if portability becomes a real requirement, but premature now.

## Consequences

**Enables:**
- Prompt caching on stable system prompts
- Use of Citations API for the critic's `check_citation` tool
- Extended thinking on planner / analyst / critic
- Stronger interview story ("I picked the best tool for the job") vs ("I hedged with an abstraction layer")

**Constrains:**
- Provider swap is a non-trivial refactor, not a config change
- Tied to Anthropic's pricing and uptime

**Reopen if:**
- Customer requires a provider Anthropic doesn't offer
- Anthropic has sustained outage or pricing shock
- A second-provider implementation becomes a real requirement, not speculation

## Related

- spec.md §4.1, §4.2
- ADR 0001 (LangGraph)
