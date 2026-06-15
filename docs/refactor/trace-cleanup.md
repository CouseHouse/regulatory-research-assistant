# Langfuse trace instrumentation cleanup

Derived from a 2026-06-15 review of LIVE Langfuse traces (happy-path `/query` = 47
observations, a failed query, a `security.guardrail_block` one-shot). Prioritized task
list for cleaning up trace accuracy and necessity.

**Status (2026-06-15):**
- **Item 1 ✅ done** — removed the duplicate `search_corpus` retriever span in `researcher.py`;
  the canonical metadata-only span at `retrieval.search_corpus` survives.
- **Item 5 ✅ done** — ADR 0025; `product_context` redacted to a char-count in the `query` span.
- **Item 3 ✅ resolved by ADR 0025** — policy: generations record token usage + PII-safe
  *derived* I/O only, never raw prompts/passages/`product_context`. Current behavior already
  complies (researcher's reformulated-query I/O is a derived search string, kept); no code change.
- **Item 2 ⏸ deferred with root cause** (see below).
- **Item 4 ⏸ deferred** (optional).
- **Owed:** one paid `/query` to re-verify the trace (one `search_corpus` per retrieval;
  `product_context` redacted; observation count drops from 47).

## Item 2 — root cause (deferred, needs runtime iteration)

The analyst **revision** generation (`anthropic:analyst`, rev ≥ 1) parents to the **`critic`**
span instead of `analyst:rev{n}` (which renders empty). The agent code is **correct** —
`analyst.py` opens `analyst:rev{n}` and nests its generation inside that span handle; the
first-pass analyst nests correctly. The mis-parent is an **OTEL active-span context bleed across
LangGraph nodes**: the critic node's `start_as_current_observation` context isn't fully detached
before the next node (the revision analyst) runs, so the revision generation attaches to the
still-active critic context. Not a safe agent-code edit — it lives in how the observability
port's spans propagate through LangGraph's pregel runner. A fix needs runtime experimentation
(e.g. anchor each node span to the request-trace context, or reset OTEL context at node
boundaries) **and** a looped trace to verify (paid `/query`, loops ~60% of runs).

---

You're in the Regulatory Research Assistant repo (multi-agent RAG over FDA guidance;
LangGraph; Anthropic SDK direct; Langfuse observability behind an observability PORT —
ADR 0020). Read CLAUDE.md first, then: `src/rra/ports/observability.py`,
`src/rra/adapters/langfuse_observability.py`, `src/rra/agents/{researcher,analyst,critic}.py`,
`src/rra/api.py`, and `docs/refactor/RT-redteam.md` (RT-4).

Fix the issues below IN PRIORITY ORDER. Branch off `refactor/ports-adapters-security`;
small PR; conventional commits; dev-log entry; ADR for any decision (item 5).

## Gates (a change is wrong if it lowers a gate even with green tests)
- `HF_HUB_OFFLINE=1 uv run pytest tests/ --no-cov` → all pass
- `uv run mypy` on touched files → clean
- `uv run python -m rra.evals.security` → unchanged (coverage 0.895 / FP 0.200)
- `CRITIC_FORCE_VERDICT` unset everywhere
- NEVER add secrets or PII to traces — tighten, never loosen
- `citation_validity` eval is a PAID call: run at most ONCE, only if you change LLM-visible bytes

## P1 — structural, deterministic, no paid calls
1. **De-dupe `search_corpus` retriever spans.** Each sub-question emits TWO retriever
   observations: the tool's own (rich, passages in output) AND a redundant wrapper in
   `researcher.py` (`with span.start_as_current_observation(name="search_corpus",
   as_type="retriever", ...): pass`, carries only passage_count + sub_question). End with
   exactly ONE `search_corpus` observation per retrieval.
   *Acceptance:* a fresh `/query` trace shows one `search_corpus` per sub-question.
2. **Fix the analyst-revision nesting.** On a critic-loop revision the `anthropic:analyst`
   revision generation is emitted UNDER the `critic` span while the dedicated `analyst:rev1`
   span is empty. Re-parent the revision generation under `analyst:rev1`.
   *Acceptance:* in a looped trace, `analyst:rev1` owns the revision generation; `critic`
   contains only critic generation(s) + citation checks.

## P2 — consistency + optional cleanup
3. **Make generation I/O recording consistent.** `anthropic:researcher` records input/output;
   `anthropic:{planner,analyst,critic}` record token usage only (`in=0b/out=0b`). Pick ONE
   policy and apply everywhere — PII-safe (no raw passage text / product_context). Document it.
4. **(Optional, lowest)** Condense the 24 `check_citation` spans (12/critic-pass × 2) into one
   span-per-pass with a per-citation result summary, or leave with a one-line justification.

## P3 — PII posture (DECISION → ADR; security-relevant)
5. Traces store the full `product_context` (confidential commercial info) and full retrieved
   passage text (RT-redteam.md RT-4 gap). ADR decides: redact/hash `product_context` and/or
   truncate passage text at the instrumentation boundary, vs. accept-and-document (self-hosted
   Langfuse). Blocked content must still never reach traces (already true — keep it).

## Verify at the end
`./scripts/serve.sh restart`, run ONE benign `/query` (paid, once — the PCCP question reliably
triggers the critic loop), fetch the trace via the Langfuse public API
(`GET /api/public/traces/{id}`, keys from `config.py`), and confirm: one `search_corpus` per
retrieval, revision under `analyst:rev1`, consistent generation I/O, no secret/PII regressions.
Report before/after observation count (was 47).
