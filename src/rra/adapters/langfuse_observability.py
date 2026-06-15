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

from typing import Any, Literal

import structlog

from rra.tracing import get_langfuse  # Only this adapter + exempted evals touch tracing.get_langfuse

log = structlog.get_logger(__name__)

# Stable Langfuse categorical-score name for a blocked security event (ADR 0024).
# One name → filter/group every guardrail block in the Langfuse Scores view.
_SECURITY_SCORE_NAME = "security.guardrail_block"


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

    def record_security_event(
        self,
        *,
        boundary: Literal["user_input", "retrieved_content"],
        categories: tuple[str, ...] = (),
        detector_score: float | None = None,
        reason: str | None = None,
        location: str | None = None,
    ) -> None:
        """Emit a blocked security event as a categorical Langfuse score.

        Reuses the same ``create_score`` surface as the eval harness
        (langfuse_eval.emit_scores), so a caught injection shows up — and is
        FILTERABLE — in the Langfuse Scores view under ``security.guardrail_block``.

        Trace attachment:
          - Inside a live request trace (retrieved_content during /query): the
            score hangs on that trace, in context with the agent spans.
          - No active trace (user_input blocked before the request span opens): a
            one-shot observation is opened so the incident still has a home trace
            rather than an orphaned score.

        Metadata-only (RT-redteam.md): boundary, detector categories/score, and a
        corpus location may be recorded; the offending TEXT never is. Never raises
        — observability must not break the request path.
        """
        client = self._client
        if client is None:  # langfuse disabled; nothing to record
            return

        metadata: dict[str, Any] = {
            "boundary": boundary,
            "categories": list(categories),
        }
        if detector_score is not None:
            metadata["detector_score"] = round(float(detector_score), 4)
        if location is not None:
            metadata["location"] = location

        try:
            if client.get_current_trace_id() is not None:
                self._emit_security_score(client, boundary, reason, metadata)
            else:
                # No active trace: open a one-shot observation so the score has a
                # home trace instead of orphaning.
                with client.start_as_current_observation(
                    name=_SECURITY_SCORE_NAME,
                    as_type="span",
                    input={"boundary": boundary},
                    metadata=metadata,
                ):
                    self._emit_security_score(client, boundary, reason, metadata)
        except Exception:  # noqa: BLE001 — observability must never fail a request
            log.warning("observability.security_event_failed", boundary=boundary)

    @staticmethod
    def _emit_security_score(
        client: Any,
        boundary: str,
        reason: str | None,
        metadata: dict[str, Any],
    ) -> None:
        """Create the categorical security score on the CURRENT trace."""
        client.create_score(
            name=_SECURITY_SCORE_NAME,
            value=boundary,  # CATEGORICAL: "user_input" | "retrieved_content"
            data_type="CATEGORICAL",
            trace_id=client.get_current_trace_id(),
            comment=reason,
            metadata=metadata,
        )
