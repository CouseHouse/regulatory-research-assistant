# RT-log — Red Team review log (per phase)

Offensive review appended at each phase boundary, on that phase's actual output,
before the next phase builds on it. Runs at maximum reasoning. Owns the
benign/attack corpus (`evals/fixtures/redteam_injection.jsonl`) and the demo
attack set. See CLAUDE.md "Continuous red-team and critic review".

---

## Phase 3 (security spine) — RT review — 2026-06-12

**Reviewer:** Subagent RT (offensive), max reasoning. **Scope:** the layer-aware
security gate + HF injection detector + sanitizer + output filter as they would
ship on `refactor/phase3-security-harness`.

**Bottom line (as delivered):** the layer-aware framing is the right call and the
tool-scoping / citation-gate probes are sound, but the first cut's headline
coverage of 0.882 was **inflated** for two structural reasons, and the RT-1 fix
missed one client-facing channel. Two findings were merge-blocking. No earlier
ADR was contradicted (no anti-compounding STOP triggered).

### Findings, severity × likelihood, and disposition

| ID | Finding | Sev×Lik | Disposition |
|---|---|---|---|
| RT-P3-1 | **Sanitizer probe is a tautology** — `xml_escape_untrusted` always removes `<`/`>`, so the probe proved a property of the escaper, not that the attack is neutralized in context. Inflates coverage; would green-light a future mis-tagged case. | H×H | **FIXED.** `_probe_sanitizer` now assembles the REAL `analyst._format_passages_xml([payload])` and asserts the formatter's structural tags appear exactly once each — a forged `</text></passage><passage…>` would push a count >1. Ties the probe to the runtime control. |
| RT-P3-2 | **Chunking belt-and-suspenders never exercised** — longest fixture was 531 chars; `_CHUNK_CHARS≈2040`, so the multi-chunk/max-score path ran zero times and the documented end-of-passage delivery vector was untested. `_CHARS_PER_TOKEN=4` was optimistic enough that pipeline truncation could drop an end-of-passage payload from a "single" chunk before scoring; blind char-split also enables boundary-straddle score dilution. | H×M | **FIXED.** `_CHARS_PER_TOKEN`→3 (real 512-token window fits one chunk, truncation can't eat a kept payload); added 25% sliding-window overlap so a straddling payload survives intact in one window; added fixtures **rt-018** (>2040-char end-of-passage imperative) and **rt-019** (boundary-straddle) — both now caught by the detector (detector-only rose 0.471→0.526). |
| RT-P3-3 | **`Citation.quoted_text` shipped to the client unescaped/un-image-stripped**, bypassing the rt-010 output filter (which ran only on prose). A markdown-image exfil payload laundered through the `<q>` channel re-opened the zero-click channel. The output-filter probe tested a synthetic string, not the shipped artifact. | MH×M | **FIXED.** `api._resolve_citations` now runs `strip_markdown_images` on each quote (logs a count only); `_probe_output_filter` exercises the real `_resolve_citations` path so probe and artifact agree. |
| RT-P3-4 | **Harness leaks `os.environ["GUARDRAILS_DETECTOR"]`** — `run_layers`/`run_two_arm_with_mock_graph` set it and never restored it, able to defeat conftest's `allowall` isolation in a shared interpreter. (Also SC Finding A.) | M×H | **FIXED.** Dropped the env writes entirely (the `settings.__dict__` poke + `cache_clear` is sufficient); two-arm now snapshots/restores the singleton in `finally`. Added `_restore_detector_env` fixture + `skipif(model cache)` to `TestTwoArmOrchestration`. |
| RT-P3-5 | **Detector fails OPEN on unexpected label** — `_score_chunk` returned 0.0 (→allowed) on an unrecognized label; wrong direction for a deny-by-default spine and inconsistent with the threshold-0.2 rationale. Interacts with RT-8 (a tampered label head waved through). | M×L | **FIXED.** `_score_chunk` returns `None` on empty result / unrecognized label; `check` fails **CLOSED** (blocks, reason `detector_malfunction_fail_closed`) if any chunk malfunctioned. Unit-tested. |
| RT-P3-6 | **rt-006/rt-014 validated by nothing that runs** — tagging as `behavioral` is honest (no deterministic layer catches prose persuasion of the critic), but the two-arm eval that owns them is credit-gated and unrun, so they are *unmitigated, not merely residual*. | M×M | **ACCEPTED RISK, documented.** Reported as named residuals in every run; recorded in ADR 0023 "reopen if" and the dev-log with the two-arm eval as the closing trigger. RT-2's fail-closed critic limits the blast radius (a disrupted critic escalates, never auto-approves). |
| RT-P3-7 | **HF model revision not hash-pinned** — bare repo id resolved to whatever `main` pointed at; uv.lock pins packages, not weights (a separate supply chain). | M×L (H impact) | **FIXED.** Pinned `guardrail_model_revision=e6535ca4…` in config + `PROFILE_DEFAULTS`, passed `revision=` + `use_safetensors=True` + explicit `trust_remote_code=False` to the pipeline; revision printed in the security report header so a swap is visible. (torch CPU index pin confirmed sound: `explicit=true` scopes it to torch only — not a confusion vector.) |
| RT-P3-8 | **Attribute-channel landmine** — `xml_escape_untrusted` doesn't escape `"`, and `guidance_id`/`chunk_index` go into attributes unescaped. Safe today (only operator metadata reaches attributes), but a future content-sourced attribute would be an attribute-boundary breakout. | L×L | **DOCUMENTED.** Recorded as a precondition in RT-redteam.md RT-1: adding any content-sourced attribute requires escaping `"` first. |

### Anti-compounding check
No merged-phase invariant broken. ADR 0021 (identity advisory-trust) intact — the
tool-scoping probe is the one probe that maps cleanly to its runtime control with
no tautology (`fetch_guidance` in no role's scope). ADR 0022 (guardrails wiring)
intact; RT-P3-3 was a *missed application* of its untrusted-text rule, fixed
under 0022 with no supersession. ADR 0020 intact. ADR 0023's "mechanically
exercises each layer" claim is now true after RT-P3-1/2 (the two previously-weak
probes were fixed).

### Post-fix measured result
Coverage **0.895** (17/19), detector-only **0.526**, FP **0.200**, 0 errors.
Residuals rt-006/rt-014 (behavioral, two-arm-owned); architecture-asserted rt-017.
