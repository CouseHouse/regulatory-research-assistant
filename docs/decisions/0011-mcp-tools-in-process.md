# 0011 — MCP tools as in-process functions; server as exposure layer

**Status:** Active
**Date:** 2026-06-01
**Owner:** Kyle Couse

## Context

The Day 5 design plan states that the critic agent "calls `check_citation` via MCP." That phrase is ambiguous. A natural reading implies the critic spawns an MCP subprocess and communicates via the MCP protocol transport — the same mechanism an external client like Claude Desktop would use. That reading is incorrect and would introduce latency, startup-ordering fragility, and test coupling. A recorded decision is needed so this is not accidentally implemented (or later refactored) that way.

The broader question — how do agents invoke tool functions, and what is the MCP server's role relative to in-process agent calls — recurs for every tool in `src/rra/mcp_server/tools.py`. Without a decision on record, the answer is re-derived (and potentially wrong) by each person who touches the agent code.

## Decision

Tool functions live in `src/rra/mcp_server/tools.py` as plain importable Python. Agent code imports and calls them directly, in-process, as ordinary function calls. `src/rra/mcp_server/server.py` is a thin MCP protocol wrapper that registers the same functions as MCP handlers for external clients. There is no subprocess-per-query for agent calls.

**File structure:**

```
src/rra/mcp_server/
    __init__.py     (package marker)
    tools.py        (tool functions; pure Python; importable standalone)
    server.py       (MCP server; registers tools.py functions as MCP handlers)
```

**Agent import pattern:**

```python
# researcher.py
from rra.mcp_server.tools import search_corpus

# critic.py
from rra.mcp_server.tools import check_citation
```

**MCP server registration pattern (server.py):**

```python
# using FastMCP or the Python MCP SDK
@mcp.tool()
def check_citation(...):
    ...  # same underlying function, registered as MCP handler
```

The functions in `tools.py` are the single implementation. `server.py` delegates to them; it does not duplicate them.

**"Via MCP" means "via the tool function defined for MCP consumption," not "via the MCP transport."** When design documents say an agent calls a tool "via MCP," they mean the agent depends on the tool abstraction defined in `tools.py` — the same function surface exposed over the MCP protocol to external clients. The transport layer is not involved.

## Alternatives considered

- **Subprocess-per-query (start MCP server, communicate via stdio/HTTP per call)** — Rejected. Adds approximately 50–100ms IPC overhead per tool call; the critic calls `check_citation` once per inline citation in the draft, so the overhead multiplies. Makes critic node startup fragile: the critic node would depend on the MCP server process being running and healthy before it executes. Complicates error handling: Python exceptions must be serialized to MCP protocol errors, sent over the transport, deserialized, and re-raised — adding three failure surfaces relative to a direct function call. Complicates testing: tests would need to start and tear down a server process. No offsetting benefit for in-process callers.

- **LangGraph tool node (LLM-callable tool alongside `submit_verdict`)** — Rejected for citation verification specifically. Making `check_citation` LLM-callable would make verification non-deterministic across runs: the LLM decides when to call the tool and which citations to check. Deterministic pre-validation — calling `check_citation` in Python before the LLM call, for every inline citation in the draft — is what makes the `citation_validity` gate (spec §6.2) meaningful. This decision governs transport only; the calling pattern (pre-validation before LLM call) is recorded in ADR 0010.

- **Direct import from `rra.retrieval` (bypass `tools.py` entirely)** — Rejected. Routing through `tools.py` adds Pydantic input validation and a consistent error surface (`ToolError` with `retryable` semantics). It also makes the tool-layer dependency explicit in the import path, which makes it possible to later add per-tool instrumentation (Langfuse spans, rate limiting) in one place rather than at every call site. The cost is one indirection level; the benefit is a stable abstraction boundary.

## Consequences

**Enables:**
- Direct function call latency and reliability: no IPC round-trip, no subprocess startup, no transport-layer serialization.
- Tests call tool functions as plain Python without starting an MCP server. Unit tests for `check_citation` matching logic are isolated from the MCP protocol entirely.
- Protocol-layer integration tests (Claude Desktop compatibility, external MCP client behavior) start `server.py` as a separate process in their own test scope. The two test surfaces do not interfere.
- `tools.py` is independently importable: if the MCP server is not running (local dev, unit test environment), agents still work.
- A single implementation in `tools.py` serves both in-process callers and external MCP clients. Changes to tool logic propagate automatically to both.

**Orthogonal to ADR 0003:**
ADR 0003 governs model calls — the decision to use the Anthropic SDK directly rather than a framework abstraction. This ADR governs Python function calls among agents and tools. The two decisions are independent. ADR 0003 does not imply anything about how tool functions are invoked.

**Constrains:**
- Tool functions in `tools.py` must be safe to call from multiple concurrent contexts: they must not rely on per-process singleton state that would be corrupted by concurrent in-process calls. Connection pooling (ADR 0004) satisfies this for DB access.
- If a future requirement needs genuine isolation between the agent process and the tool execution environment (e.g., sandboxing, resource limits, separate failure domains), the in-process model would need to be replaced. That scenario is not anticipated; if it materializes, it warrants a new ADR superseding this one.
- External MCP clients (Claude Desktop) go through `server.py` and the MCP transport, which means they see MCP protocol error shapes, not Python exceptions. `server.py` is responsible for translating `ToolError` and unhandled exceptions to MCP protocol errors. In-process callers handle Python exceptions directly. The error contract in `tools.py` must support both surfaces.

## Related

- ADR 0003 (Anthropic SDK direct) — orthogonal; governs model calls, not tool function calls
- ADR 0004 (data-access layer) — connection pool that makes in-process concurrent calls safe
- ADR 0010 (check_citation matching contract) — what `check_citation` computes; this ADR governs only how it is called
- docs/plan/day5-design.md sections C, F.2
