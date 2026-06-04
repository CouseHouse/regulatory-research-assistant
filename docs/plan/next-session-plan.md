# Next-session plan (reviewed, sequenced)

**Produced:** 2026-06-03, autonomous planning pass (analysis only — no system state changed).
**Method:** three independent reasoning passes — Architect → Critic → Reviewer — each given
artifacts, not the prior role's conclusions, so the Critic re-derived from the actual files
rather than rubber-stamping. Companion doc: [`../decisions/PENDING-DECISIONS.md`](../decisions/PENDING-DECISIONS.md)
(the decisions that need **you** before the gated/blocked items can move).

---

## STEP 0 — GATE CHECK: **PASSED** ✅

The structural re-ingest + re-validation (dev-log `2026-06-03 — Resolution A`) completed cleanly.
Verified against live read-only DB state this pass — not just the dev-log prose:

| Check | Dev-log claim | Live DB (read-only) | Match |
|---|---|---|---|
| `corpus.chunks` rows | 2745 | **2745** | ✅ |
| Distinct guidances | 71 docs | **71** | ✅ |
| Backup / restore path | `chunks_fixedsize_backup` @ 2726 | **exists, 2726 rows** | ✅ |
| char offsets populated | structural tracks spans | **2745/2745** | ✅ |
| Token spread | structural (variable, not fixed-512) | **avg 471, min 10, max 1108** | ✅ |
| recall@10 / faithfulness | 1.00 (13/13) / 386/446 @ τ=0.85 | dev-log + on-disk distribution file (not re-run — costs $) | ✅ (doc) |

**Treat structural as the live, validated architecture.** Restore path intact
(`corpus.chunks_fixedsize_backup`, 2726 rows). Unit suite green on this state: **160 passed,
3 deselected** (`uv run pytest -m "not integration"` — the 3 deselected integration tests were
excluded deliberately: two write to `corpus.chunks`, one can touch Voyage; excluding them honors
the no-corpus-touch / no-Voyage constraint).

One verified strengthening of the gate, found this pass: the live-corpus faithfulness distribution
(all 446 per-citation `similarity_score`s) **already exists on disk** at `evals/smoke-chunks-detail.json`
— measured post-swap against the live 2745-row table (file mtime 15:57, after the 14:27 scratch
run), **row-by-row identical** to the clean-scratch distribution (maxdelta = 0.0). So "delta = 0 /
all numbers transfer" is *evidenced*, not asserted — and τ-calibration needs **no** re-measurement.

---

## How to read the tags

- **READY** — safe to execute on your return: ordinary dev work + tests, nothing costly,
  irreversible, or corpus-touching.
- **GATED** — needs a costly or irreversible action; the specific gate is named inline
  (AWS spend, a judge eval run that costs $, Voyage calls, `DROP TABLE`, etc.).
- **BLOCKED** — needs one of your decisions first; the unblocking decision is named inline
  (see [`PENDING-DECISIONS.md`](../decisions/PENDING-DECISIONS.md)).

Sequence (Architect-proposed, Critic-endorsed, Reviewer-confirmed; **eval-maturation re-slotted
before the cloud demo per the 2026-06-03 post-inspection decision**):
**Day 8 eval-maturation (matcher-v2 → τ-confirm → critic-flip → critic-delta → Langfuse) → Day 9 IaC
→ Day 10 cloud demo → Day 11 sessions → Day 12 design docs → Days 13–16.**
Langfuse scores/datasets is now pulled into the Day-8 eval-maturation phase (after critic-delta —
Decision 2). The dev-log Correction is honored: **the eval-maturation work (τ + critic-flip) is its
own day, NOT folded into the IaC day.** *(The +1 renumber shifted **day numbers** only; **Item
numbers** and the `dayNN.md` filenames stay as stable IDs — so Item 3 = eval-maturation = Day 8 runs
ahead of Item 1 = IaC = Day 9, and `day08.md` now describes Day-9 IaC work, etc.)*

---

## The plan

