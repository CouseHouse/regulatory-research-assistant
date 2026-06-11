"""Memory/state port: stable protocol that graph.py targets for checkpointing.

Design note:
  LangGraph's PostgresSaver is the wire type here — it is both the LangGraph
  checkpointer interface and the concrete type graph.py uses.  This is
  analogous to how the LLM port uses ``anthropic.types.Message`` as its wire
  type: the external library type IS the contract, so we use ``Any`` in the
  Protocol to avoid importing LangGraph at the port boundary while still
  allowing graph.py to narrow to ``PostgresSaver`` at call-site.

  The single method ``checkpointer()`` returns the process-lifetime
  LangGraph-compatible checkpointer (typed Any / BaseCheckpointSaver).

Factory:
  ``get_state()`` is the only entry point.  Profile-resolved and lru_cache'd
  to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Protocol

from rra.config import settings


class StatePort(Protocol):
    """Protocol for memory/state (LangGraph checkpointer) access."""

    def checkpointer(self) -> Any:
        """Return the process-lifetime LangGraph-compatible checkpointer.

        Typed ``Any`` so that the port does not import LangGraph at definition
        time.  graph.py can use the returned object as ``PostgresSaver``
        because it already imports that type.
        """
        ...  # pragma: no cover


@lru_cache(maxsize=1)
def get_state() -> StatePort:
    """Return the profile-resolved state adapter (process-lifetime singleton).

    Adapter modules are imported lazily here so that future cloud adapters
    are not imported in the local profile.
    """
    profile = settings.rra_profile

    if profile == "local":
        from rra.adapters.postgres_state import PostgresStateAdapter  # lazy

        return PostgresStateAdapter()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
