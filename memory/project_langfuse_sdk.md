---
name: project-langfuse-sdk
description: Langfuse 4.7.1 is installed (pyproject declares >=2.50.0); v4 uses OTel context managers, not the v2 stateful trace.span() API
metadata:
  type: project
---

`langfuse>=2.50.0` in pyproject.toml resolves to **4.7.1** (as of 2026-05-31).

**Why:** The v4 SDK changed the instrumentation model entirely — it's built on OpenTelemetry. The old v2 API (`lf.trace()`, `trace.span()`, `trace.generation()`) does NOT exist. Any code that calls those methods will raise `AttributeError`.

**v4 API to use:**
- `lf.start_as_current_observation(name, as_type="span"|"generation"|"retriever", input, ...)` — context manager that makes the span current in OTel context; nested calls auto-parent.
- `span.update(output=..., usage_details={"input": n, "output": n})` — update span data before it ends.
- `lf.get_current_trace_id()` — returns the current trace ID (string UUID) from OTel context; call from inside the outer `start_as_current_observation` block.
- `contextlib.nullcontext(None)` — use as the no-op gate when Langfuse is disabled.
- `lf.flush()` — synchronously flush buffered spans to Langfuse.

**How to apply:** When writing any new Langfuse instrumentation in this project, use the v4 context manager pattern, not the v2 stateful pattern. See `src/rra/api.py` and `src/rra/retrieval.py` for working examples.
