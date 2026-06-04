# PENDING DECISIONS — working scratch doc

> **This is NOT a numbered ADR.** It is a working scratch doc of decisions that need **you**
> before the gated/blocked items in [`../plan/next-session-plan.md`](../plan/next-session-plan.md)
> can move. **Do not implement anything here** until you've decided — these were deliberately left
> un-actioned by the 2026-06-03 autonomous planning pass (Architect → Critic → Reviewer).
>
> Each decision: the **question**, the **options**, the **tradeoffs**, the **sub-agents'
> recommendation** (reconciled across the three passes), and **what it unblocks**. When you decide
> one, the matching plan item flips from BLOCKED → READY/GATED.
>
> **✅ STATUS — 2026-06-03: ALL FIVE SETTLED.** D1 (τ=0.85 + matcher-v2) · D2 (Langfuse → Day-8 phase) ·
> D3 (accept residual, document Day-13) · D4 (4a pointer note, 4b keep both tables) · D5 (no action).
> Each resolution is in its **▶ SETTLED** block below; unblocked plan items are updated in
> [`../plan/next-session-plan.md`](../plan/next-session-plan.md).

---

## Decision 1 — critic-flip slot + the τ target/acceptance criteria

> **▶ SETTLED — 2026-06-03 (post near-miss-band inspection). τ = KEEP 0.85.**
> Hand-inspecting the 13-record 0.70–0.85 near-miss band: **12/13 are matcher false-negatives**
> — PDF line-number / whitespace noise the Day-7 v1 `_normalize` missed (mid-line line-numbers,
> digits glued to words, intra-word line-break splits) — and **1/13 is a genuine borderline**
> (ellipsis stitch, case #9). Lowering τ to absorb the band would **launder** fixable matcher
> noise *and* the one real borderline into a "pass," hiding a matcher bug behind a threshold.
> **The lever is matcher-preprocessing v2, NOT the threshold** — see
> [`../plan/matcher-preprocessing-v2.md`](../plan/matcher-preprocessing-v2.md) (slotted on the
> eval-maturation day, ahead of the critic-flip). **This supersedes the planning-pass lean toward
> a τ in the high-0.7s** (preserved below as the record).
> **Slot unchanged:** the critic-flip stays on the eval-maturation day, ordered *after* matcher-v2
> (matcher as good as it gets before the critic-delta is measured on it); per the scheduling
> decision that day runs **before the IaC/cloud days** (now Days 9–10). → **Item 3b flips BLOCKED → READY.**

**Question.** Two parts: **(i)** where to slot the critic-flip *execution day*; **(ii)** what τ
target / acceptance criteria to adopt from the residual `similarity_score` distribution.
*(A third sub-question the architect raised — "accept the pre-swap distribution vs. do a $0
re-measure against the live table" — is **struck**. The Critic proved, and the Reviewer verified,
that `evals/smoke-chunks-detail.json` (mtime 15:57, `table='chunks'`, 386/446, row-by-row delta=0
vs the scratch file) **is** the post-swap live-corpus measurement. The calibration data is already
on disk, $0. Nothing to re-measure.)*

**Options.**
- *Slot (i):* **(A)** critic-flip as the **first post-cloud day, ahead of session tracking**;
  **(B)** after Day-11 sessions; **(C)** defer further.
- *τ target (ii):* **(A)** keep 0.85 (but then a live critic causes churn — ADR 0013);
  **(B)** lower to absorb the **0.70–0.85 near-miss band** (13 records — honest boundary-straddle
  quotes); **(C)** lower further into **0.50–0.70 mid-range** (27 records — riskier: admits
  paraphrase / wrong-chunk); **(D)** set a **pass-rate acceptance target** (e.g. "τ such that ≥X%
  of locked analyst quotes pass") rather than a fixed number.

**Tradeoffs.** The residual is **60/446 non-passing**, structured: 13 ellipsis (analyst
synthesized/omitted), 8 very-low <0.50 (likely synthesized), 27 mid 0.50–0.70 (boundary straddle /
paraphrase / wrong-chunk), 13 near-miss 0.70–0.85 (the τ-sensitivity band). Lowering τ to cover the
13 near-misses cheaply recovers honest boundary-splits; reaching into the 27 mid-range starts
laundering genuinely-weak citations and erodes the faithfulness signal. τ too high (0.85) + a live
critic = revision churn on honest quotes. **The 8 synthesized + 13 ellipsis are real analyst
behaviour τ cannot fix** — that's a *prompt* lever, not a threshold lever (→ Decision 3).

**Sub-agents' recommendation.** **Slot = (A)** — first post-cloud day, ahead of sessions (clean
distribution freshest; sharpens every later narrative; single post-flip eval baseline for Day-11).
**Order:** calibrate τ *before* flipping the critic — hard-required, non-negotiable. **τ value:**
the agents deliberately did **not** pick a number (it needs your eye on the distribution), but the
evidence points to **(D)+(B)** — a τ in roughly the **high-0.7s** that covers the 13 near-miss band
**without** reaching the 27 mid-range, validated by the pass-rate it yields. Confirm the actual cut.

**What it unblocks.** Plan **Item 3b** (BLOCKED → READY for the $0 calibration + the small
critic/config edit). The post-flip end-to-end validation (Item 3c) remains **GATED** on a judge
eval run regardless.

---

## Decision 2 — Langfuse scores/datasets: keep in future-work §14, or grant a real slot?

> **▶ SETTLED — 2026-06-03. Grant a real slot — pulled into the Day-8 eval-maturation phase.**
> Langfuse scores/datasets is **no longer future-work**: it slots **last in the Day-8 eval-maturation
> day, after critic-delta**, so it captures **post-flip** scores (not pre-flip numbers the flip
> invalidates — the rationale the agents themselves gave for *when* to pull it). This **supersedes the
> sub-agents' unanimous "(A) keep in future-work, lowest priority" lean** — a deliberate override. →
> **Item 10 flips BLOCKED → SCHEDULED.** (Caveat: it makes Day 8 heavy — matcher-v2 + τ + critic-flip
> + critic-delta + ~1-day Langfuse wiring; treat as the phase order, compressible if Day 8 overruns.)

**Question.** The reopen precondition (a stable Day-7 scorer contract) is now met. Pull
future-work §14 into a real calendar slot, or leave it unscheduled?

**Options.** **(A)** keep in future-work; **(B)** grant a real ~1-day slot.

**Tradeoffs.** Today eval results write to `evals/results/*.md` + git tags; scores do **not** flow
into Langfuse datasets/experiments. Wiring them is ~1 day, fully parallelizable, with **no
dependents**. The Langfuse stack is up (clickhouse container unhealthy, web/worker live). It is the
lowest-value item on the board relative to postmortems / Loom / cost-model — pure observability
polish on an already-working eval path.

**Sub-agents' recommendation.** **(A) keep in future-work; pull only if the Day-16 buffer
survives.** If pulled, schedule *after* Item 3 (so it captures post-flip scores, not pre-flip
numbers the flip invalidates) and *after* Item 5. Unanimous, lowest priority.

**What it unblocks.** Plan **Item 10** (BLOCKED → scheduled, or formally left in future-work).

---

## Decision 3 — What to do about the faithfulness residual (the numbers held — so now what?)

> **▶ SETTLED — 2026-06-03. Lever = matcher-v2 + accept-the-rest (not τ-calibration).**
> **matcher-preprocessing v2 recovers the ~9 cleanly-fixable cases** in the near-miss band (the
> mid-line / glued / line-break-split line-number noise — see
> [`../plan/matcher-preprocessing-v2.md`](../plan/matcher-preprocessing-v2.md)). The remainder of
> the band stays **ACCEPTED residual, documented in the Day-13 postmortem**: footnote-splices
> (#4, #7), the ellipsis stitch (#9), and the debatable inline enumerator (#13). The original
> "(B) τ-lever now" half of the lean is **replaced by "matcher-v2 now"**; the "(A) accept +
> document the rest" half **stands** (the 27 mid-range + 8 synthesized + 13 ellipsis populations
> remain a correct true-negative signal). **No analyst-prompt change** — option (C) stays deferred
> exactly as recommended: it still costs a judge eval to validate and risks the 386.

**Question.** Re-validation moved **nothing** (recall@10 = 1.00 held; faithfulness **386/446** held;
delta = 0). The clean corpus did not lift the score. So is any action warranted on the **60/446
non-passing** *now* — a prompt change? accept-and-document? — or is τ-calibration (Decision 1) the
only lever?

**Options.** **(A)** accept the residual as-is and document it honestly in the Day-13 postmortem
(it's a *true-negative* signal, not a bug); **(B)** τ-calibration only (Decision 1) — recovers the
threshold-addressable near-misses; **(C)** also make an **analyst-prompt change** to attack the ~21
real analyst issues (8 synthesized + 13 ellipsis) — push the analyst to quote verbatim spans rather
than synthesize/elide; **(D)** (B)+(C).

**Tradeoffs.** The residual splits into two populations: **~39 matcher/corpus near-misses** (the
0.70–0.85 + 0.50–0.70 bands — threshold-addressable, Decision 1's lever) and **~21 genuine analyst
issues** (8 synthesized + 13 ellipsis — paraphrase/elision; **τ cannot touch these**, only a prompt
change can). Accepting is the honest portfolio story ("21 of 446 are true faithfulness misses the
system correctly flags"). A prompt change risks regressing the other 386 and **requires a fresh full
eval to validate** (judge $) — non-trivial, and widens scope late in the build.

**Sub-agents' recommendation.** **(B) now + (A) for the remainder.** Pull the τ lever (Decision 1)
to recover the boundary-straddle near-misses; **accept + document the ~21 analyst-behaviour misses
as a correct true-negative signal** in the Day-13 postmortem rather than chasing a risky late prompt
change. Treat (C) as explicitly **deferred** (a future-work candidate), not a now-action — it costs
a judge eval to validate and risks the 386. **Do not let a prompt change sneak into Item 3.**

**What it unblocks.** Sharpens Item 3's scope (confirms τ is the only code lever now) and feeds the
Day-13 postmortem narrative. No new build item.

---

## Decision 4 — Atomic-swap-vs-`--truncate` documentation integrity (+ leftover scratch tables)

> **▶ SETTLED — 2026-06-03. (4a) pointer note, NOT a new ADR. (4b) keep both tables.**
> **4a:** reconcile the swap-vs-`--truncate` mismatch with a **one-line pointer note appended to ADR
> 0014's validation banner** (append-only — Decision body untouched) **+ a cross-ref in `index.md`**.
> A whole new ADR for a benign *documented-preferred (swap) vs. executed (`--truncate`)* footnote is
> ceremony: the outcome is correct (`--truncate` clears orphan rows; ADR 0014 §Alternatives permits it
> on a dev box; validated 386/446 + recall@10=1.00) and the banner already records `--truncate`.
> Rejected **(B)** new ADR (disproportionate) and **(C)** leave-it (the bare "swap" cross-refs read as
> a contradiction). **4b:** **KEEP** both `corpus.chunks_rechunk` (live tooling references it —
> `smoke_rechunk._VALID_TABLES`; the `DROP` is irreversible for near-zero gain) **and**
> `corpus.chunks_fixedsize_backup` (restore path); reclaiming the scratch table later stays a GATED
> future action. **With D4 closed, all five decisions (D1–D5) are settled.**

**Question.** Two linked sub-questions:
- **(4a)** ADRs 0014 (Decision step 4; rationale at 0014:39,48,64), 0006:17, and 0010:12 document
  the cutover as an **atomic RENAME swap** (`BEGIN; RENAME chunks→chunks_old; RENAME
  chunks_rechunk→chunks; COMMIT;`). Execution used **`uv run python -m rra.ingest --truncate`**.
  ADR 0014's *validation banner* already records `--truncate`, but the Decision section + cross-refs
  still say "swap", and **ADR bodies are append-only.** How to reconcile? (Outcome is correct —
  `--truncate` also kills orphan rows, and ADR 0014 explicitly permits it on a dev box — so this is
  integrity hygiene, not a bug.)
- **(4b)** Keep or drop `corpus.chunks_rechunk` (the Day-7 scratch table, now orphaned)? Confirm-keep
  `corpus.chunks_fixedsize_backup` (2726, documented restore path).

**Options.**
- *4a:* **(A)** append a single **reconciling pointer note**; **(B)** write a tiny **new ADR**
  ("cutover executed via `--truncate`, not RENAME swap; rationale preserved"); **(C)** leave it.
- *4b:* `chunks_rechunk` → **(A)** keep; **(B)** `DROP` (GATED — irreversible).
  `chunks_fixedsize_backup` → keep (not in question).

**Tradeoffs.**
- *4a:* Append-only ADRs mean the Decision-section prose will always say "swap" somewhere; a pointer
  note is the lightest honest fix and matches the project's "pointers only" convention. A new ADR is
  heavier but gives a clean canonical "documented-preferred ≠ executed, and why that's fine" record
  (arguably good portfolio hygiene). Leaving it (C) is defensible only because the banner already
  notes `--truncate` — but the unqualified "swap" cross-refs read as contradictions to a careful
  reviewer.
- *4b:* **`chunks_rechunk` is referenced by live tooling** — `smoke_rechunk._VALID_TABLES =
  {chunks, chunks_rechunk}` (`smoke_rechunk.py:46`). Dropping it breaks any future
  `smoke_rechunk --table chunks_rechunk` run, and the `DROP` is irreversible for near-zero benefit.

**Sub-agents' recommendation.**
- *4a:* **(A) — append one reconciling pointer note**, consistent with the append-only convention.
  Suggested placement: **in ADR 0014's banner/append-block** (where a reader of the affected Decision
  section lands) **plus a one-line cross-ref in `index.md`.** A new ADR (B) is acceptable if you
  prefer a cleaner record, but it's more than the situation needs. **Not (C)** — the bare "swap"
  cross-refs are a real (if minor) contradiction.
- *4b:* **KEEP `chunks_rechunk`** (live tooling references it; the `DROP` is irreversible for
  near-zero gain) and **KEEP `chunks_fixedsize_backup`** (restore path). Reclaiming the scratch table
  later is a deliberate **GATED — irreversible `DROP TABLE`** action for a future session, not now.

**What it unblocks.** Closes the documentation-integrity loop before Day-14 polish / Day-15 demo, and
resolves the standing `corpus`-schema table ambiguity. (This is the one "doc fix" that is NOT in the
plan's READY hygiene batch, precisely because it touches append-only ADRs.)

---

## Decision 5 — `--save-baseline` cost-source correction (acknowledge only — no action)

> **▶ SETTLED — 2026-06-03. No action.** Acknowledged: `--save-baseline` re-runs the **analyst** model
> (a paid call), **not** Voyage — the critic's correction stands; it stays forbidden in any planning
> pass. Nothing to implement; recorded only to kill the latent "it's safe, it's not Voyage" mis-rationale.

**Question.** The architect pass asserted `--save-baseline` is "the paid **Voyage** step." The
critic flagged this as wrong. Does it change anything?

**Options / tradeoffs.** Verified: `src/rra/evals/smoke_rechunk.py:8` (docstring) + the
`save_baseline` path re-run the **analyst** model to lock fresh quotes — an **analyst-API** cost, not
a Voyage embedding cost (the file's own docstring says "No re-embed, no Voyage"). Both are forbidden
in a planning pass regardless, so the **plan is unaffected** — but the rationale must be stated
correctly so nobody later reasons "it's safe because it's not Voyage" (it's still a paid model call).

**Sub-agents' recommendation.** **No action beyond acknowledging the correction.** Recorded here only
because it was an explicit architect↔critic conflict you asked to be reconciled: the critic is right —
`--save-baseline` = **analyst** cost, and it stays forbidden in any planning pass.

**What it unblocks.** Nothing — it removes a latent mis-rationale that could otherwise green-light a
paid call under a false "$0" belief.

---

## Quick index

| # | Decision | Unblocks | Agents' lean |
|---|---|---|---|
| 1 | critic-flip slot + τ target | Item 3b (BLOCKED→**READY**) | **✅ SETTLED: KEEP τ=0.85; lever = matcher-v2, not threshold** (supersedes the high-0.7s lean) |
| 2 | Langfuse: future-work vs slot | Item 10 (BLOCKED→**SCHEDULED**) | **✅ SETTLED: pulled into Day-8 eval-maturation, after critic-delta** (overrides the keep-in-future-work lean) |
| 3 | faithfulness residual handling | Item 3 scope + Day-13 postmortem | **✅ SETTLED: matcher-v2 recovers ~9; accept+document #4/#7/#9/#13; no prompt change** |
| 4 | ADR swap-vs-truncate + scratch tables | doc integrity; corpus-schema cleanup | **✅ SETTLED: (4a) pointer note in 0014 banner — not a new ADR; (4b) keep both tables** |
| 5 | `--save-baseline` cost rationale | — (acknowledge) | **✅ SETTLED (no action): critic correct — analyst cost, not Voyage; stays forbidden** |
