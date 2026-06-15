"""Observability port: stable protocol that agent logic, api.py, and retrieval target.

Design note:
  The codebase currently calls Langfuse directly via ``get_langfuse()`` from
  rra.tracing.  Every call site follows the same pattern:

    lf = get_langfuse()                         # → Langfuse | None
    span_cm = (
        lf.start_as_current_observation(...)
        if lf is not None
        else contextlib.nullcontext(None)
    )
    with span_cm as span:
        ...
        if span is not None:
            span.update(...)

  This port abstracts that pattern behind two components:

  1. ``SpanHandle`` Protocol — the object yielded by ``start_span``; must
     support ``.update(**kwargs)`` so agent code can record output, usage, etc.
     The yielded type is ``Any`` in the context-manager signature for mypy
     compatibility, but the adapter documents that it yields a SpanHandle.

  2. ``ObservabilityPort`` Protocol — the port itself.  Methods cover exactly
     the used subset observed in the codebase:
       - ``start_span`` — the main instrumentation call (maps to
         ``lf.start_as_current_observation``); returns a ContextManager whose
         ``__enter__`` yields a SpanHandle.  Nested calls on the handle also
         return context managers of the same SpanHandle type.
       - ``current_trace_id`` — maps to ``lf.get_current_trace_id()``.
       - ``flush`` — maps to ``lf.flush()``.
       - ``propagate_session`` — maps to ``langfuse.propagate_attributes
         (session_id=...)``; wraps the full outer session context in api.py.

  ``NoopObservabilityAdapter`` eliminates the scattered ``if lf is not None``
  guards at call sites.  ``start_span`` yields a noop SpanHandle whose
  ``update()`` does nothing, so call sites become a clean unconditional
  ``with obs.start_span(...) as span: ... span.update(...)``.

  ``api.py``'s ``propagate_attributes`` import: this is a Langfuse-SDK-level
  context-manager that sets session_id on the active Langfuse context.  It is
  modelled as ``propagate_session(session_id)`` on the port so the adapter can
  call the Langfuse API and the noop adapter returns a do-nothing context.

Factory:
  ``get_observability()`` is the only entry point.  Profile-resolved and
  lru_cache'd to a process-lifetime singleton.  Non-local profiles raise
  ``NotImplementedError`` until the cloud-adapter phase lands.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Generator, Literal, Protocol

from rra.config import settings


# ─── SpanHandle Protocol ──────────────────────────────────────────────────────


class SpanHandle(Protocol):
    """Protocol for the span handle yielded by ObservabilityPort.start_span.

    The only operation agent code performs on a span handle is:
      - ``span.update(**kwargs)``    — record output, usage, metadata, etc.
      - ``span.start_as_current_observation(...)``  — open a nested child span
        (generation, retriever, etc.).

    The nested call returns a context manager whose ``__enter__`` yields
    another SpanHandle (or the same Protocol), so any depth of nesting is
    supported.
    """

    def update(self, **kwargs: Any) -> None:
        """Record output / metadata on this span."""
        ...  # pragma: no cover

    def start_as_current_observation(
        self,
        name: str,
        *,
        as_type: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Open a nested child observation (generation, retriever span, etc.).

        Returns a context manager whose ``__enter__`` yields a SpanHandle.
        Typed ``Any`` to avoid recursive Protocol generics.
        """
        ...  # pragma: no cover


# ─── ObservabilityPort Protocol ───────────────────────────────────────────────