### Item 0 — Doc/code hygiene batch — **READY**
Clear-cut corrections of fact (not judgment calls). Do freely; **the README fix must land before
the Day-15 Loom.**
- **`README.md:40` [the important one] — READY.** The architecture table says
  `Chunking | Recursive, 512 tokens, 50 overlap | Fixed-size beats semantic chunking…` — the
  **opposite** of what shipped. Replace with the live truth: hand-rolled **structural** splitter
  (paragraph→sentence) packing to a 512-token budget with 50-token soft overlap (ADR 0014),
  rationale consistent with `spec.md §4.4` (already correct). Most-visible portfolio artifact;
  currently contradicts the shipped decision.
- **`init-db/01-init.sql:40-42` — READY.** Comment "Build it AFTER ingesting … ingest script will
  (re)create this" is false — the HNSW index is `CREATE INDEX IF NOT EXISTS` on an empty table,
  maintained incrementally, never rebuilt-after-bulk (`--truncate` at `ingest.py:725-728` does
  `TRUNCATE … RESTART IDENTITY`, no post-load REINDEX). Fix the comment. (No recall impact — this
  is exactly why local == cloud holds; see Item 2.)
- **Stale `2726` counts — READY.** `docs/plan/day06-golden-blueprint.md:16`,
  `docs/plan/day5-design.md:14,87`. Update to 2745 or mark historical. Leave the genuinely-historical
  sites that already show the 2726→2745 delta (`dev-log.md:74,76,87`, ADR `0014:7`).
- **`docs/plan/day07-priority3-rechunk.md:3,211` — READY.** Present-tense "Nothing cleaned,
  re-chunked, or re-embedded" is now false (Resolution A executed). Add a one-line
  "SUPERSEDED — executed via Resolution A (dev-log 2026-06-03)" pointer at top.
- **dev-log forward-pointers — READY.** `dev-log.md:62` ("re-embed … remain DEFERRED") and
  `dev-log.md:132` ("Nothing re-chunked yet") are self-contradicted by the same log's Resolution A.
  Append a forward-pointer line; do not rewrite history.

> The atomic-swap-vs-`--truncate` ADR mismatch is **not** in this batch — it touches append-only
> ADR bodies and is a judgment call → **Decision 4**.

### Item 1 — Day 9: IaC authoring (Terraform + Dockerfile) — **READY** (stop-condition GATED)
Author from scratch — `infra/terraform/` is an empty directory; no `Dockerfile`/`.dockerignore`
exist yet. Deliverables per `day08.md` plus two Critic additions:
- All `.tf` files, `README`, `.gitignore`, `terraform.tfvars.example`, `Dockerfile`,
  `.dockerignore` (**must exclude `.env`**), secrets via Secrets Manager (not plaintext env).
- **[Critic add] Add an `aws_ecr_repository` Terraform resource.** It's implied by Decision 6
  (build+push the app image) but **absent from the explicit `day08.md` deliverable list** —
  without it, the Day-10 `docker push` has no target.
- **[Critic add] Guard the local==cloud invariant:** wire `embedding_model`/`embedding_dim` from
  the same `config.py` contract (`voyage-3`, 1024) into `variables.tf`/`secrets.tf`/the ECS task
  env — **do not hardcode alternates**, or retrieval silently diverges from local.
