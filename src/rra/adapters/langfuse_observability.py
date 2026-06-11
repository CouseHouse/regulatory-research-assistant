"""Langfuse observability adapter for the local profile.

Wraps the current Langfuse singleton (rra.tracing.get_langfuse) and delegates
every ObservabilityPort call to the corresponding Langfuse SDK call verbatim.

IMPORTANT: rra.tracing.get_langfuse is the ONLY place in the codebase that
constructs the Langfuse client.  This adapter calls it; it does NOT construct
a second client.  All other modules MUST use get_observability() (the port)
rather than get_langfuse() directly.  Exemptions:
  - rra.tracing       — defines and owns get_langfuse(); adapter source of truth
  - rra.evals.run     — Langfuse dataset features are eval-harness-specific
  - rra.evals.langfuse_eval — same exemption
  - rra.evals.judge   — same exemption
"""
from __future__ import annotations

from typing import Any

from rra.tracing import get_langfuse  # Only this adapter + exempted evals touch tracing.get_langfuse


class LangfuseObservabilityAdapter:
    """ObservabilityPort backed by the Langfuse SDK client.

    start_span delegates to ``lf.start_as_current_observation`` with all
    keyword arguments forwarded verbatim.  propagate_session delegates to
    ``langfuse.propagate_attributes(session_id=session_id)`` — required to
    populate the Langfuse Sessions view (v4 SDK; metadata alone does not work).
    """

    def __init__(self) -> None:
        # client is the live Langfuse instance from the singleton.
        self._client = get_langfuse()

    def start_span(
        self,
        name: str,
        *,
        as_type: str | None = None,
        input: Any = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Any:
        """Open a top-level Langfuse observation.

        Forwards all non-None kwargs to start_as_current_observation verbatim.
        Langfuse ignores unknown kwargs, so passing only what is set avoids
        polluting the trace with null attributes.
        """
        kwargs: dict[str, Any] = {"name": name}
        if as_type is not None:
            kwargs["as_type"] = as_type
        if input is not None:
            kwargs["input"] = input
        if session_id is not None:
            kwargs["session_id"] = session_id
        if metadata is not None:
            kwargs["metadata"] = metadata
        if model is not None:
            kwargs["model"] = model
        return self._client.start_as_current_observation(**kwargs)

    def current_trace_id(self) -> str | None:
        """Return the active Langfuse trace ID."""
        return self._client.get_current_trace_id()  # type: ignore[no-any-return]

    def flush(self) -> None:
        """Flush buffered Langfuse events to the server."""
        self._client.flush()

    def propagate_session(self, session_id: str) -> Any:
        """Return a context manager that propagates *session_id* to child spans.

        Uses ``langfuse.propagate_attributes(session_id=session_id)`` which is
        required for the Langfuse Sessions view in v4 to group spans under the
        correct session.  Imported lazily to keep langfuse off the cold-path
        when tracing is disabled.
        """
        from langfuse import propagate_attributes  # lazy — mirrors api.py's existing pattern

        return propagate_attributes(session_id=session_id)