class ObservabilityPort(Protocol):
    """Protocol for tracing / observability.

    Every method maps to a current usage pattern in the codebase; the protocol
    covers ONLY the used subset so that adapters stay thin and the noop path
    remains trivially correct.
    """

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
        """Open a top-level observation and yield a SpanHandle.

        Returns a context manager.  ``__enter__`` yields a SpanHandle;
        ``__exit__`` closes the observation.

        Typed ``Any`` so the Protocol does not need to parameterise the
        context-manager generics — callers use the yielded handle as a
        SpanHandle.
        """
        ...  # pragma: no cover

    def current_trace_id(self) -> str | None:
        """Return the active trace ID, or None when tracing is disabled."""
        ...  # pragma: no cover

    def flush(self) -> None:
        """Flush any buffered trace data to the backend."""
        ...  # pragma: no cover

    def propagate_session(self, session_id: str) -> Any:
        """Return a context manager that propagates *session_id* to child spans.

        In the Langfuse adapter this calls ``langfuse.propagate_attributes
        (session_id=session_id)`` — necessary to populate the Sessions view
        (v4 SDK).  In the Noop adapter it returns a nullcontext.

        The context manager is entered BEFORE the top-level span so that all
        child spans inherit the session.
        """
        ...  # pragma: no cover

    def record_security_event(
        self,
        *,
        boundary: Literal["user_input", "retrieved_content"],
        categories: tuple[str, ...] = (),
        detector_score: float | None = None,
        reason: str | None = None,
        location: str | None = None,
    ) -> None:
        """Record a BLOCKED security event so it surfaces in the trace UI (ADR 0024).

        Emitted when a guardrail blocks untrusted input. The adapter maps this to
        a filterable ``security.guardrail_block`` score, so a caught injection is
        visible and auditable in observability — the original "show the security
        incident" requirement.

        METADATA-ONLY by contract (RT-redteam.md): boundary, detector categories,
        detector confidence, and a corpus *location* (``guidance_id#chunk_index``)
        may be recorded — but NEVER the offending text, which must not reach traces.

        Args:
            boundary:       Which guardrail boundary fired.
            categories:     Detector category labels (machine labels, not text).
            detector_score: Detector confidence (0.0–1.0), if available.
            reason:         Short machine-ish label; NEVER the raw text.
            location:       Corpus locator for a blocked passage
                            (``guidance_id#chunk_index``); None for user input.
        """
        ...  # pragma: no cover


# ─── Noop adapters ────────────────────────────────────────────────────────────


class _NoopSpanHandle:
    """SpanHandle whose every operation is a no-op."""

    def update(self, **kwargs: Any) -> None:
        return

    def start_as_current_observation(
        self,
        name: str,
        *,
        as_type: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Return a context manager that yields another _NoopSpanHandle."""
        return _noop_span_cm()


@contextmanager
def _noop_span_cm() -> Generator[_NoopSpanHandle, None, None]:
    yield _NoopSpanHandle()


class NoopObservabilityAdapter:
    """ObservabilityPort that does nothing.

    Returned by get_observability() when settings.langfuse_enabled is False.
    Replaces the scattered ``if lf is not None … else contextlib.nullcontext``
    guards at call sites: code can now call span.update() unconditionally.
    """

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
        return _noop_span_cm()

    def current_trace_id(self) -> str | None:
        return None

    def flush(self) -> None:
        return

    def propagate_session(self, session_id: str) -> Any:
        import contextlib

        return contextlib.nullcontext()

    def record_security_event(
        self,
        *,
        boundary: Literal["user_input", "retrieved_content"],
        categories: tuple[str, ...] = (),
        detector_score: float | None = None,
        reason: str | None = None,
        location: str | None = None,
    ) -> None:
        return


# ─── Factory ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_observability() -> ObservabilityPort:
    """Return the profile-resolved observability adapter (process-lifetime singleton).

    local profile + langfuse_enabled → LangfuseObservabilityAdapter
    local profile + langfuse disabled → NoopObservabilityAdapter
    Non-local profiles raise NotImplementedError until the cloud-adapter phase.
    """
    profile = settings.rra_profile

    if profile == "local":
        if settings.langfuse_enabled:
            from rra.adapters.langfuse_observability import (  # lazy
                LangfuseObservabilityAdapter,
            )

            return LangfuseObservabilityAdapter()

        return NoopObservabilityAdapter()

    raise NotImplementedError(
        f"{profile!r} adapter lands in the cloud-adapter phase"
    )