- **Stop condition (`terraform init` + clean `terraform plan`) — GATED: needs local AWS creds**
  (`aws sts get-caller-identity`). Authoring the HCL is READY; hitting the stop condition needs
  credentials configured. **Do NOT `apply`** (that's Day 10).

### Item 2 — Day 10: Deploy → smoke → destroy — **GATED**
**Gate: spends real AWS $ + runs a Voyage ingest (`--limit 50`). Set the $5 AWS Budget alert
*before* apply; needs your explicit go/no-go.** Flow: `terraform apply` → bootstrap RDS via
`01-init.sql` → `--limit 50` structural ingest → smoke query → eval-5 → screenshots → `destroy`.
Target < $5. Prereqs: Item 1 complete, clean plan, AWS creds.
- **Reassurance (verified this pass):** the **HNSW index is reproduced cloud-side** —
  `01-init.sql:44-46` creates `chunks_embedding_hnsw_idx USING hnsw (vector_cosine_ops)`; Day-10
  runs `01-init.sql` then ingest; pgvector defaults on both sides; retrieval uses cosine `<=>`.
  **No silent recall divergence between local and cloud.** Day 10 deliberately runs the
  sessions-free `01-init.sql` (smaller blast radius); sessions land after the cloud demo.

### Item 3 — Day 8: Eval-maturation (matcher-v2 → τ-confirm → critic-flip → critic-delta → Langfuse) — **matcher-v2 + τ READY/$0 (Decision 1 SETTLED) · critic-delta + Langfuse GATED**
**Day 8** — the +1 renumber put eval-maturation first: get the matcher as good as it'll get, confirm τ,
flip the critic, measure the delta, then wire Langfuse. Runs **before the IaC/cloud days** (so the
cloud demo shows the matured matcher + faithfulness-aware critic), **ahead of session tracking**. Order
within the day is fixed: **matcher-v2 → τ-confirm → critic-flip → critic-delta → Langfuse.**
- **matcher-preprocessing v2 — READY, $0.** First in the day. Recover the ~9 cleanly-fixable
  near-miss false-negatives (mid-line / glued / line-break-split PDF line-numbers) the Day-7 v1
  `_normalize` missed; **386 → ~395/446**. Touches production `check_citation`, so the hard gate is
  **ZERO regression on the 386** via the $0 text-only smoke. Full scope (in/out, guards, validation):
  [`matcher-preprocessing-v2.md`](matcher-preprocessing-v2.md).
- **τ confirmation — READY, $0. SETTLED (Decision 1): τ stays 0.85.** The near-miss band was matcher
  false-negatives, not a threshold problem — with matcher-v2 landed there is nothing to lower τ *for*.
  The 446-record distribution on disk (`evals/smoke-chunks-detail.json`, post-swap, delta=0 vs
  scratch) is the record; re-confirm the pass count after v2 — no score re-measure, no Voyage, no judge.
- **τ → critic-flip order is HARD-REQUIRED** (ADR 0013: uncalibrated τ + a live critic = revision
  churn on honest boundary-straddle quotes). Calibrate first, then flip.
- **critic-flip — small edit:** `critic.py:260` pass the analyst's parsed quote instead of
  `quoted_text=None`; τ stays **0.85** at `config.py:122` (no value change — additive wiring only;
  ADR 0013 Reopen authorizes; **no ADR-body edit**).
- **critic-delta (post-flip end-to-end validation) — GATED: requires a full golden judge eval run (~$).**
  The matching itself is $0/text-only; the cost is closing the loop with a real eval pass to measure the
  critic's effect on faithfulness.
- **Langfuse scores/datasets — last in the day (Decision 2 SETTLED).** Wire eval scores into Langfuse
  datasets/experiments *after* critic-delta, so it captures **post-flip** scores (not pre-flip numbers
  the flip invalidates). ~1-day effort folded in here (see Item 10); effectively gated behind
  critic-delta's judge run.

### Item 4 — Day 11: Session tracking (ADR 0015) — **READY to build** (closing eval GATED)
Self-contained DB + API change; full plan in `day10-session-tracking.md`. Prereqs: Item 2 (cloud
ran sessions-free) + Item 3 (closing eval baselines against the post-flip critic — one baseline,
not two).
- **[Critic add — enlarges scope] `_ensure_schema` must ALSO backfill `app.query_audit`, not just
  add `app.sessions`.** Verified: `ingest.py:602-630` creates schemas + `corpus.chunks` + indexes
  only; `app.query_audit` lives in `01-init.sql:49-62` but **not** in the runtime ensure-schema —
  so the "`01-init.sql` ↔ runtime mirror" is **already partial today**. Day-11 work: add
  `app.sessions` to **both** `01-init.sql` and `_ensure_schema`, **and** backfill the missing
  `app.query_audit` (+ index) into `_ensure_schema`.
- Plus: fail-open audit writes, additive `/query` contract (return + accept `session_id`),
  `GET /sessions/{id}`, link eval-runs, tests incl. a forced-write-failure proving fail-open.
  Identity stays deferred to ADR 0016 (proposed, unwritten).
- **Closing full eval pass — GATED (judge $)**: `citation_validity ≥ 0.95`, other scorers unmoved.

### Item 5 — Day 12: Design docs (cost-model + identity-design + README diagram) — **READY**
Design-doc deliverable (not code). Soft data deps: infra costs from Items 1–2 + per-session
`token_count` from Item 4. `cost-model.md` ranks levers (caching biggest; Haiku-researcher & critic
cap already done; critic `source_text` truncation ~27% bloat is the cheapest unbuilt lever) —
it **documents, does not implement**, the truncation lever. Compressible (top of the cut-list after
Day-15 stretches).

### Items 6–9 — Days 13 → 16 — **READY**
Day 13 postmortems (3× ~250–400 words, before/after numbers — the Day-7 matcher story is the
strongest) → Day 14 polish (README rewrite; fold in the Item-0 README fix here if not already done)
→ Day 15 Loom (6–8 min) → Day 16 buffer. **Never-cut:** postmortems, Loom, Day-6 evals, Day-5
`check_citation`.

### Item 10 — Langfuse scores/datasets (future-work §14) — **SCHEDULED into Day 8 (Decision 2 SETTLED)**
~1 day, fully parallelizable, no dependents. **Decision 2 settled:** pulled out of future-work into a
real slot — the **Day-8 eval-maturation phase, after critic-delta** (captures post-flip scores, not
pre-flip numbers the flip invalidates). Supersedes the sub-agents' unanimous "keep in future-work,
lowest priority" lean. Caveat: it makes Day 8 heavy (matcher-v2 + τ + critic-flip + critic-delta +
~1-day Langfuse wiring) — treat as the phase order, compressible if Day 8 overruns.

