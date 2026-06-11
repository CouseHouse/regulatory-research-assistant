"""Local-profile identity/NHI adapter.

Implements IdentityPort with a static registry of agent principals and a
constant-time API-key check against settings.rra_api_key.

This adapter is intentionally minimal:
  - No external service calls (local profile = free, self-hosted).
  - Scopes are exactly the tools each agent actually calls today:
      planner    → no tools
      researcher → search_corpus
      analyst    → no tools
      critic     → check_citation
  - Unknown roles raise KeyError (fail-closed; never synthesize a permissive
    principal).
  - verify_api_caller uses secrets.compare_digest to prevent timing attacks;
    the presented key is NEVER logged.

Cloud profiles (aws / azure / gcp) will replace this adapter in the cloud-
adapter phase with the appropriate managed-identity service (AWS IAM, Azure
Managed Identity, GCP Workload Identity).
"""
from __future__ import annotations

import secrets

import structlog

from rra.config import settings
from rra.ports.identity import Principal

log = structlog.get_logger(__name__)

# ─── Static agent registry ────────────────────────────────────────────────────
#
# Scopes are verified against the actual call sites in the agent modules:
#   researcher.py  calls search_corpus    via get_tool_transport().call_tool(...)
#   critic.py      calls check_citation   via get_tool_transport().call_tool(...)
#                  (routed through the chokepoint in commit 0ad76b4 — its scope
#                  is load-bearing, not speculative)
#
# planner and analyst call no tools → empty frozenset (deny-by-default).
#
# Trust model (ADR 0021): in the local adapter these principals are ADVISORY
# intra-process scoping — authorize_tool trusts the caller-supplied Principal,
# and any in-process code could request another role's principal. The enforced
# boundary in the local profile is the HTTP API key; non-forgeable per-agent
# identity arrives with the cloud adapters (managed identities / workload
# identity), behind this same port.

_AGENT_REGISTRY: dict[str, Principal] = {
    "planner": Principal("planner", "agent", frozenset()),
    "researcher": Principal("researcher", "agent", frozenset({"search_corpus"})),
    "analyst": Principal("analyst", "agent", frozenset()),
    "critic": Principal("critic", "agent", frozenset({"check_citation"})),
}

_API_CLIENT = Principal("api-client", "service", frozenset({"query"}))


class LocalIdentityAdapter:
    """IdentityPort backed by a static registry for the local profile."""

    def verify_api_caller(self, presented_key: str) -> Principal | None:
        """Constant-time comparison of the presented key against settings.rra_api_key.

        Returns the api-client Principal on match, None on mismatch.
        The presented key is NEVER logged.
        """
        expected = settings.rra_api_key.get_secret_value()
        # secrets.compare_digest operates on bytes; encode both sides explicitly.
        match = secrets.compare_digest(
            presented_key.encode("utf-8"),
            expected.encode("utf-8"),
        )
        if match:
            return _API_CLIENT
        return None

    def agent_principal(self, role: str) -> Principal:
        """Return the NHI Principal for *role*.

        Raises:
            KeyError: If *role* is not in the static registry.
        """
        # dict.__getitem__ raises KeyError naturally — fail-closed.
        return _AGENT_REGISTRY[role]

    def authorize_tool(self, principal: Principal, tool: str) -> bool:
        """Return True iff *tool* is in *principal*.scopes.

        Deny-by-default: returns False for any pair not explicitly listed.
        """
        return tool in principal.scopes
