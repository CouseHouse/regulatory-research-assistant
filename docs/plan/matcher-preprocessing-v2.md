# Task — matcher-preprocessing v2 ($0 text-only)

**Status:** DRAFT — not started. **Slot:** **Day 8** — the **eval-maturation day**, **before the critic-flip**
(the matcher should be as good as it'll get before the critic-delta is measured on it). Per the
scheduling decision the eval-maturation day runs **before the IaC/cloud days** (Days 9–10).
**Companion decision:** [`../decisions/PENDING-DECISIONS.md`](../decisions/PENDING-DECISIONS.md) D1 (SETTLED — keep τ=0.85, the lever is the matcher) and D3 (SETTLED — recover the clean cases, accept the rest).

---

## Why this task exists

D1 is settled: **τ stays at 0.85.** Hand-inspecting the 13-record 0.70–0.85 near-miss band showed
**12/13 are matcher false-negatives** — PDF line-number / whitespace noise the Day-7 v1 `_normalize`
didn't catch — and **1/13 is a genuine borderline** (ellipsis stitch, case #9). Lowering τ would
launder fixable matcher noise *and* the one real borderline into a "pass," hiding a matcher bug
behind a threshold. **The lever is the matcher, not the threshold.**

This task recovers the **cleanly-fixable** subset of those false-negatives and **stops there** —
the residual (footnote-splices, ellipsis, debatable enumerator) is accepted and documented in the
Day-13 postmortem, not chased.

## What v1 already does (and where it stops)

`_normalize` (`src/rra/mcp_server/tools.py:227`) runs three transforms before the final `\s+`
collapse, applied to **both** quote and chunk sides:

1. `_CURLY_MAP` — typographic quotes/apostrophes → ASCII.
2. `_LINENUM_INLINE_RE` (`tools.py:218`) — `(?<!CFR)(?<!USC)(?<!art)(?<=[\w,;:\.\)])\s+\d{2,4}\s*\n`
   — strips a 2–4 digit pypdf line-number **only when it is immediately before a newline**.
3. `_LINENUM_LINE_RE` (`tools.py:224`) — `(?m)^\s*\d{2,4}\s*\n` — strips isolated number-only lines.

**The structural gap:** rules 2–3 both require a `\n` anchor. The remaining near-miss FNs are cases
where pypdf dropped the line-number **mid-line** (no newline survived the extraction), or **glued**
it to an adjacent token, or **split a word** across the line break. v1 cannot see these.

`_normalize` feeds `match_quote` (`tools.py:247`), which is called by **both** production
`check_citation` **and** the `$0` text-only smoke (`rra.evals.smoke_rechunk`). **This task edits
production matching.** That is precisely why the zero-regression gate below is non-negotiable.

---

## IN SCOPE — the ~9 cleanly-fixable cases (honest recovery, low false-positive risk)

Each rule below is a **narrow extension** of the existing line-number logic, carrying forward v1's
guards (`CFR`/`USC`/`art` lookbehinds; preserve dotted numbers, parens, single digits).

1. **Mid-line line-numbers (not `\n`-anchored).** Extend stripping to a 2–4 digit run sitting
   **between two words on the same line**.
   - Examples: `"be 209 required"` → `"be required"`, `"regulatory 376 action"` → `"regulatory action"`.
   - **Highest-risk rule.** Dropping the `\n` anchor removes the strongest signal that a number is
     a line-number and not content. The forward/backward guards now carry *all* the weight: must
     **not** strip `"21 CFR 209"`, `"within 30 days"`, `"Form FDA 3500A"`, `"§ 820.30"`, `"510(k)"`,
     `"Part 11"`, or any dotted / unit-trailing / section-referenced number. Prove this explicitly.

2. **Line-numbers glued to words/parens (no separating space).** Strip a 2–4 digit run fused
   directly to a preceding alpha token or `)`.
   - Examples: `"only16"` → `"only"`, `"3500A)74"` → `"3500A)"`, `"mode31,"` → `"mode,"`.
   - Guards: strip **only** the trailing digit run; keep the alpha/paren/punct. Must **not** touch
     `"3500A"`, `"510(k)"`, `"Part 11"` written as `"Part11"`, version tokens, or `"21CFR"`-style
     fusions. The `)74` vs `(k)` distinction (digits-after vs letter-inside) is the seam to test.

3. **Intra-word PDF line-break splits.** Rejoin a single word pypdf broke across a line break,
   leaving a stray space inside it.
   - Example: `"Q-S ubmission"` → `"Q-Submission"`.
   - Most surgical / lowest-frequency rule. Guard: only rejoin where the fragment + continuation
     reconstruct a single hyphenated/compound term; do **not** merge two legitimately separate tokens.

4. **Inline list enumerators (#13) — DEBATABLE, OPTIONAL.** Normalize an inline enumerator mismatch
   (`"as follows: 1. foo 2. bar"`) only if it can be done without risk to real numbered content.
   **If it cannot be made clean, drop #13 to accepted residual** (D3) rather than forcing it.

## OUT OF SCOPE — over-reach risk; leave as accepted residual (D3)

- **Footnote-splices (#4, #7).** pypdf interleaves footnote text mid-sentence. Stripping it risks
  removing **real content**. Not worth it for 2 cases. → accepted residual, Day-13 postmortem.
- **Ellipsis stitching (#9).** The analyst legitimately elided with `…`. That is genuine analyst
  behavior — a **prompt** matter, deferred (D3 keeps no-prompt-change) — not a matcher fix.

---

## HARD REQUIREMENT — prove ZERO regression on the 386 currently-passing quotes

The v2 normalization could **over-strip** and silently break a working match. This gate exists to
catch that. All validation is **$0, text-only** — no Voyage, no judge — exactly like Day 7.

- **Baseline:** `evals/baseline-quotes.json` (locked analyst quotes) measured against `corpus.chunks`,
  per-citation distribution in `evals/smoke-chunks-detail.json`. Current faithfulness: **386/446**.
- **Command (same as Day 7):**
  ```bash
  uv run python -m rra.evals.smoke_rechunk --table chunks
  ```
  ⚠️ Do **not** run `--save-baseline` — that re-runs the **analyst** model (a paid call; it re-locks
  the golden quotes and would move the comparison baseline). Validate against the existing locked baseline only.
- **Pass criteria:**
  1. **No regression:** every one of the 386 currently-passing citations still passes. A net `+N`
     is **not** sufficient — a `+10/−1` is a failure, because the `−1` is a real match the v2 rule mangled.
  2. **Per-rule content-safety proof:** for each new strip rule, add unit tests showing it leaves
     real content intact — `"21 CFR 803"` / `"21 CFR 209"` mid-sentence, `"within 30 days"`,
     `"Form FDA 3500A"`, `"510(k)"`, `"§ 820.30"`, `"Part 11"`, single-digit versions — alongside the
     positive cases it's meant to strip. Mirror the v1 `TestCurlyQuoteNormalization` /
     line-number edge-case test style.
  3. Full unit suite green (`uv run pytest -m "not integration"`).
- **Eval-harness check** (per CLAUDE.md "before declaring done"): confirm `citation_validity` does not
  drop. The text-only smoke is the $0 proxy; the priced golden judge eval stays GATED (run only when
  the user authorizes the spend).

## Recovery estimate

| | Citations passing |
|---|---|
| Current baseline | 386 / 446 |
| Rules 1–3 land cleanly (~9 recovered) | **~395 / 446** |
| + #13 enumerator (optional upside) | ~396 / 446 |

Frame it honestly: **recover the cleanly-fixable, accept the rest.** The remaining residual after v2
(footnote-splices #4/#7, ellipsis #9, and #13 if not landed, plus the broader synthesized/ellipsis
and mid-range populations) is a **correct true-negative signal** — the system flagging citations that
genuinely don't have a verbatim span — and is documented as such in the Day-13 postmortem, not
papered over.

## Done when

- Rules 1–3 implemented in `_normalize`; #13 either landed cleanly or explicitly deferred.
- `smoke_rechunk --table chunks` shows **0 regressions** on the 386 and ~+9 recovered.
- Per-rule content-safety unit tests added and green; full suite green.
- A one-line dev-log entry records the before/after numbers and which near-miss cases lifted vs. stayed residual.
- **No** τ change, **no** analyst-prompt change, **no** re-embed, **no** `--save-baseline`, **no** merge/tag (unless the user says so).
