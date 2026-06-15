"""Identity/NHI port: stable protocol for principal verification and tool authorization.

Design note (ADR 0021 — lead writes the ADR; this file is the implementation):
  Every boundary where the system verifies who is calling and what they are
  allowed to do flows through this port.  Two classes of principals are managed:

  1. Human/service callers of the HTTP API (verified via API key).
  2. Non-Human Identities (NHI) — the agent roles inside the graph
     (planner, researcher, analyst, critic).  Each role has a static,
     minimal scope: the set of tool names it is allowed to call.  Unknown
     roles raise KeyError (fail-closed; never synthesize a permissive
     principal on the fly).

  Authorization is deny-by-default: ``authorize_tool`` returns False for any
  (principal, tool) pair that is not in the principal's declared scopes.

Security invariants:
  - ``verify_api_caller`` uses ``secrets.compare_digest`` to prevent
    timing-side-channel attacks (fixes the ``!=`` compare in the original
    api.py ``_verify_api_key``).
  - The presented key is NEVER logged; only ``principal.name`` is logged on
    success / failure.
  - ``agent_principal`` raises ``KeyError`` for unknown roles rather than
    returning a default — fail closed, not fail open.
  - ``authorize_tool`` always returns ``bool``; no exception on deny.

Exception exported by this module:
  ``ToolAccessDenied`` — raised by ``InProcessToolTransport.call_tool`` when
  authorization is denied.  Defined here so it can be imported by both
  the transport adapter and tests without a circular dependency.

Ordering guarantee:
  In ``InProcessToolTransport.call_tool`` authorization is checked BEFORE the
  tool registry is consulted.  This means an unknown tool name raises
  ``ToolAccessDenied`` (not an "unknown tool" error) when the calling principal
  lacks a scope.  Only once authorization passes does the unknown-tool check
  run.  This prevents probing for tool existence without a valid scope.

Factory:
  ``get_identity()`` is the only entry point.  Profile-resolved and
  ``lru_cache``'d to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Protocol

from rra.config import settings


# ─── Data types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Principal:
    """An authenticated, authorized identity.

    Attributes:
        name:   Human-readable identifier (e.g. "researcher", "api-client").
        kind:   Taxonomy: "human" for interactive users, "service" for external
                service accounts, "agent" for in-graph NHI roles.
        scopes: The set of tool names this principal is allowed to call.  An
                empty frozenset means no tool access at all (deny-by-default).
    """

    name: str
    kind: Literal["human", "service", "agent"]
    scopes: frozenset[str]


# ─── Exception ────────────────────────────────────────────────────────────────


class ToolAccessDenied(Exception):
    """Raised when a principal attempts to call a tool outside its scopes.

    Also raised (intentionally) when the tool name is unknown AND the principal
    lacks a scope — authorization is checked before existence so that tool-name
    probing requires a valid scope first.
    """

    def __init__(self, principal: str, tool: str) -> None:
        self.principal = principal
        self.tool = tool
        super().__init__(
            f"Principal {principal!r} is not authorized to call tool {tool!r}"
        )


# ─── Port Protocol ────────────────────────────────────────────────────────────


class IdentityPort(Protocol):
    """Protocol for identity verification and authorization.

    The three methods cover:
      1. ``verify_api_caller`` — HTTP boundary: check the presented API key
         and return the api-client Principal on match, or None on mismatch.
         Uses constant-time comparison to prevent timing attacks.
      2. ``agent_principal`` — NHI lookup: return the static Principal for an
         agent role.  Unknown role → KeyError (fail-closed).
      3. ``authorize_tool`` — scope check: pure boolean, deny-by-default.
    """

    def verify_api_caller(self, presented_key: str) -> Principal | None:
        """Verify the HTTP caller's API key.

        Returns the api-client Principal if ``presented_key`` matches the
        configured ``settings.rra_api_key``.  Returns ``None`` on any
        mismatch — caller decides the HTTP response (401).

        MUST use ``secrets.compare_digest`` to prevent timing attacks.
        MUST NOT log the presented key.

        Args:
            presented_key: The raw key string from the X-API-Key header.

        Returns:
            Principal on success, None on mismatch.
        """
        ...  # pragma: no cover

    def agent_principal(self, role: str) -> Principal:
        """Return the NHI Principal for an agent role.

        Args:
            role: One of "planner", "researcher", "analyst", "critic".

        Raises:
            KeyError: If *role* is not in the static registry.  Fail-closed:
                never synthesize a Principal for an unknown role.
        """
        ...  # pragma: no cover

    def authorize_tool(self, principal: Principal, tool: str) -> bool:
        """Return True iff *principal* has *tool* in its declared scopes.

        Deny-by-default: any (principal, tool) pair not explicitly present in
        ``principal.scopes`` returns False.  No exception is raised here; the
        transport adapter raises ``ToolAccessDenied`` on False.

        Args:
            principal: The authenticated Principal.
            tool:      The tool name being requested.

        Returns:
            True if authorized, False otherwise.
        """
        ...  # pragma: no cover


# ─── Factory ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_identity() -> IdentityPort:
    """Return the profile-resolved identity adapter (process-lifetime singleton).

    Adapter modules are imported lazily so future cloud adapters (AWS IAM /
    Bedrock resource policy, Azure Managed Identity, GCP Workload Identity) are
    not imported in the local profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.local_identity import LocalIdentityAdapter  # lazy

        return LocalIdentityAdapter()

    raise NotImplementedError(
        f"{profile!r} identity adapter lands in the cloud-adapter phase"
    )
