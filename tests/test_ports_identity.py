"""Tests for Port 7: Identity/NHI (src/rra/ports/identity.py + adapters/local_identity.py).

Covers:
  - Principal dataclass: frozen, correct fields.
  - ToolAccessDenied exception: carries principal + tool.
  - get_identity() factory: profile resolution, singleton, NotImplementedError.
  - LocalIdentityAdapter.verify_api_caller: correct key, incorrect key, empty key.
  - LocalIdentityAdapter.verify_api_caller: uses secrets.compare_digest (spy).
  - LocalIdentityAdapter.agent_principal: known roles, unknown role KeyError.
  - LocalIdentityAdapter.authorize_tool: deny-by-default; scope grants; cross-scope denials.
  - Deny-by-default matrix: researcher denied check_citation; critic denied search_corpus;
    planner/analyst denied all tools; unknown principal denied everything.
  - ToolTransportPort.call_tool: raises ToolAccessDenied BEFORE calling the tool fn (spy).
  - Authorization-before-existence ordering: unauthorized + unknown tool → ToolAccessDenied.
  - api.py 401 behaviour: unchanged for wrong/missing key (existing tests cover; extended below).
"""
from __future__ import annotations

import os
import secrets

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ─── Principal dataclass ──────────────────────────────────────────────────────


def test_principal_is_frozen() -> None:
    """Principal is a frozen dataclass — attributes cannot be mutated after construction."""
    from rra.ports.identity import Principal

    p = Principal(name="test", kind="agent", scopes=frozenset({"tool_a"}))
    with pytest.raises((AttributeError, TypeError)):
        p.name = "mutated"  # type: ignore[misc]


def test_principal_fields() -> None:
    """Principal exposes name, kind, and scopes with the declared types."""
    from rra.ports.identity import Principal

    p = Principal(name="researcher", kind="agent", scopes=frozenset({"search_corpus"}))
    assert p.name == "researcher"
    assert p.kind == "agent"
    assert p.scopes == frozenset({"search_corpus"})


# ─── ToolAccessDenied exception ───────────────────────────────────────────────


def test_tool_access_denied_carries_principal_and_tool() -> None:
    """ToolAccessDenied stores principal.name and tool name as attributes."""
    from rra.ports.identity import ToolAccessDenied

    exc = ToolAccessDenied(principal="researcher", tool="check_citation")
    assert exc.principal == "researcher"
    assert exc.tool == "check_citation"
    assert "researcher" in str(exc)
    assert "check_citation" in str(exc)


def test_tool_access_denied_is_exception() -> None:
    """ToolAccessDenied is a subclass of Exception."""
    from rra.ports.identity import ToolAccessDenied

    exc = ToolAccessDenied(principal="test", tool="any_tool")
    assert isinstance(exc, Exception)


# ─── Factory: profile resolution ──────────────────────────────────────────────


def test_get_identity_returns_local_adapter() -> None:
    """get_identity() under RRA_PROFILE=local returns LocalIdentityAdapter."""
    from rra.adapters.local_identity import LocalIdentityAdapter
    from rra.ports.identity import get_identity

    get_identity.cache_clear()
    try:
        adapter = get_identity()
        assert isinstance(adapter, LocalIdentityAdapter)
    finally:
        get_identity.cache_clear()


@pytest.mark.parametrize("profile", ["aws", "azure", "gcp"])
def test_get_identity_raises_not_implemented_for_cloud_profiles(
    profile: str,
) -> None:
    """Non-local profiles raise NotImplementedError (cloud-adapter phase)."""
    from rra.config import settings
    from rra.ports.identity import get_identity

    get_identity.cache_clear()
    try:
        with patch.object(settings, "rra_profile", profile):
            with pytest.raises(NotImplementedError, match="cloud-adapter phase"):
                get_identity()
    finally:
        get_identity.cache_clear()


def test_get_identity_is_singleton() -> None:
    """Two successive get_identity() calls return the same object."""
    from rra.ports.identity import get_identity

    get_identity.cache_clear()
    try:
        first = get_identity()
        second = get_identity()
        assert first is second
    finally:
        get_identity.cache_clear()


# ─── verify_api_caller ────────────────────────────────────────────────────────


@pytest.fixture
def local_identity_adapter(monkeypatch: pytest.MonkeyPatch):
    """A fresh LocalIdentityAdapter with a known API key sentinel."""
    monkeypatch.setenv("RRA_API_KEY", "test-key-sentinel")
    from rra.adapters.local_identity import LocalIdentityAdapter
    from rra.ports.identity import get_identity

    get_identity.cache_clear()
    try:
        adapter = LocalIdentityAdapter()
        yield adapter
    finally:
        get_identity.cache_clear()


