"""Langfuse instrumentation singleton.

Returns the live Langfuse client when settings.langfuse_enabled is True,
or None otherwise. All callers must guard against None — that is the
disabled/no-op path, not an error condition.

Usage:
    from rra.tracing import get_langfuse

    lf = get_langfuse()
    if lf is not None:
        trace = lf.trace(name="query", input={"q": q})
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
