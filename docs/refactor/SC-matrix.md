# SC-matrix — Security Critic / Blue Team defense matrix (per phase)

Defensive secure-by-default review that GATES each phase: the phase does not
proceed until SC's gaps are closed or recorded as accepted risks with rationale.
Runs at maximum reasoning. Maintains the defense matrix across all phases:
each attack → the control that stops it → the owning port/seam → default-on or not.
See CLAUDE.md "Continuous red-team and critic review".

---

## Phase 3 (security spine) — SC review + defense matrix — 2026-06-12

**Reviewer:** Subagent SC (defensive gate), max reasoning. **Verdict (as
delivered):** PASS-WITH-NOTES. **Verdict after fixes:** all conditions cleared
except two external/accepted items (C, D below). Method: read every artifact,
reproduced the free harness, traced config resolution under `local`/`aws`,
confirmed the CFV guard exits 2 and the gate exit codes propagate.

### Defense matrix (attack → control → port/seam → default-on)

| # | Attack / threat | Control that stops it | Owning port / seam | Default-on? |
|---|---|---|---|---|
| 1 | Direct injection in user query (RT-3) | HF DeBERTa detector @ `user_input` (block before any side effect, generic 400) | guardrails port, `api.py` | YES (local-hf) |
| 2 | Indirect injection via poisoned passage (RT-1) | HF detector @ `retrieved_content` (passage dropped; researcher does NOT try/except → infra failure fails secure) | guardrails port, `researcher.py` | YES |
| 3 | Forged `</passage><passage>` XML in corpus text/title | `xml_escape_untrusted` (& < >) | sanitizer seam, analyst + critic | YES (deterministic) |
| 4 | Injection through critic trust-anchor `<citation_checks>` (source_text/quoted_text) | `xml_escape_untrusted` on both | sanitizer seam, `critic.py` 6 sites | YES |
| 5 | Fabricated citation address/quote (RT-1 path) | `check_citation` rejects via transport chokepoint w/ critic principal | tools + identity ports | YES |
| 6 | Tool-misuse directive (e.g. `fetch_guidance`) | deny-by-default `authorize_tool` for every role | identity port (ADR 0021) | YES |
| 7 | Zero-click exfil via markdown image — prose AND citation quote | `strip_markdown_images` deny-all on both channels (RT-P3-3) | output-filter seam, `api.py` | YES |
| 8 | Secret/PII in assembled prompt | model never sees secrets (SecretStr confinement) | config boundary | YES (architectural) |
| 9 | Critic disruption → auto-approve fabricated draft (RT-2) | fail-CLOSED to `escalate` on malformed/parse-error verdict | critic seam | YES |
| 10 | Detector malfunction (bad label / empty result) waves content through (RT-P3-5) | fail-CLOSED block on malfunction | guardrails adapter | YES |
| 11 | Persistence / resume replay (RT-9) | no resume path exists (inert-no-resume, rt-017) | architecture-asserted | N/A |
| 12 | Prose persuasion of analyst/critic (rt-006, rt-014) | **none local** — named residual, two-arm eval owns | behavioral (not exercised) | **NO — residual** |
| 13 | Malicious model code / weight swap (RT-8) | `trust_remote_code=False` + `use_safetensors=True` + revision hash pin; revision in report | guardrails adapter, supply chain | YES |

### Verified claims (checked, not trusted)
- No raw text in logs (detector adapter + harness): YES — only boundary/chars/score/blocked/label.
- `reason` is a label, never the text: YES.
- Critic fails CLOSED to `escalate`; `route_after_critic`→END; surfaced via `_build_warning`, never coerced to approve: YES.
- Output filter logs a count only; `_sanitize` has zero logging and never sees the stripped URL: YES.
- `secret-confinement` probe reads `get_secret_value()` for membership only; error path uses `type(exc).__name__`: YES.
- Report on disk contains no payloads/secrets/URLs: YES.
- Gate exit codes: 1 on coverage<min / fp>max / any harness error; 2 on CFV; empty fixture → 1 (can't report green): YES.
- Blast radius: real defense-in-depth — with the detector swapped to allowall, controls 3–9 (sanitizer, citation-gate, tool-scoping, secret-confinement, output-filter, fail-closed critic) still hold. Detector-only 0.526 vs coverage 0.895 quantifies the layering.

### Findings and disposition

| ID | Finding | Sev | Disposition |
|---|---|---|---|
| SC-A | **`os.environ` leak + `TestTwoArmOrchestration` had no skipif** — detector setting could bleed `local-hf` into the worker, defeating conftest's `allowall` isolation (fail-secure direction, but breaks the stated invariant). | M | **FIXED** (same as RT-P3-4): env writes dropped; singleton snapshot/restore in `finally`; `_restore_detector_env` fixture + `skipif(model cache)` added. |
| SC-B | **Cloud profile silently inherited `local-hf` field default** — `RRA_PROFILE=aws` with no override resolved to the local HF detector instead of `NotImplementedError`, contradicting the documented contract (fail-secure, but masks a missing cloud adapter). | L | **FIXED.** `get_guardrails` now raises for any non-local profile unless detector is an explicit `allowall` stub; the local HF path is reachable only under the local profile. Two parametrized regression tests added. |
| SC-C | **CI `security-gate` job must be a required status check** — the step exit code propagates (no `continue-on-error`), but the "required" flag lives in branch protection, not the repo. | L | **OPEN — external.** Flagged to the lead: mark `security-gate` required on the integration branch. |
| SC-D | **FP rate is exactly 0.200 against a ≤0.20 ceiling, zero margin** over a 5-row benign denominator (rt-c03, the supersession look-alike, is the accepted hard FP). One new benign FP tips the gate. | Info | **ACCEPTED RISK, documented.** Recorded in ADR 0023 and config; the "reopen if corpus grows" trigger now also covers widening the benign set. |

### Gate verdict
**PASS** after fixes. SC-A and SC-B are closed in this phase with regression
tests; SC-C is an external branch-protection action for the lead; SC-D is an
explicitly accepted residual with a reopening trigger. No exploitable
secure-by-default hole; both deviations (A, B) failed in the secure direction.