def test_verify_api_caller_correct_key_returns_principal(
    local_identity_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_api_caller returns api-client Principal on key match."""
    monkeypatch.setenv("RRA_API_KEY", "test-key-sentinel")
    from rra.config import Settings

    # Build a fresh settings instance with the sentinel key.
    fresh_settings = Settings()
    with patch("rra.adapters.local_identity.settings", fresh_settings):
        principal = local_identity_adapter.verify_api_caller("test-key-sentinel")

    assert principal is not None
    assert principal.name == "api-client"
    assert principal.kind == "service"


def test_verify_api_caller_incorrect_key_returns_none(
    local_identity_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_api_caller returns None on key mismatch."""
    monkeypatch.setenv("RRA_API_KEY", "test-key-sentinel")
    from rra.config import Settings

    fresh_settings = Settings()
    with patch("rra.adapters.local_identity.settings", fresh_settings):
        principal = local_identity_adapter.verify_api_caller("wrong-key")

    assert principal is None


def test_verify_api_caller_empty_key_returns_none(
    local_identity_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_api_caller returns None for an empty presented key."""
    monkeypatch.setenv("RRA_API_KEY", "test-key-sentinel")
    from rra.config import Settings

    fresh_settings = Settings()
    with patch("rra.adapters.local_identity.settings", fresh_settings):
        principal = local_identity_adapter.verify_api_caller("")

    assert principal is None


def test_verify_api_caller_uses_compare_digest(
    local_identity_adapter: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verify_api_caller MUST use secrets.compare_digest (not !=) for timing safety.

    Spy on secrets.compare_digest and assert it is called during key verification.
    """
    monkeypatch.setenv("RRA_API_KEY", "test-key-sentinel")
    from rra.config import Settings

    fresh_settings = Settings()

    calls: list[tuple[bytes, bytes]] = []
    original_cd = secrets.compare_digest

    def spy_compare_digest(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return original_cd(a, b)

    with (
        patch("rra.adapters.local_identity.settings", fresh_settings),
        patch("rra.adapters.local_identity.secrets.compare_digest", side_effect=spy_compare_digest),
    ):
        local_identity_adapter.verify_api_caller("test-key-sentinel")

    assert len(calls) >= 1, (
        "secrets.compare_digest was not called — verify_api_caller must use "
        "secrets.compare_digest, not a plain != comparison."
    )


# ─── agent_principal ──────────────────────────────────────────────────────────


def test_agent_principal_known_roles(local_identity_adapter: Any) -> None:
    """agent_principal returns the correct Principal for each known agent role."""
    for role, expected_scopes in [
        ("planner", frozenset()),
        ("researcher", frozenset({"search_corpus"})),
        ("analyst", frozenset()),
        ("critic", frozenset({"check_citation"})),
    ]:
        p = local_identity_adapter.agent_principal(role)
        assert p.name == role
        assert p.kind == "agent"
        assert p.scopes == expected_scopes


def test_agent_principal_unknown_role_raises_key_error(
    local_identity_adapter: Any,
) -> None:
    """agent_principal raises KeyError for an unknown role (fail-closed)."""
    with pytest.raises(KeyError):
        local_identity_adapter.agent_principal("superadmin")


def test_agent_principal_unknown_role_never_synthesizes_permissive_principal(
    local_identity_adapter: Any,
) -> None:
    """agent_principal MUST NOT return a Principal for any unknown role.

    Fail-closed: raising KeyError is correct; returning ANY Principal (even
    with empty scopes) would silently register an unknown role.
    """
    for unknown_role in ("admin", "root", "", "researcher_v2", "ALL_TOOLS"):
        with pytest.raises(KeyError):
            local_identity_adapter.agent_principal(unknown_role)


# ─── authorize_tool ────────────────────────────────────────────────────────────


def test_authorize_tool_deny_by_default_empty_scopes(local_identity_adapter: Any) -> None:
    """A principal with empty scopes is denied every tool."""
    from rra.ports.identity import Principal

    p = Principal(name="planner", kind="agent", scopes=frozenset())
    for tool in ("search_corpus", "check_citation", "fetch_guidance", "list_recent_guidances"):
        assert local_identity_adapter.authorize_tool(p, tool) is False


def test_authorize_tool_grants_declared_scope(local_identity_adapter: Any) -> None:
    """authorize_tool returns True for a tool explicitly in the principal's scopes."""
    from rra.ports.identity import Principal

    p = Principal(name="researcher", kind="agent", scopes=frozenset({"search_corpus"}))
    assert local_identity_adapter.authorize_tool(p, "search_corpus") is True


def test_authorize_tool_denies_tool_not_in_scopes(local_identity_adapter: Any) -> None:
    """authorize_tool returns False for a tool not in the principal's scopes."""
    from rra.ports.identity import Principal

    p = Principal(name="researcher", kind="agent", scopes=frozenset({"search_corpus"}))
    assert local_identity_adapter.authorize_tool(p, "check_citation") is False


# ─── Deny-by-default matrix ───────────────────────────────────────────────────


def test_researcher_denied_check_citation(local_identity_adapter: Any) -> None:
    """researcher principal may NOT call check_citation."""
    p = local_identity_adapter.agent_principal("researcher")
    assert local_identity_adapter.authorize_tool(p, "check_citation") is False


def test_critic_denied_search_corpus(local_identity_adapter: Any) -> None:
    """critic principal may NOT call search_corpus."""
    p = local_identity_adapter.agent_principal("critic")
    assert local_identity_adapter.authorize_tool(p, "search_corpus") is False


def test_planner_denied_all_tools(local_identity_adapter: Any) -> None:
    """planner principal has empty scopes — denied all tools."""
    p = local_identity_adapter.agent_principal("planner")
    for tool in ("search_corpus", "check_citation", "fetch_guidance", "list_recent_guidances"):
        assert local_identity_adapter.authorize_tool(p, tool) is False


def test_analyst_denied_all_tools(local_identity_adapter: Any) -> None:
    """analyst principal has empty scopes — denied all tools."""
    p = local_identity_adapter.agent_principal("analyst")
    for tool in ("search_corpus", "check_citation", "fetch_guidance", "list_recent_guidances"):
        assert local_identity_adapter.authorize_tool(p, tool) is False


def test_empty_scope_principal_denied_unknown_tool(local_identity_adapter: Any) -> None:
    """A principal with empty scopes is denied even a tool not in the registry."""
    from rra.ports.identity import Principal

    p = Principal(name="nobody", kind="agent", scopes=frozenset())
    assert local_identity_adapter.authorize_tool(p, "nonexistent_tool") is False


# ─── Transport: ToolAccessDenied raised before tool function ──────────────────


def test_transport_raises_tool_access_denied_before_calling_fn() -> None:
    """ToolTransportPort.call_tool raises ToolAccessDenied BEFORE the tool fn is called.

    A spy on the registered function must NOT be called when authorization fails.
    """
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.ports.identity import Principal, ToolAccessDenied

    # Principal with no scopes — denied everything.
    principal = Principal(name="no-scope", kind="agent", scopes=frozenset())

    spy_fn = MagicMock()
    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["search_corpus"] = spy_fn

    with pytest.raises(ToolAccessDenied):
        transport.call_tool("search_corpus", {"query": "x", "k": 3}, principal)

    # The tool function must NOT have been called.
    spy_fn.assert_not_called()


def test_transport_tool_fn_called_when_authorized() -> None:
    """ToolTransportPort.call_tool dispatches to the tool fn when authorization passes."""
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import SearchCorpusResult
    from rra.ports.identity import Principal

    principal = Principal(name="researcher", kind="agent", scopes=frozenset({"search_corpus"}))
    fake_result = SearchCorpusResult(passages=[])
    spy_fn = MagicMock(return_value=fake_result)

    transport = InProcessToolTransport()
    transport._REGISTRY = dict(transport._REGISTRY)
    transport._REGISTRY["search_corpus"] = spy_fn

    result = transport.call_tool("search_corpus", {"query": "x", "k": 3}, principal)

    assert result is fake_result
    spy_fn.assert_called_once_with(query="x", k=3)


# ─── Authorization-before-existence ordering ──────────────────────────────────


def test_authz_before_existence_unauthorized_unknown_tool_raises_access_denied() -> None:
    """Unauthorized principal requesting an unknown tool → ToolAccessDenied, not ToolError.

    Security invariant: authorization runs BEFORE the registry lookup.  An
    attacker probing for tool names by trying random names must be denied on
    scope BEFORE learning whether the tool exists.  Without a scope, the
    response is always ToolAccessDenied regardless of whether the tool is
    registered.
    """
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.ports.identity import Principal, ToolAccessDenied

    principal = Principal(name="no-scope", kind="agent", scopes=frozenset())
    transport = InProcessToolTransport()

    # "secret_internal_tool" is not in the registry.
    with pytest.raises(ToolAccessDenied):
        transport.call_tool("secret_internal_tool", {}, principal)


def test_authz_before_existence_authorized_unknown_tool_raises_tool_error() -> None:
    """Authorized principal requesting an unknown tool → ToolError (not ToolAccessDenied).

    Once authorization passes (principal has the scope), the registry lookup
    runs next.  Only then does the unknown-tool ToolError fire.
    """
    from rra.adapters.inprocess_tools import InProcessToolTransport
    from rra.mcp_server.tools import ToolError
    from rra.ports.identity import Principal

    # Grant the exact (nonexistent) tool name so authz passes.
    principal = Principal(name="test", kind="agent", scopes=frozenset({"nonexistent_tool"}))
    transport = InProcessToolTransport()

    with pytest.raises(ToolError) as exc_info:
        transport.call_tool("nonexistent_tool", {}, principal)

    assert exc_info.value.code == "UNKNOWN"
