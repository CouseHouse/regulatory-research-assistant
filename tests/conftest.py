"""Session-wide test-environment isolation.

Ensures the test suite is a no-op with respect to external services:
  - Langfuse: keys are nulled so get_langfuse() returns None for every test.
    Tests that need a mock client use patch("rra.tracing.get_langfuse").
  - CRITIC_FORCE_VERDICT: neutralised at session scope so ambient shell/.env
    values don't leak.  Individual tests that need a forced verdict set it with
    monkeypatch.setattr(settings, "critic_force_verdict", ...) as usual.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_external_services() -> Generator[None, None, None]:
    """Null out Langfuse keys and critic_force_verdict for the whole session.

    Timing: session-scoped autouse fixtures run before any test function.
    get_langfuse() is lazy (never called at module import time), so the
    cache_clear() + key nulling happens before the first agent call in any test.

    Tests that need a mock Langfuse client should patch rra.tracing.get_langfuse
    directly.  Tests that need a forced verdict should use
    monkeypatch.setattr(settings, "critic_force_verdict", "revise") — their
    function-scoped monkeypatch overrides our None and restores it on teardown.
    """
    import rra.tracing as _tracing
    from rra.config import settings

    mp = pytest.MonkeyPatch()

    # Clear any client cached during collection-time imports.
    _tracing.get_langfuse.cache_clear()

    # Null keys → langfuse_enabled → False → get_langfuse() returns None.
    mp.setattr(settings, "langfuse_public_key", None)
    mp.setattr(settings, "langfuse_secret_key", None)

    # Neutralise any CRITIC_FORCE_VERDICT that leaked from .env or the shell.
    mp.setattr(settings, "critic_force_verdict", None)

    yield

    mp.undo()
    # Clear again so teardown leaves no live client referencing restored keys.
    _tracing.get_langfuse.cache_clear()
