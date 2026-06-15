"""Tool transport port: stable protocol for MCP tool dispatch.

Design note (ADR 0011):
  Tool functions live in src/rra/mcp_server/tools.py as plain Python (the
  in-process adapter).  The MCP server (server.py) is ALREADY an exposure
  adapter that calls those functions directly — do not touch it.

  This port abstracts the CALL SHAPE, not the function implementations.  Two
  motivations encoded here:

  (a) MCP-native call shape: ``call_tool(tool, arguments, principal)`` is the
      MCP-native call shape extended with the caller's Principal so that every
      tool invocation carries its authorization context.  A remote-MCP adapter
      implements the same Protocol without changing agent code — agent logic
      calls ``get_tool_transport().call_tool("search_corpus", {...}, principal)``
      regardless of whether the tool runs in-process or over an HTTP transport.

  (b) Single dispatch point for identity/NHI enforcement: every tool invocation
      MUST flow through ``call_tool``.  The identity port's deny-by-default scope
      check is installed here.  Scattering direct function calls across agents
      would require instrumenting each call site individually; the single
      chokepoint makes the enforcement trivially cheap.

  Ordering guarantee (security-critical):
    Authorization (scope check via get_identity().authorize_tool) is performed
    BEFORE the tool registry is consulted.  This means an unknown tool name
    raises ToolAccessDenied (not an "unknown tool" error) when the calling
    principal lacks the relevant scope.  Only once authorization passes does
    the unknown-tool check run.  This prevents tool-name probing without a
    valid scope.

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
from typing import TYPE_CHECKING, Any, Protocol

from rra.config import settings

if TYPE_CHECKING:
    from rra.ports.identity import Principal


class ToolTransportPort(Protocol):
    """Protocol for MCP tool dispatch with identity/NHI authorization.

    The single method ``call_tool`` is the MCP-native call shape: a tool name,
    a ``dict`` of arguments, and the calling Principal.  Every agent tool
    invocation MUST flow through this chokepoint.

    Authorization ordering:
      1. scope check (authorize_tool) — deny raises ToolAccessDenied BEFORE
         the registry is consulted (prevents tool-name probing without scope).
      2. registry lookup — ToolError("UNKNOWN") only reached after authz passes.
      3. dispatch to the tool function.
    """

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        principal: "Principal",
    ) -> Any:
        """Invoke *tool* with *arguments* on behalf of *principal*.

        Args:
            tool:       The MCP tool name (e.g. "search_corpus").
            arguments:  Keyword arguments forwarded to the tool function.
            principal:  The authenticated NHI or service Principal making the
                        call.  Used for scope authorization.

        Returns:
            The tool's native result object (SearchCorpusResult, etc.).

        Raises:
            ToolAccessDenied: if *principal* does not have *tool* in its scopes
                (raised BEFORE the registry is consulted — see ordering note).
            ToolError: when the underlying tool function raises (propagated
                unchanged so the caller's existing ToolError handling works).
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
