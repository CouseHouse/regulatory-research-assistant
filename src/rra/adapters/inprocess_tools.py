"""In-process tool transport adapter for the local profile.

Implements ToolTransportPort by dispatching directly to the tool functions in
rra.mcp_server.tools.  The registry maps the four public tool names to their
Python callables.  call_tool checks authorization FIRST, then looks up and
calls with **arguments.

Authorization ordering (security-critical):
  1. get_identity().authorize_tool(principal, tool) is consulted BEFORE the
     registry lookup.  On deny, ToolAccessDenied is raised and the tool
     function is NEVER called.  This means an unknown tool name raises
     ToolAccessDenied (not an "unknown-tool" error) when the calling principal
     lacks a scope — tool-name probing requires a valid scope first.
  2. Only once authorization passes does the registry lookup run.  An unknown
     tool name at this point raises ToolError(code="UNKNOWN").
  3. The authorized, known tool function is called with **arguments.

On access denial a structured event ``tool.access_denied`` is logged with ONLY
``principal=principal.name`` and ``tool=tool`` — NO arguments, NO content.

This is intentionally thin — zero business logic lives here.  The MCP server
(server.py) already calls the same functions directly for external clients; that
path stays untouched.

Return types are the tools' native result objects (SearchCorpusResult,
CitationCheckResult, etc.) — callers narrow them as needed.  ToolError
propagates unchanged: the caller's existing ToolError handling logic must not
be bypassed by this adapter layer.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from rra.mcp_server.tools import (
    ToolError,
    check_citation,
    fetch_guidance,
    list_recent_guidances,
    search_corpus,
)
from rra.ports.identity import ToolAccessDenied

if TYPE_CHECKING:
    from rra.ports.identity import Principal

log = structlog.get_logger(__name__)


class InProcessToolTransport:
    """ToolTransportPort backed by direct in-process function calls.

    Authorization is enforced BEFORE dispatch: see module-level ordering note.
    """

    _REGISTRY: dict[str, Any] = {
        "search_corpus": search_corpus,
        "fetch_guidance": fetch_guidance,
        "check_citation": check_citation,
        "list_recent_guidances": list_recent_guidances,
    }

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        principal: "Principal",
    ) -> Any:
        """Authorize then dispatch *tool* on behalf of *principal*.

        Step 1 — Authorization (BEFORE registry lookup):
            get_identity().authorize_tool(principal, tool) is called.  On
            deny, logs ``tool.access_denied`` (principal.name + tool only —
            NO arguments) and raises ToolAccessDenied.  An unknown tool name
            that fails authorization also raises ToolAccessDenied here, not
            an "unknown tool" error — this is the intended ordering.

        Step 2 — Registry lookup (only if authorized):
            If the tool is not in the registry, raises ToolError("UNKNOWN").

        Step 3 — Dispatch:
            Calls the registered function with **arguments.  ToolError raised
            by the function propagates unchanged.
        """
        # ── Step 1: Authorization (deny-by-default, before existence check) ──
        from rra.ports.identity import get_identity

        identity = get_identity()
        if not identity.authorize_tool(principal, tool):
            log.warning(
                "tool.access_denied",
                principal=principal.name,
                tool=tool,
                # Intentionally omit: arguments, content, query text.
            )
            raise ToolAccessDenied(principal=principal.name, tool=tool)

        # ── Step 2: Registry lookup (only reached after authorization) ────────
        fn = self._REGISTRY.get(tool)
        if fn is None:
            raise ToolError(
                code="UNKNOWN",
                message=f"Unknown tool {tool!r}. "
                f"Known tools: {sorted(self._REGISTRY)}",
                tool=tool,
                retryable=False,
            )

        # ── Step 3: Dispatch ──────────────────────────────────────────────────
        return fn(**arguments)
