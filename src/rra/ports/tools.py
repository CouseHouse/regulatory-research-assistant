"""Tool transport port: stable protocol for MCP tool dispatch.

Design note (ADR 0011):
  Tool functions live in src/rra/mcp_server/tools.py as plain Python (the
  in-process adapter).  The MCP server (server.py) is ALREADY an exposure
  adapter that calls those functions directly — do not touch it.

  This port abstracts the CALL SHAPE, not the function implementations.  Two
  motivations encoded here:

  (a) MCP-native call shape: ``call_tool(tool, arguments)`` is the exact
      signature the MCP protocol uses for tool invocation over HTTP or stdio.
      A remote-MCP adapter implements the same Protocol without changing agent
      code — agent logic calls ``get_tool_transport().call_tool("search_corpus",
      {"query": ..., "k": ...})`` regardless of whether the tool runs in-process
      or over an HTTP transport.

  (b) Single dispatch point for identity/NHI enforcement: every tool invocation
      MUST flow through ``call_tool``.  The identity/NHI port (next sub-phase)
      will install a deny-by-default tool-scope check here.  Scattering direct
      function calls across agents would require instrumenting each call site
      individually; the single chokepoint makes the enforcement trivially cheap.

  Return types: ``call_tool`` returns the tool's NATIVE result object
  (``SearchCorpusResult``, ``CitationCheckResult``, ``FetchGuidanceResult``,
  ``ListRecentGuidancesResult``).  Callers may narrow the return with ``assert
  isinstance(result, ...)`` or a cast for mypy; no new DTOs are introduced.

Factory:
  ``get_tool_transport()`` is the only entry point.  Profile-resolved and
  lru_cache'd to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from rra.config import settings


class ToolTransportPort(Protocol):
    """Protocol for MCP tool dispatch.

    The single method ``call_tool`` is the MCP-native call shape: a tool name
    and a ``dict`` of arguments.  Every agent tool invocation MUST flow through
    this chokepoint — it is where identity/NHI deny-by-default scope enforcement
    will be installed in the next sub-phase.
    """

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke *tool* with *arguments* and return the native result object.

        The return type is the tool's native result (SearchCorpusResult,
        CitationCheckResult, etc.) typed as ``Any`` so the port does not import
        mcp_server.tools at definition time.  Callers narrow as needed.

        Raises:
            ToolError: when the underlying tool function raises (propagated
                unchanged so the caller's existing ToolError handling works).
            KeyError / similar: if *tool* is not a known tool name.
        """
        ...  # pragma: no cover


@lru_cache(maxsize=1)
def get_tool_transport() -> ToolTransportPort:
    """Return the profile-resolved tool transport adapter (process-lifetime singleton).

    Adapter modules are imported lazily so future remote-MCP adapters (boto3
    Bedrock AgentCore, Vertex, Azure Foundry) are not imported in the local
    profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.inprocess_tools import InProcessToolTransport  # lazy

        return InProcessToolTransport()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
