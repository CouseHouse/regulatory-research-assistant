"""Langfuse instrumentation singleton.

Returns the live Langfuse client when settings.langfuse_enabled is True,
or None otherwise.

USAGE RESTRICTION (ports/adapters refactor):
  After Port 6 (observability) landed, get_langfuse() is the INTERNAL source
  used ONLY by:
    - rra.adapters.langfuse_observability  — the observability adapter
    - rra.evals.run                        — dataset features are Langfuse-specific
    - rra.evals.langfuse_eval              — same exemption
    - rra.evals.judge                      — same exemption

  All other modules MUST import get_observability() from rra.ports.observability
  instead.  Direct get_langfuse() calls outside the four exempted modules are
  a violation of the port boundary.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from rra.config import settings


@lru_cache(maxsize=1)
def get_langfuse() -> Any:
    """Return the process-lifetime Langfuse client, or None if disabled.

    Cached so the client (and its background flush thread) is created once
    per process. Returns None immediately when langfuse_enabled is False so
    all call sites can treat None as a no-op gate without extra try/except.
    """
    if not settings.langfuse_enabled:
        return None

    from langfuse import Langfuse  # local import; keep langfuse off the cold-path

    return Langfuse(
        public_key=settings.langfuse_public_key.get_secret_value(),  # type: ignore[union-attr]
        secret_key=settings.langfuse_secret_key.get_secret_value(),  # type: ignore[union-attr]
        host=settings.langfuse_host,
    )