---

## READY / GATED / BLOCKED at a glance

| Item | Tag | If GATED/BLOCKED: the gate / decision |
|---|---|---|
| 0 — Hygiene batch (README:40 etc.) | **READY** | — |
| 1 — Day 9 IaC authoring | **READY** | stop-condition (clean `plan`) GATED on local AWS creds |
| 2 — Day 10 deploy/smoke/destroy | **GATED** | AWS $ + Voyage ingest + $5 budget alert + your go/no-go |
| 3a — matcher-v2 (recover ~9) | **READY ($0)** | hard gate: 0 regression on the 386 (text-only smoke) |
| 3b — τ-confirm + critic-flip | **READY ($0)** | **Decision 1 SETTLED — τ stays 0.85; lever = matcher, not threshold** |
| 3c — post-flip validation | **GATED** | full golden judge eval run (~$) |
| 4 — Day 11 session tracking (build) | **READY** | closing eval pass GATED (judge $) |
| 5 — Day 12 design docs | **READY** | soft data deps on Items 1–2, 4 |
| 6–9 — Days 13–16 | **READY** | — |
| 10 — Langfuse (→ Day-8, post-critic-delta) | **SCHEDULED** | **Decision 2 SETTLED** — pulled into eval-maturation |

**Decisions that gate the above:** D1 (τ target + critic-flip slot) — **✅ SETTLED: keep τ=0.85, lever
= matcher-v2**; D2 (Langfuse) — **✅ SETTLED: pulled into Day-8 eval-maturation, after critic-delta**; D3 (faithfulness residual handling) — **✅ SETTLED: matcher-v2 recovers
~9, accept the rest**; D4 (ADR doc-integrity + scratch-table keep/drop); D5 (cost-rationale fix — no
action). All in [`PENDING-DECISIONS.md`](../decisions/PENDING-DECISIONS.md).

---

## What this pass did NOT do (constraint compliance)

No re-embed, no re-ingest, no table swap/drop, no Voyage calls, no `--save-baseline`, no judge eval
runs, no merge/tag/push, no ADR-body edits. Actions taken were limited to: reading code/docs,
read-only `SELECT` SQL (gate corroboration), the unit test suite (`-m "not integration"`), and
writing this doc + `PENDING-DECISIONS.md`. **System state is exactly as the re-ingest left it.**
