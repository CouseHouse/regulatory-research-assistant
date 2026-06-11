"""In-process tool transport adapter for the local profile.

Implements ToolTransportPort by dispatching directly to the tool functions in
rra.mcp_server.tools.  The registry maps the four public tool names to their
Python callables.  call_tool looks up and calls with **arguments.

This is intentionally thin — zero business logic lives here.  The MCP server
(server.py) already calls the same functions directly for external clients; that
path stays untouched.

Return types are the tools' native result objects (SearchCorpusResult,
CitationCheckResult, etc.) — callers narrow them as needed.  ToolError
propagates unchanged: the caller's existing ToolError handling logic must not
be bypassed by this adapter layer.
"""
from __future__ import annotations

from typing import Any

from rra.mcp_server.tools import (
    ToolError,
    check_citation,
    fetch_guidance,
    list_recent_guidances,
    search_corpus,
)


class InProcessToolTransport:
    """ToolTransportPort backed by direct in-process function calls."""

    _REGISTRY: dict[str, Any] = {
        "search_corpus": search_corpus,
        "fetch_guidance": fetch_guidance,
        "check_citation": check_citation,
        "list_recent_guidances": list_recent_guidances,
    }

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Look up *tool* in the registry and call it with **arguments.

        Raises ToolError (from mcp_server.tools) on infrastructure failures
        exactly as the direct-call path does — no wrapping.

        Raises ToolError("UNKNOWN", ...) if *tool* is not in the registry.
        The UNKNOWN code is retryable=False because an unknown name is a
        programming error, not a transient failure.
        """
        fn = self._REGISTRY.get(tool)
        if fn is None:
            raise ToolError(
                code="UNKNOWN",
                message=f"Unknown tool {tool!r}. "
                f"Known tools: {sorted(self._REGISTRY)}",
                tool=tool,
                retryable=False,
            )
        return fn(**arguments)
