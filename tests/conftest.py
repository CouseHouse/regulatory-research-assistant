"""Session-wide test-environment isolation.

Ensures the test suite is a no-op with respect to external services:
  - Langfuse: keys are nulled so get_langfuse() returns None for every test.
    Tests that need a mock client use patch("rra.tracing.get_langfuse").
  - CRITIC_FORCE_VERDICT: neutralised at session scope so ambient shell/.env
    values don't leak.  Individual tests that need a forced verdict set it with
    monkeypatch.setattr(settings, "critic_force_verdict", ...) as usual.

Conftest ordering vs pydantic-settings precedence
--------------------------------------------------
pydantic-settings loads sources in this order (highest → lowest priority):
  1. Environment variables (os.environ)
  2. .env file
  3. Field defaults

`os.environ.setdefault` writes to source 1, so it wins over the .env file.
For developers who have real secrets in .env, the stub values would shadow the
real keys and cause live-DB integration tests to fail unless keys were manually
exported into the shell first.

Fix: before applying stubs we seed os.environ from the project .env (if it
exists) for the four required-secret fields.  The stubs then only apply when
the key is absent from BOTH the shell AND the .env file, which is exactly the
CI case (no .env, no exported keys → stubs apply → CI passes).

python-dotenv (dotenv_values) is a direct dependency of pydantic-settings, so
it is always available.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Seed os.environ from project .env BEFORE applying stub defaults.
# This preserves real developer credentials for integration tests while
# keeping CI working (CI has no .env → stubs apply).
# ---------------------------------------------------------------------------

_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
_REQUIRED_SECRET_KEYS = (
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "POSTGRES_PASSWORD",
    "RRA_API_KEY",
)

if _DOTENV_PATH.exists():
    try:
        from dotenv import dotenv_values as _dotenv_values

        _dotenv_env = _dotenv_values(_DOTENV_PATH)
        for _key in _REQUIRED_SECRET_KEYS:
            if _key not in os.environ and _key in _dotenv_env:
                _val = _dotenv_env[_key]
                if _val is not None:
                    os.environ[_key] = _val
    except Exception:  # noqa: BLE001
        # Parsing failed; fall through to stubs — this is a best-effort seed.
        pass

# Stub defaults: apply only when the key is absent from both shell and .env.
# These are the CI defaults — real values should come from .env or the shell.
# These must be set BEFORE any test module imports rra.config, which builds the
# Settings() singleton at import. pytest imports this conftest before collecting
# test modules, so these module-level setdefaults win the race.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")
os.environ.setdefault("POSTGRES_PASSWORD", "test-postgres-password")
os.environ.setdefault("RRA_API_KEY", "dev-key-change-me")


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
