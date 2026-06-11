# Dev log

## 2026-06-11 — Phase 2a: six functional ports + local adapters (ADR 0020)

**Branch:** `refactor/phase2-ports` (off the integration branch). Three implementation
subagents (B1/B2/B3) sequenced by the lead, full test gate between each.

Ports landed in `src/rra/ports/`, local adapters in `src/rra/adapters/`, all as
Protocol + profile-resolved `lru_cache` factory (non-local profiles raise until the
cloud-adapter phase):

- **LLM** → `AnthropicLLMAdapter`. Client construction is the seam; `anthropic.types`
  stay the wire types (ADR 0020 §wire-types — the SDK's own Bedrock/Vertex clients are
  the cross-cloud story). Call sites rewired: 4 agents + evals/judge. Only behavior
  delta: one process-lifetime client instead of per-call construction.
- **Embeddings/rerank** → `VoyageEmbeddingsAdapter` (owns VOYAGE_MAX_BATCH, preserves
  the retry asymmetry: batch-embed retries, query-embed doesn't).
- **Vector store** → `PgVectorStoreAdapter`. All corpus SQL moved verbatim from
  retrieval/tools/ingest. **Review catch:** the first cut passed caller-built SQL
  through the port (a connection factory, not a boundary); reworked so the port takes
  `(embedding, top_k, guidance_ids)` and the adapter owns SQL + vector literals
  (commit `0dc7e7e`). `/readyz`'s pool probe in api.py stays outside the port (health,
  not corpus).
- **Memory/state** → `PostgresStateAdapter` (graph checkpointer moved verbatim).
- **Tool transport** → `InProcessToolTransport`, single `call_tool(name, args)`
  chokepoint (MCP-native shape; the identity port enforces scoping here in phase 2b).
- **Observability** → `LangfuseObservabilityAdapter` + `NoopObservabilityAdapter`
  (replaces the `if lf is not None`/nullcontext dances; langfuse imports now confined
  to the adapter + the exempted eval harness). `search_corpus(lf=...)` param kept but
  deprecated (frozen contract).

**Gates:** 310 passed (262→310 with port tests), mypy clean on touched files, grep
gates verify SDK-construction confinement. Eval runs still deferred (credits); the free
CI citation gate runs on the PR.

## 2026-06-11 — Phase 1: RRA_PROFILE config/profile system

**Branch:** `refactor/phase1-profile-system` (based on `refactor/ports-adapters-security`)

### What landed

- **`RRA_PROFILE` field** added to `Settings` (`Literal["local","aws","azure","gcp"]`, default `"local"`).
- **`PROFILE_DEFAULTS` registry** + `@model_validator(mode="before")` in `config.py`: per-profile
  config defaults injected only for fields absent from env + .env. Strict precedence: explicit env >
  .env > profile default > field default. The `local` profile reproduces today's field-level defaults
  exactly — behaviour is byte-identical when `RRA_PROFILE` is unset or `"local"`.
  `aws`/`azure`/`gcp` entries are empty dicts; adapter phases (Phase 2+) fill them.
- **Conftest `.env`-seed fix**: `tests/conftest.py` now seeds `os.environ` from the project `.env`
  (via `dotenv_values`) before applying stub defaults. Stubs apply only when a key is absent from
  both shell and `.env` — the CI case. Fixes the pre-existing footgun where stub values shadowed real
  keys (env beats .env in pydantic-settings), causing live-DB integration tests to fail without
  manual key export.
- **`tests/test_no_secret_leak.py`** (14 tests): pins that `repr`/`str`/`model_dump`/`model_dump_json`
  never expose raw `SecretStr` sentinel values; `pg_dsn` is the documented exception (it is a plain-str
  computed field that must contain the password); structlog capture confirms no sentinel leaks into
  log events; profile-system invariants (default profile, precedence, valid profiles, no secrets in
  `PROFILE_DEFAULTS`) are all tested.
- **ADR 0019** (`docs/decisions/0019-rra-profile-config-system.md`): documents context, decision,
  alternatives, consequences, and a Security implications section. ADR index updated.

### Test results

`uv run pytest tests/ -q --no-cov` → **246 passed** (232 original + 14 new), no failures.

### Conftest fix rationale

pydantic-settings source priority: env vars > .env file > defaults.
`os.environ.setdefault(key, stub)` writes to the env-var source — it wins over the .env file.
Result: real developer credentials in `.env` were shadowed by stubs, so any test that hit a live
service saw stub values and failed (Voyage `pa-test`, Postgres `test-postgres-password`).
Fix: read the `.env` into `os.environ` via `dotenv_values` BEFORE calling `setdefault`, so real
credentials win when present. CI has no `.env`, so stubs still apply there. `dotenv_values` is a
direct dep of python-dotenv (already in the lock file as a dep of pydantic-settings).

### Deviations from brief

- `retrieval.py` line ~151 already used `settings.rerank_model` (not a literal `"rerank-2"`).
  The Phase 0 dev-log entry documented a pre-existing issue, but it had already been fixed.
  No change needed. Verified with `grep -rn '"rerank-2"' src/` — the only occurrences are the
  field default in `config.py`.

## 2026-06-11 — Refactor kickoff (Phase 0): orient + baseline on `refactor/ports-adapters-security`

Autonomous run start for the ports/adapters/profiles + security refactor (see CLAUDE.md north-star
section, committed as the first commit on the integration branch).

### Branch decision (unilateral, logged per working-style rule)

Integration branch **`refactor/ports-adapters-security`** is based on **`docs/postmortems`**, not
`main`. Rationale: `docs/postmortems` is a strict superset of main (6 commits, doc-only plus
`7696564` which adds the POSTGRES_PASSWORD/RRA_API_KEY stubs the eval-gate CI workflow needs to
pass). Basing on main would have made every phase PR fail CI until that fix merged. No code
diverges; when `docs/postmortems` merges to main this collapses to a normal main-based branch.

### Preconditions verified

- `CRITIC_FORCE_VERDICT`: **unset** in shell (`printenv`) and **absent** from `.env` (checked key
  names only; values never read into the session).
- Local stack: `docker compose up -d` → all 7 services healthy; corpus present
  (`corpus.chunks` = 2,745 rows).

### Green baseline (commands + results)

- **Unit + integration tests:** `uv run pytest tests/ -q` → **232 passed** (with `.env` exported).
  Note: a bare `pytest` run fails `test_search_corpus_returns_results_from_live_db` because
  `tests/conftest.py:23-24` stubs `ANTHROPIC_API_KEY`/`VOYAGE_API_KEY` via `os.environ.setdefault`,
  and an env var (even a stub) beats the `.env` file in pydantic-settings. Pre-existing footgun,
  not a regression; candidate fix in the config/profile phase.
- **Eval harness, CI mode (free, no LLM calls):**
  `uv run python -m rra.evals.run --fixture ci-valid --no-llm-judges --tag refactor-baseline-ci`
  → citation_validity **1.000 PASS** (`evals/results/20260611T034138Z-refactor-baseline-ci.md`).
- **Eval harness, full golden:** `uv run python -m rra.evals.run --tag refactor-baseline`
  → **PARTIAL**: 24/30 scored, citation_validity **0.930**, 6 cases errored with Anthropic
  **"credit balance is too low"** mid-run (`evals/results/20260611T040923Z-refactor-baseline.md`).
  The errors are account-credit exhaustion, not system behavior. Last clean full-run anchor stays
  **0.972** (critic ON, 2026-06-05, eval-maturation work).

### Eval policy for the rest of this run (human directive)

Owner directed: **no further eval runs** this session (API credits exhausted). Per-phase gates are
therefore: unit tests locally + the free CI-mode citation gate in GitHub Actions on each PR. The
full golden eval re-run to prove no regression vs baseline is **deferred until credits are
restored** — it stays on the final-phase checklist, explicitly owed.

### Current-state inventory

Captured (boundaries, config-vs-hardcoded, secrets/PII flow, Fargate path, identity state); lands
in `docs/refactor/00-implementation-report.md` with Phase 1. Headlines: config is already fully
centralized (no `os.getenv` outside `src/rra/config.py`); MCP tools are in-process (ADR 0011) with
**no scoping**; identity is a single `X-API-Key` compared non-constant-time (`src/rra/api.py:77`);
`rerank-2` model string hardcoded at `src/rra/retrieval.py:151`; Terraform state is empty (nothing
live, deploy-and-destroy honored).

## 2026-06-08 — ALB 503 hunt: wrong Dockerfile stage shipped, then a pinned deployment hid the fix

**Branch `feat/private-rds-bootstrap`** (PR #7). After the bootstrap work landed, the deployed app
returned **503 from the ALB on both `/health` and `/readyz`** — no healthy target. Read-only AWS triage
(target health → service events → stopped tasks → CloudWatch) found the ECS service **crash-looping**:
`runningCount=0`, tasks living ~60 s (start → register → deregister/drain → restart), each exiting `1`.
The damning log line: the `app` container wasn't running uvicorn at all — it was running **`rra.ingest`**
(`corpus.download` → `failed=50 succeeded=0` → `corpus.ingest.no_files_downloaded` → exit 1, the ADR 0018
FDA-IP block). uvicorn never started → nothing on `:8000` → no healthy target → 503.

**Root cause #1 — wrong Dockerfile stage at `:latest`.** The serving task def (`rra-demo:1`, no `command`
override) ran the image's own entrypoint, which was `python -m rra.ingest` — i.e. the `:latest` image was
built from the Dockerfile's **last stage (`bootstrap`)**, not `runtime`. A bare `docker build .` defaults
to the last stage; the serving image needs `--target runtime` (uvicorn `CMD`). ECR confirmed: no serving
image had ever been pushed — only ingest builds and `:bootstrap`.

**Root cause #2 — the running deployment pinned the old digest.** Rebuilt `--target runtime`, smoke-tested
locally (`docker inspect` → `CMD=[uvicorn …]`; ran it → saw "Uvicorn running"), and pushed the correct
uvicorn image to `:latest` (digest `08b9657…`). **Still 503.** `describe-tasks` showed the tasks' *resolved*
`imageDigest` was `31c0596…` — the original 17:15 ingest image — even for tasks launched *after* the push.
An **ECS service deployment resolves its image tag to a digest once, at deployment creation, and pins it**;
new `:latest` pushes are invisible to a live deployment. Every "push didn't help" was this, not a bad image.

**Fix:** `aws ecs update-service … --force-new-deployment` → new deployment re-resolved `:latest` →
`08b9657…` (uvicorn) → target went **`healthy`**, `/readyz` → `{"status":"ready"}` (DB up + `corpus.chunks`
present, so bootstrap had run), and `/query` returned a full multi-agent RAG answer with citations
(80958, 109618, 153781). End to end working.

**Aside (502 vs 503):** a `curl` mid-saga returned **502**, not 503. Same root cause, different moment in
the loop: **0 registered targets → 503**; a doomed task briefly *registered* but with nothing listening on
`:8000` → ALB gets a connection refusal → **502**. Not progress — just timing.

**LESSON:** two traps compounded. (1) A multi-stage Dockerfile whose **last stage isn't the default you
want** silently ships the wrong image on a bare `build` — reorder so `runtime` is last, and gate pushes on
`docker inspect … CMD`. (2) **`:latest` + a running ECS service hides your deploys** — the deployment pins a
digest, so re-pushing the tag is a no-op until `--force-new-deployment`. Both vanish if you deploy
**immutable tags** (`serve-<sha>` via `app_image_tag`): a push *is* a task-def change, digest pinning can't
mislead, and the running image is unambiguous.


## 2026-06-08 — First cloud deploy: FDA blocks the Fargate IP → ADR 0018 (bake the corpus)

**Branch `feat/private-rds-bootstrap`** (PR #7). `terraform apply` succeeded (44 resources); pushed
both images; ran the bootstrap task. **It failed: 50/50 FDA downloads 4xx-failed instantly** (~25 ms
each, non-retryable — so a 4xx, not a NAT/timeout). Diagnosis: the same URL returns `200` from a
residential IP but is blocked from the Fargate datacenter IP (Akamai blocks datacenter ranges). The
in-VPC re-download path from ADR 0017 had **never been exercised** — local ingest short-circuited on
`dest.exists()` because `data/corpus/` was pre-populated, so the live fetch was untested until cloud.

**Fix — ADR 0018 (supersedes 0017):** bake the cached PDFs into the bootstrap image instead of
re-downloading. ingest then serves from the on-disk cache. Verified the cache covers all 50 ingested
docs (and all 72 manifest entries); rebuilt `--target bootstrap` and confirmed 72 PDFs in the image.
Changes: `.dockerignore` no longer excludes `data/corpus/*.pdf`; Dockerfile + `.dockerignore` comments
updated; ADR 0017 → Superseded, ADR 0018 → Active. Serving image untouched (COPYs no `data/`), so only
`:bootstrap` needs a rebuild/re-push. NAT egress is still required (Voyage embeddings).

**LESSON:** a cache short-circuit (`dest.exists()`) can hide an un-exercised network path straight
through to production. The local "success" proved nothing about the live download — the cloud was the
first real test. Bake immutable inputs; don't re-fetch a public CDN from a datacenter IP at deploy time.

**Note on `--limit 50`:** that's `var.bootstrap_ingest_command`'s demo default, NOT the manifest size
(72). Set it to `[]` for the full corpus (+ re-apply to update the task def); the image already carries
all 72 PDFs regardless.


## 2026-06-08 — Pre-deploy static analysis + private-RDS bootstrap (ADR 0017)

**Branch `eval-maturation`** (analysis cut at `bda1576`; infra files identical on `main`). Read-only
pre-deploy pass over Dockerfile / Terraform / app config to predict first-deploy connectivity. The
serving stack checked out clean — SG chain (ALB→ECS→RDS), NAT egress, DATABASE_URL assembled from
`POSTGRES_HOST`=RDS address (no localhost fallback), secrets from Secrets Manager, uvicorn `0.0.0.0`,
Langfuse a clean no-op when disabled, `CRITIC_FORCE_VERDICT` absent from cloud env — all correct.

**One blocker (B1):** RDS is private and nothing in the IaC creates the `vector` extension /
`corpus.chunks` / corpus, and the laptop can't reach the endpoint. `/health` is DB-free, so the task
goes healthy and the **first `/query` 500s** on `relation "corpus.chunks" does not exist`. The serving
image can't self-bootstrap (`.dockerignore` drops `data/`; `PROJECT_ROOT` mis-resolves in the wheel
layout). Two related warnings logged for later: lazy DB hides connectivity until first query (suggest
a `/readyz` with `SELECT 1`), and `lf.flush()` is synchronous in the request path (only bites if
Langfuse is enabled pointing at an unreachable host).

**Decision — ADR 0017 (Active): bootstrap via a one-off in-VPC ingest task.** A second `bootstrap`
Dockerfile target ships source + `data/corpus/manifest.json` and installs the project EDITABLE
(`PROJECT_ROOT`→`/app`); `rra.ingest` is schema-sufficient (its `_ensure_schema()` creates the
extension + schema + indexes, then embeds). Run once via `aws ecs run-task` (`bootstrap.tf`) into the
private subnets on the existing `ecs_tasks` SG — RDS already trusts it, NAT reaches FDA + Voyage, no
new network rule/repo/standing resource. Rejected: bastion, public-flip, ECS-Exec, startup migration.

**Implemented + verified:** `Dockerfile` (`bootstrap` target), `infra/terraform/bootstrap.tf` (task def
+ log group), `outputs.tf` (`bootstrap_run_task_command`), `variables.tf` (4 bootstrap vars),
`.dockerignore` (exclude only `data/corpus/*.pdf`, keep the 37 KB manifest), README runbook + ADR 0017
+ index. `terraform validate` passes. Built `--target bootstrap` and ran it: manifest present, PDFs
absent (re-downloaded in-VPC at runtime), `PROJECT_ROOT=/app`, `ingest --help` works.

**Two bugs caught only by building the image (not by review):**
1. Inline `# comment` on a `COPY` line is parsed as COPY args (Dockerfile, unlike RUN, has no inline
   comments) → build failed. Moved comments above the instruction.
2. `config.Settings()` is instantiated at import (`config.py:150`) and `ANTHROPIC_API_KEY` is a
   *required* field with no default — so even though ingest never calls Anthropic, `import rra.config`
   fails fast without it. Added `ANTHROPIC_API_KEY` to the bootstrap task secrets (Voyage + Anthropic
   are the only required keys; `RRA_API_KEY`/`POSTGRES_PASSWORD` have defaults).

**LESSON:** "config fails fast at import" means a job that uses *none* of a required secret still needs
it present. Trim the secret set to what the *code path* uses and you trip the *import-time* validator.

### Open follow-ups

1. ~~`/readyz` so RDS connectivity surfaces at boot, not on first query (W1).~~ **DONE** — added
   `GET /readyz` (`api.py`): borrows the request-path pool and runs
   `SELECT to_regclass('corpus.chunks')` — one round-trip that is both the connectivity check (raises
   → 503 "database unreachable") and the "did the bootstrap run?" check (NULL → 503 "corpus not
   initialized"). 200 `{"status":"ready"}` otherwise. Deliberately NOT the ALB liveness probe (that
   stays DB-free `/health`); it's a smoke-test / on-demand probe. Skips `register_vector` (SQL-only),
   and does NOT count rows (empty corpus serves 200s — degraded, not broken). This closes the gotcha
   the connectivity-only first cut missed: bootstrap skipped → `/readyz` green → `/query` 500s.
   Tests: reachable+present→200, unreachable→503, corpus-absent→503, plus a guard that `/health`
   never touches the pool.
2. `app.query_audit` is not created by the bootstrap flow and not written at runtime — wire it up or
   leave it. Not on the `/query` path either way.
3. Bootstrap reuses the app ECR repo with a `:bootstrap` tag (vs. a second repo) — revisit if it gets
   confusing.


## 2026-06-05 — eval-maturation: trace the LLM judges + pricing audit (close the $2.26 cost gap)

**Branch `eval-maturation`**, HEAD `bda1576`. Follow-up on the previous entry's LESSON ("Langfuse
cost is a FLOOR"): make traced cost trustworthy by (A) tracing the two LLM judges and (B) auditing
model pricing. No ADR — observability plumbing, no trace/span model change.

### Part A — judges now attach to the per-case trace (DONE, unit-tested)

The judges fire in the scorer layer AFTER `run_agent` closes the `eval-case` span (`run.py:231`), so
there is no live span to nest under. Mirrored `emit_scores`' approach: attach to the *closed* trace
by id rather than opening a child span.

- `judge.py`: `judge_call(model, prompt, prefill=None, trace_id=None)` now captures `msg.usage`
  (previously discarded) and, when `trace_id` is set, calls `_emit_judge_observation` →
  `lf.start_observation(trace_context={"trace_id": …}, as_type="generation", model=…,
  usage_details={"input":…, "output":…})` then `.end()`. The `model` + `usage_details` are what make
  Langfuse COMPUTE the judge's cost.
- `scorers.py`: both LLM judges pass `trace_id=response.raw_trace_id` (already carried to the scorer
  on the `AgentResponse`) — no `run.py`/`run_eval` signature changes; `score()` signature unchanged.
- **Gating (both natural no-ops):** `raw_trace_id` is non-None only on `--allow-population` runs, and
  `get_langfuse()` returns None when keys are absent. So `judge_call` still works standalone with
  zero Langfuse side-effect. Emission wrapped in try/except — a tracing hiccup can never fail a judge.
- Tests: `tests/test_evals_scorers.py` — scorer threads `raw_trace_id`; `judge_call` emits a priced
  generation when a trace is active; hard no-op when `trace_id=None` or Langfuse disabled. Full suite
  227 passed (1 pre-existing live-DB/Voyage-key failure, unrelated). Free CI eval green
  (`--fixture ci` exits 1 gate-bite, `ci-valid` exits 0).

### Part B — pricing audit: the gap was 100% judges, NOT unpriced models

Enumerated distinct model strings across all observations in both run traces
(`critic-delta-arm1-forced-approve`, `critic-delta-arm2-live-critic`):

| Model | Observations | Priced |
|---|---|---|
| `claude-haiku-4-5` | 233 | 233 (0 null) |
| `claude-sonnet-4-6` | 200 | 200 (0 null) |

Both already have Langfuse price entries (`claude-haiku-4-5` matched by the
`claude-haiku-4-5-20251001` regex; `claude-sonnet-4-6` exact). **cost.py's cause-(2) hypothesis
(unpriced graph models) does NOT apply to these runs** — every observation was priced. The judges
reuse those same two model strings, so once Part A traces them they price automatically — **no new
model definitions were needed.** cost.py re-pull confirms arm1 $1.7496 + arm2 $6.9008 = **$8.65**
(unchanged; old runs' judge calls were never traced and can't be retroactively reconciled). So the
full **$2.26 gap = untraced judges**, which Part A closes for future runs.

**Latent gap (flagged, not fixed):** no `claude-opus-4-8` definition exists in Langfuse (only opus
`4-5`/`4-6`/`4-7`). No project role uses opus-4-8 today, so it doesn't affect this eval — but if a
role ever switches to it, its observations would price to null. Future-work note.

### Not yet done

Paid validation deferred at user's request — wiring is unit-test verified but NOT yet proven on a
live run. To confirm judge spend lands + prices in Langfuse, run a cents-scale smoke:
`uv run python -m rra.evals.run --limit 1 --allow-population --run-name judge-trace-smoke-<ts>`,
then re-pull `cost.py` and confirm 2 priced `llm-judge` generations on the case trace.


## 2026-06-05 — eval-maturation: critic-delta PAID 2-arm eval (ADR-0009 "is the critic theater?")

**Branch `eval-maturation`**, HEAD `bda1576`. Full 30-case golden set run twice, judges ON, both
arms published to Langfuse Datasets→Runs (dataset `rra-golden-eval`). Staged/gated paid execution
(~$20 ceiling); each arm green-lit individually.

### Result — the critic moves citation_validity materially

| Scorer | Arm 1 critic OFF | Arm 2 critic ON | Δ |
|---|---|---|---|
| citation_validity (HARD 0.95) | 0.842 (6.7% pass) | **0.972** (73.3% pass) | **+0.130** |
| key_fact_coverage (warn 0.80) | 0.783 | 0.808 | +0.025 |
| position_quality (warn 4.0/5, normalized) | 0.913 (≈4.56/5) | 0.960 (≈4.80/5) | +0.047 |

Both arms 30/30 scored, 0 errors. Quote-faithfulness: arm1 385/455 verified (84.6%), arm2
449/461 (97.4%); no-quote citations 6→10 under the live critic. Reports:
`evals/results/20260605T235950Z.md` (arm1), `evals/results/20260606T004425Z.md` (arm2). Run-names:
`critic-delta-arm1-forced-approve`, `critic-delta-arm2-live-critic`.

The +0.130 lift clears the ≥2% "critic earns its cost" bar by a wide margin — but the verdict call
is the user's, made in a separate planning session. Not asserting it here.

### Actual cost — $10.91 total (Anthropic Console = ground truth)

Reconciliation: Langfuse-traced graph cost was arm 1 **$1.7496** + arm 2 **$6.9008** = **$8.65**;
the Console bill was **$10.91**, leaving a **$2.26 untraced gap**. Under the $12–16 estimate and
the $20 ceiling. Arm 2 ~3.9× arm 1 on the graph (live critic ~17–23k input tokens/pass + revision
re-runs).

**LESSON — Langfuse-computed cost is a FLOOR, not the bill.** I estimated the untraced portion at
~$0.65 and reported "~$9.3 total"; the real untraced spend was **$2.26** (~3.5× my estimate). Two
causes: (1) the two LLM judges (`KeyFactCoverageScorer` Haiku-4.5, `PositionQualityScorer`
**Sonnet-4.6**) run in the scorer layer AFTER `run_agent` closes the per-case trace (`run.py:231`)
and `judge_call` (`judge.py:25`) opens no span — 120 judge calls (2/case × 30 × 2 arms) are
entirely untraced, and the Sonnet judge over full answer text is the dominant cost; (2) `cost.py`
only checks each trace has a non-null `total_cost`, not that every nested observation was priced,
so any graph model lacking a Langfuse pricing entry makes even the $8.65 a floor. To trust
Langfuse for spend in future: trace the judges (or price them separately) AND verify every model
has a Langfuse pricing entry. Treat the Console as the billing source of truth.

### Follow-up (open) — revision cap may be biting before convergence

Arm 2: **14/30 cases hit `cap_hit=True`** (39 `revise` vs 46 `approve` verdicts across all passes).
On ~47% of cases the critic was still demanding revisions when `max_critic_revisions` ran out.
Open question: does raising the cap lift citation_validity further, or just burn tokens on cases the
critic will never approve? Candidate for a future cheap-ish targeted run.

### Process notes (operational, for repeatability)

- **Workspace usage-limit footgun:** first two arm-1 launches failed 30/30 with HTTP 400
  "workspace API usage limits … regain access 2026-07-01" at **$0** (400s are pre-billing). Root
  cause was an org/wrong-workspace limit, not code. The per-case 400s land in the REPORT file, not
  stdout — a log-only grep gave a false "healthy" reading once; authoritative fail signal is the
  log's `Langfuse sync … 'skipped_no_trace': 30` + a `Report:` line within ~2s of launch.
- `CRITIC_FORCE_VERDICT` set ONLY via inline command-prefix on arm 1 (never exported); verified
  empty in shell before each arm and `.env` stayed commented. No leak across arms.
- Runs backgrounded with `nohup … & disown` (detached, survive polling timeouts); PID+log on disk;
  a `run_in_background` watcher blocks on the PID and notifies on exit.


## 2026-06-04 — eval-maturation: test-isolation guard + Langfuse cleanup (end of session)

**Branch `eval-maturation`**, commit `6b53329` (pushed).

### Langfuse tracing verified intact

Phase 7 parenting confirmed at trace `9ee44be4` (easy-001): 49 observations, 1 root `eval-case` span, 48 children properly nested, sessionId present, linked to dataset-run `cheap-validate-20260604T173754Z` (both easy-001 and easy-002 present). The apparent "regression" was test pollution, not a code regression.

### Root cause of the orphan traces

pytest run with live Langfuse keys in env (loaded from `.env` via Pydantic Settings). `test_graph.py` force-verdict tests (`test_force_verdict_revise_hits_cap`, `test_force_verdict_escalate_exits_immediately`) patch planner/researcher/analyst but **not** `run_critic`, so the real critic node ran and called `get_langfuse()` — emitting orphan spans (null parent, null session) with synthetic fixture inputs (`gd-001`, `Draft answer.`, `Refusal text.`). Other test files emitted additional orphans via mocked `run_agent` calls (agent/api tests).

### Fix: `tests/conftest.py` session-scoped autouse isolation guard

Session-scoped autouse fixture runs before any test:
- `get_langfuse.cache_clear()` — the non-obvious necessary piece; the `@lru_cache` would otherwise serve a live client cached during module import if any code had called it before the fixture ran.
- `settings.langfuse_public_key = None`, `settings.langfuse_secret_key = None` → `langfuse_enabled → False` → all subsequent `get_langfuse()` calls return `None`.
- `settings.critic_force_verdict = None` — guards against ambient `.env`/shell leakage; function-scoped `monkeypatch` in individual tests still overrides and restores correctly.

`test_langfuse_disabled_in_test_session()` added to `test_graph.py` as regression guard — fails immediately if the conftest fixture is removed or broken. Suite: **223 passed** (1 pre-existing integration failure: `test_search_corpus_returns_results_from_live_db` hits live Voyage API with a stub key; fails identically with and without the guard; unrelated).

### Cleanup: 34 fixture orphan traces deleted

All 34 traces timestamped `2026-06-04T17:40:40Z` deleted via Langfuse API. Synthetic inputs: `gd-100`, `gd-999`, `q`, `x`, `test`, `anything`, `Bogus claim`, `sub_questions: ['q1','q2']`, etc. Legitimate validate traces (`easy-001`, `easy-002`) confirmed surviving with full observation counts.

### CRITIC_FORCE_VERDICT swept clean

Not set in shell env, `.env` (confirmed via runtime settings check), `config.py` default (`None`), or `.env.example` (commented-out). Critic-delta paid eval is safe to run.

### Next

**critic-delta** — paid, ~$3–6, two arms (`arm1-forced-approve` / `arm2-live-critic`), diff `citation_validity` to measure the critic-flip's effect. ADR-0009 theater verdict check. Deferred to a fresh session.

---

## 2026-06-04 — eval-maturation Steps 2+3: τ-confirm (keep 0.85) + critic-flip (faithfulness-aware)

**Branch `eval-maturation`**, off matcher-v2 (6836cb4). `CRITIC_FORCE_VERDICT` confirmed UNSET
before and after. **Flip-only — no paid eval; the citation_validity / critic-delta (Step 4) is
deferred for human review of this diff.**

### Step 2 — τ-confirm: KEEP τ = 0.85 (no config change)
Re-ran the $0 text-only smoke (`smoke_rechunk --table chunks`) against the post-v2 live corpus:
**402/446** verified (best-chunk == doc-level, gap 0) — reproduces the matcher-v2 number exactly.
The `best_chunk_score` distribution (n=446) is sharply bimodal with an **empty valley straddling τ**:

| band | n |
|---|---|
| ==1.0 | 328 |
| 0.99–1.0 | 56 |
| 0.95–0.99 | 14 |
| 0.90–0.95 | 1 |
| 0.85–0.90 (just above τ) | 3 |
| 0.80–0.85 (just below τ) | 2 |
| 0.70–0.80 | 2 |
| 0.50–0.70 | 31 |
| <0.50 | 9 |

- **384 of 402 verified land ≥0.99** — the v2 recoveries are exact-after-denoise, nowhere near τ,
  so they exert zero pull on the threshold (as predicted in the highest-risk flag).
- **τ bisects a 0.037-wide empty gap**: nearest below = medium-001 @0.835, nearest above =
  medium-014 @0.872; nothing lives in [0.835, 0.872]. Any τ in ~[0.836, 0.871] yields the identical
  verdict — maximally robust placement.
- **Sensitivity is flat**: τ=0.80→404, 0.85→402, 0.90→399 (at most +2/−3 across the whole band).
- The 4 sub-τ cases are **genuine borderlines, not recoverable matcher noise**: medium-004 @0.759
  (ellipsis stitch #9) and hard-005 @0.743 (inline enumerator #13) — both documented D3 residual —
  plus two partial-divergence cases (easy-008 @0.815, medium-001 @0.835). Lowering τ would launder
  real faithfulness misses into passes (the Day-7 lesson). **Nothing argues for a change.**

### Step 3 — critic-flip: parse-then-flip, single file (`src/rra/agents/critic.py`)
Wired the analyst's emitted `<q>…</q>` supporting quote into `check_citation` so the critic's
`revise` verdict is now faithfulness-aware (activates ADR-0010 matching / ADR-0013 quote-
faithfulness at the critic's point in the graph). **Not a graph change** — the quote already
reached the critic in `state["draft"]`; the critic previously discarded it at parse time.

- **3a — parser reuse + de-dedup.** Replaced the address-only `_citation_re` (captured only
  `(guid, idx)`, deduped to a SET) with the SHARED `rra.citations.parse_answer` (the same parser
  used by `api.py:89` and `run.py:90`) → `(guid, idx, quote|None)` triples. Dedup now on the
  **full triple** (`dict.fromkeys`, order-preserving): the old address-only set collapsed a
  same-chunk/different-quote pair (one faithful, one not), which matters once the quote is what
  gets checked. Exact-duplicate triples still collapse to one DB read + one `<check>`.
- **3b — quote passthrough.** `check_citation(..., quoted_text=quote)`. A bare citation (no `<q>`,
  allowed by analyst rule 6 → `parse_answer` yields `None`) stays key-existence mode, so a valid
  bare address is never a faithfulness failure.
- **3c — de-conflate the two failures.** `verified="false"` split into an ADDRESS failure
  (`source_text==""` → `reason="address_not_found"`) and a QUOTE-FAITHFULNESS failure (chunk
  present, quote not matched → `reason="quote_unfaithful"` + similarity score). Each `<check>`
  now carries `check="address|quote"`. System prompt + per-call instruction reworded so the
  critic distinguishes them, treats `quote_unfaithful` as a fixable hard-severity revise (not
  escalate), and does not flag a bare-but-valid address as unfaithful.

**Implementation choice (logged):** dedup on the full `(guid, idx, quote)` triple rather than
literal per-occurrence — preserves the distinct-quote fix while avoiding redundant identical DB
reads / `<check>` entries. Not architectural; within ADR 0010/0013. The `source_text==""`
discriminator for address-vs-quote failures relies on `check_citation`'s contract (NOT_FOUND →
empty `source_text`; every other path returns the stored chunk text).

**Validation ($0):** 6 new unit tests (`TestCriticFlipFaithfulness`) pin the deterministic
pre-validation contract — faithful→clean quote check, unfaithful→`quote_unfaithful`, bare→address
key-existence, address-vs-quote de-conflation, distinct-quote de-dedup, exact-dup collapse —
with `check_citation` mocked (no PG). **Existing critic/agent tests green; 190 non-integration
tests pass (+6, zero regressions); mypy clean.** No config change, no new deps, no graph change.

**Pending (human review gate):** Step 4 critic-delta — a paid full-harness run to measure the
flip's effect on `citation_validity`. Not run; awaiting review of this diff.

## 2026-06-03 — eval-maturation Step 1: matcher-preprocessing v2 (386 → 402/446, 0 regressions)

**Branch `eval-maturation`** (off `main`). Implemented matcher-v2 in `_normalize`
(`src/rra/mcp_server/tools.py`) — the narrow line-number / word-split recovery scoped in
`docs/plan/matcher-preprocessing-v2.md`. **No τ change, no analyst-prompt change, no critic-flip,
no re-embed, no Langfuse.** `CRITIC_FORCE_VERDICT` confirmed UNSET before the smoke.

**Result (`smoke_rechunk --table chunks`, $0): 386 → 402/446 faithful, ZERO regressions** (row-by-row
vs `main`'s committed detail: 16 failing→passing, 0 passing→failing). Every recovery lands at **≥0.99**
— exact matches once pypdf line-numbers are removed, not near-τ laundering.
- **9 from the [0.70,0.85) near-miss band** (as planned): mid-line line-numbers (`regulatory \n376
  action`), digits glued to words/parens (`only16`, `3500A)74`, `AI-DSFs)3`, `mode31`), the intra-word
  split (`Q-S ubmission`→`Q-Submission`), the hyphen-fused line-number (`Q-220 Submission`→`Q-Submission`).
- **7 bonus from the <0.70 band** — same noise morphology, more line-numbers per quote (easy-003
  "Minor change" bullets w/ `855/856/867/868`, footnote `list.15`). The ~9 estimate was conservative;
  the rules generalized to multi-noise quotes without over-reaching.

**Stayed residual (as planned, D3):** ellipsis stitch (#9, medium-004, 0.759), footnote-splices
(#4/#7), inline enumerator (#13, hard-005 `a. Software`, 0.743 — deferred, not chased).

**Guards carry the load now that the `\n` anchor is gone:** reg-word backward+forward guards keep
`21 CFR 803.52` (both `21` and `803` survive), `Part 11`, `Form FDA 3500A`, `Title 21`; a unit/measure
denylist keeps `within 30 days` / `10 mg`; a 2–3-digit cap keeps 4-digit content (`1995`, `ISO 9001`);
case-gating keeps `COVID-19`, `N95`, `Type-A submission`, `Part11`. 18 content-safety/recovery unit
tests added (`tests/test_mcp_tools.py`); full suite green (184 passed). **Documented blind spots** (held
safe by symmetric normalization + the zero-regression smoke): a 2–3-digit count before an unlisted noun,
alphanumeric IDs like `p53`, a real cap-hyphen-cap pair before a lowercase word (`X-Y coordinate`).

**The zero-regression gate caught a real bug mid-build:** the first mid-line rule stripped the *leading*
title number in `21 CFR Part 820` (the `(?<!CFR)` guard only protects numbers *after* CFR). An existing
span-recovery test failed → added a forward reg-word guard and strengthened the CFR/USC tests to
full-string equality. Exactly the discipline working: net `+N` is not enough; the row-by-row `−0` is.

## 2026-06-03 — Day 9 build (unattended): Terraform IaC + Langfuse eval integration

**Where we are.** Autonomous build session on `day09-iac-langfuse`. Both Day-9 deliverables
built BUILD-ONLY via architect → engineer → independent-critic passes; nothing applied or
populated. Full write-up: [`docs/plan/session-report.md`](plan/session-report.md). Two commits
(`feat(evals)` Langfuse, `feat(infra)` IaC), **not pushed**.

### Built
- **IaC** (`infra/terraform/`): VPC (2-AZ, single NAT) · security-group chain (ALB→ECS→RDS) ·
  RDS Postgres 16 (encrypted, private) · **ECR** · Secrets Manager (API keys + generated DB
  password) · ECS Fargate (cluster/taskdef/service) + ALB/TG/listener · IAM least-privilege ·
  CloudWatch logs · outputs/vars/README/lockfile. Plus repo-root **Dockerfile** (multi-stage uv,
  non-root, no `.env`) + `.dockerignore`. `terraform fmt` clean · `init` (aws v5.100.0, random
  v3.9.0) · **`validate` → Success**. NOT applied (no creds; that's Day-10).
- **Langfuse** (`src/rra/evals/langfuse_eval.py` + `run.py --langfuse-sync`): dataset push +
  per-case scores linked to a trace, reusing the shared `tracing.get_langfuse()` client. 21
  mocked unit tests; **population gated** behind `POPULATION_GATED` until the critic-flip.

### Unilateral implementation decisions (no new ADR — implements settled spec §4.10 + PENDING D2)
- **`GET /health`** added to `api.py` (unauthenticated, no DB): the ALB target group needs a GET
  health check; `/query` is POST + API-key-gated. ALB health-check path defaults to `/health`.
- **`enable_langfuse` toggle (default off)** in the IaC: Secrets Manager rejects an empty
  `SecretString`, so the Langfuse secrets are created only when explicitly enabled — otherwise a
  default apply would fail. (Critic Bug A. The "filter on the value" alternative was rejected:
  it would taint the `for_each` key set as sensitive.)
- **`health_check_grace_period_seconds = 60`** on the ECS service (critic Bug B — avoid a cold
  first-boot crash-loop).
- **Langfuse trace model:** post-hoc one `eval-case` span per case (the graph gets no trace_id in
  eval), mirroring api.py's idiom; scores attach to that span's trace. Sync is **best-effort**
  (a Langfuse outage never fails the eval). Dataset name `rra-golden-eval`.
- **Env/tooling:** Terraform 1.15.5 downloaded to `~/.local/bin` to run `validate` ($0).
  `ecr.tf`/`iam` split: ECR is its own file; IAM lives in `ecs.tf`. Single NAT + single-AZ RDS +
  HTTP-only listener are deliberate demo-scope choices (HTTPS/ACM, multi-AZ, VPC endpoints
  deferred — see infra README).

### Gated / next
Langfuse population needs critic-flip → critic-delta → flip `POPULATION_GATED=False` → a paid
`--langfuse-sync` eval. IaC deploy needs AWS creds + the Day-10 work (docker build/push to ECR,
DB bootstrap, smoke, destroy). Docker `build`/`--check` couldn't run here (sandbox can't auth to
Docker Hub — environmental, not a Dockerfile defect).


## 2026-06-03 — Planning pass: τ / matcher-v2 / Langfuse settled; eval-maturation renumbered to Day 8

**Where we are.** The autonomous planning pass (Architect → Critic → Reviewer, each handed artifacts
not the prior role's conclusions) produced `docs/plan/next-session-plan.md` +
`docs/decisions/PENDING-DECISIONS.md`. This session inspected the residual, settled the open
decisions, and renumbered the schedule. **Supersedes the "Day 8 = IaC / critic-flip unscheduled" note
in the Day-7 Correction below** — forward-pointer only; that entry is left intact.

### Near-miss band inspection → KEEP τ = 0.85
Classified the 13 residual quotes in the **[0.70, 0.85)** near-miss band by hand: **12/13 are matcher
false-negatives** — PDF line-number / whitespace noise the Day-7 v1 `_normalize` didn't catch
(mid-line line-numbers, digits glued to words, intra-word line-break splits) — and **1/13 is a genuine
borderline** (ellipsis stitch, #9). **Decision: keep τ = 0.85; the lever is matcher-preprocessing v2,
not the threshold.** Lowering τ would launder fixable matcher noise + the one real borderline into a
"pass." **The Day-7 lesson repeating: fix the matcher, don't lower the bar.**

### Decisions settled (PENDING-DECISIONS.md)
- **D1 — τ = 0.85 + matcher-v2.** Lever is the matcher; critic-flip slot unchanged. Item 3b BLOCKED→READY.
- **D2 — Langfuse gets a real slot**, pulled into the Day-8 eval-maturation phase **after critic-delta**
  (captures post-flip scores). Overrides the sub-agents' "keep in future-work" lean. Item 10 BLOCKED→SCHEDULED.
- **D3 — accept the residual.** matcher-v2 recovers ~9 clean cases (**386 → ~395/446**); footnote-splices
  (#4, #7) + ellipsis (#9) + debatable enumerator (#13) stay **accepted residual, documented in the
  Day-13 postmortem**. No analyst-prompt change.
- **D5 — no action** (`--save-baseline` is an *analyst* API call, not Voyage; stays forbidden in planning passes).
- **D4a still OPEN** — atomic-swap-vs-`--truncate` ADR reconciliation (pointer note vs. a tiny new ADR).
  Left open deliberately; the planning commit is **not pushed** so D4a can land in the same branch.

### Plan renumbered — +1 shift (eval-maturation inserted as Day 8)
Chose "insert + shift everything +1." New schedule: **Day 8 = eval-maturation** (matcher-v2 → τ-confirm
→ critic-flip → critic-delta → Langfuse) → **Day 9 = IaC** → **Day 10 = cloud demo** → **Day 11 =
sessions** → **Day 12 = design docs** → **Days 13–16 = postmortems / polish / Loom / buffer.**
Eval-maturation runs **before the cloud demo** so the demo shows the matured matcher + faithfulness-aware
critic. **Item numbers and `dayNN.md` filenames are kept as stable IDs** — only day *numbers* shifted, so
e.g. `day08.md` now describes Day-9 (IaC) work.

### Highest-risk item flagged for next session
matcher-v2's **mid-line line-number rule drops the `\n` anchor** that v1 used to tell a pypdf line-number
from real content. With that anchor gone, the lookbehind guards (`CFR`/`USC`/`art`, dotted/paren
preservation) carry all the false-positive weight against real reg numbers ("21 CFR 209", "30 days",
"Form FDA 3500A"). **The zero-regression gate on the 386 currently-passing quotes is load-bearing** —
validate $0 text-only (`smoke_rechunk --table chunks`), no `--save-baseline`. Full task:
`docs/plan/matcher-preprocessing-v2.md`.

## 2026-06-03 — Day 7: $0 matcher preprocessing fixes (quote faithfulness)

### Count reconciliation: 137 vs 106

- **137** = total quote failures at τ<0.85 (the configured threshold) — the "persistent failures" population.
- **106** = the subset with best-chunk score <0.70 — the "hard fails" within that 137. The other 31 are near-misses (0.70–0.85).

### Step 1 — Smart-quote normalization (tools.py `_normalize`)

Added `_CURLY_MAP` (6 entries: U+2018/19/1C/1D/2032/2033 → ASCII '/"') and applied via `str.translate` before whitespace collapse. Applied to **both** quote and chunk sides. Narrow by design — only quotes/apostrophes, preserving §, en-dash, em-dash, ×, and all other regulatory-document Unicode.

**Unit test added:** `TestCurlyQuoteNormalization` (4 tests) — curly-in-chunk matches straight-in-quote, vice versa, double quotes, and `§` preservation. 48→51 tests pass.

**Smoke results after Step 1:**

| Arm | Before | After Step 1 | Δ |
|---|---|---|---|
| `chunks` (dirty) | 309/446 | 350/446 | +41 |
| `chunks_rechunk` (clean) | 309/446 | 350/446 | +41 |

+41 rescued exactly (matches the c-1 projection). Gap remains 0.

### Step 2 — PDF line-number stripping (tools.py `_normalize`)

Added `_LINENUM_INLINE_RE` and `_LINENUM_LINE_RE` patterns, applied to **both** sides in `_normalize` before whitespace collapse. pypdf embeds sequential 2–4 digit line numbers between sentence fragments in FDA draft guidance PDFs (e.g. `"medical 105 \ndevices"` → `"medical devices"`).

**Regex:** `(?<!CFR)(?<!USC)(?<!art)(?<=[\w,;:\.\)])\s+\d{2,4}\s*\n`

**Edge cases handled (verified against corpus):**
- `"21 CFR 820\n"` → unchanged (`(?<!CFR)` fires) ✓
- `"10 USC 7902\n"` → unchanged (`(?<!USC)` fires) ✓
- `"under Part 820\n"` → unchanged (`(?<!art)` fires — "Part" ends in "art") ✓
- `"§ 820.30"` → unchanged (§ not in `[\w,;:\.\)]` lookbehind) ✓
- `"510(k)"` → unchanged (no \n after; paren not matched) ✓
- `"TLS 1.3"` → unchanged (single digit "3", not 2–4) ✓
- `"medical 105 \ndevices"` → `"medical devices"` ✓

Verified: **0 regression on the 309 (→350) passing quotes** before adding Step 2.

**Smoke results after Step 2:**

| Arm | After Step 1 | After Step 2 | Δ (Step 2) | Total Δ |
|---|---|---|---|---|
| `chunks` (dirty) | 350/446 | 386/446 | +36 | +77 |
| `chunks_rechunk` (clean) | 350/446 | 386/446 | +36 | +77 |

c-3 near-miss resolution: the original near-miss band (0.70–0.85) had 31 cases. After both steps it has 13 residual — 18 near-miss cases crossed τ (combined across both steps).

### Final residual breakdown (60 cases)

| Category | Count | Notes |
|---|---|---|
| Ellipsis (… or ...) | 13 | Analyst synthesized/omitted text between sections |
| Very low (<0.50, no ellipsis) | 8 | Likely synthesized — no verbatim span exists |
| Mid-range (0.50–0.70) | 27 | Mixed: boundary straddle, paraphrased, wrong-chunk |
| Near-miss (0.70–0.85) | 13 | Near-threshold; may resolve with corpus cleaning |

**Real analyst issues (~21):** 8 synthesized + 13 ellipsis = real faithfulness problems the prompt could theoretically fix. Too few clear synthesized cases to justify a prompt change now. **Matcher/corpus near-misses (~39):** 27 mid-range + 13 near-miss — boundary straddle, wrong-chunk citations, or corpus cleaning residual.

**Why delta=0 across corpus arms matters:** The fact that both `chunks` and `chunks_rechunk` went from 309→386 identically — same +77, same residual 60 — proves the faithfulness lever was the matcher, not corpus cleaning or re-chunking. The `chunks_rechunk` clean corpus offered zero additional faithfulness benefit. Re-embed and table-swap therefore remain **DEFERRED**: unneeded for faithfulness. Their remaining value (if any) is retrieval quality — measured separately via the Priority 4 recall comparison, not here.

**No analyst prompt change. No re-embed. No merge, no tag.**

154 tests pass (excluding pre-existing Voyage live-api test).

### Correction — critic-flip / τ-calibration scheduling (2026-06-03)

The note in the 2026-06-02 entry ("quote-faithfulness activation + critic upgrade are gated on τ-calibration (expected Day 8)") was written before Day 8 was locked. **Day 8 = IaC (Terraform, ECS, RDS, Fargate).** Critic-flip (wiring the analyst's emitted quotes into `check_citation` to make `revise` faithfulness-aware) and τ-recalibration are **NOT Day 8 items**. They remain gated on τ-calibration; the clean smoke distribution now exists (386/446, delta=0 across corpus arms). Both items are currently **unscheduled — need a calendar slot**. Do not insert into Day 8.

### Resolution A — structural corpus adopted as live (2026-06-03)

Prior state: the Day-7 rechunk commit (`300f88c`) switched `ingest.py main()` to `chunk_text_structural + clean_text` but the existing `corpus.chunks` remained fixed-size+dirty (2726 rows) — the validated eval numbers didn't transfer and Day-9 cloud ingest would silently produce an unvalidated structural corpus.

**Backup:** `corpus.chunks_fixedsize_backup` created before re-ingest (2726 rows, fixed-size+dirty). Restore path exists.

**Re-ingest:** `uv run python -m rra.ingest --truncate` — 71 docs ingested (73126 failed again, the known scanned PDF), 2745 chunks via `chunk_text_structural + clean_text`.

**Re-measured on live structural corpus:**

| Metric | Old (fixed-size) | New (structural) | Delta |
|---|---|---|---|
| recall@10 | 1.00 (13/13) | **1.00 (13/13)** | 0 |
| faithfulness (τ=0.85) | 386/446 | **386/446** | 0 |
| boilerplate rows | unknown | **0** | — |
| chunk count | 2726 | **2745** | +19 |

Gap=0 (best-chunk == doc-level). All eval numbers transfer to the structural corpus.

**Code == corpus == Day-9 cloud ingest.** A fresh `uv run python -m rra.ingest --limit N` now produces the same structural+clean architecture as the validated local corpus.

---

## 2026-06-02 — Day 7: Priority 1 (key_fact_coverage backstop) + pre-rechunk baseline

### Results — run `day7-prerechunk-baseline` (tag), 2026-06-02T21:24:14Z

Full golden set, all 30 cases, 0 errors, 0 zero-citation answers.
Report: `evals/results/20260602T212414Z-day7-prerechunk-baseline.md`.

**What this run is:** the recovered `key_fact_coverage` baseline on the **current (pre-rechunk) corpus**, captured right after the Priority 1 JSON-prefill fix made the scorer functional for the first time. This is the clean *before* number for the eventual Priority 4 comparison — `key_fact_coverage`'s ruler (Haiku judge vs. `expected_facts`) does **not** depend on chunk boundaries, so it stays comparable across the Priority 3 re-chunk, unlike `citation_validity` and `position_quality` whose substrate shifts when `chunk_index` values are reassigned (ADR 0012 P2).

| Scorer | Mean | Pass rate | Gate | Threshold |
|---|---|---|---|---|
| `citation_validity` | **1.000** | 100.0% | HARD | 0.95 |
| `key_fact_coverage` | **0.908** | 66.7% (20/30) | warn | 0.80 |
| `position_quality` | **0.973** | 100.0% | warn | 4.0 (raw) |

30 scored, 0 errors, 0 zero-citation. `citation_validity` unchanged (Priority 1 didn't touch it). `position_quality` 0.973 vs. Day 6's 0.947 is fresh-generation variance, not a substantive change.

### Priority 1 fix (recap)

`key_fact_coverage` returned N/A on all 30 at Day 6 — Haiku wrapped its JSON reply in prose and strict `json.loads` rejected it (both retries failed → `score=None`). Fixed via **Option 1: assistant-turn prefill of `{`** in `judge.py`, forcing raw JSON at the source. Strict-parse + one-retry + `score=None`-on-double-failure all preserved. `PositionQualityScorer` left untouched (it already parsed cleanly). 5/5 unit tests pass, including one new prefill test.

The backstop is now functional **and discriminating** — 10 of 30 cases land below the 0.80 threshold (hence 66.7% pass). At Day 6 this scorer was blind; the under-citing failure mode it guards (ADR 0012 D1) is now actually covered.

### Calibration reading (so the headline isn't misread)

- **Well-calibrated, not lenient.** 66.7% pass = a third of cases fail on partial fact coverage; a rubber-stamp judge would read 100%.
- **The 0.908 mean is inflated by the refusal band.** 4 of the 5 hard cases score 1.000 (hard-003 is the exception at 0.750 — see below). That is *correct*, not lenient: the hard cases are refusal cases whose `expected_facts` **are** the refusal facts ("no quantitative threshold exists" / "no genAI guidance in corpus" / "RWE supports, not replaces"). A perfect score there means the analyst correctly refused — direct evidence the refuse-to-hallucinate design works. The refusal band therefore sits at/near ceiling and can't improve, so **track easy+medium separately from hard** or real movement gets masked by the headline.
- **Genuine coverage gaps the Day-6 bug was hiding** (real analyst weaknesses, not judge artifacts):
  - **easy-005** (software docs in a premarket submission) — persistently weak: 0.500 here (0.000 on the earlier easy-subset run).
  - **0.750 band on multi-part synthesis questions** — easy-002/003/006/007, medium-004/009/011/014, hard-003 — one of four `expected_facts` missed in each.
  - **hard-003's 0.750 is expected:** the deliberate partial-coverage case, which needs *both* the real obligation *and* the gap flag.

### Day-7 status

- **Priority 1: done** — `key_fact_coverage` backstop functional; this run is the proof.
- **Priority 2 (quote-faithfulness):** plan approved, implementation pending.
- **Critic** remains in key-existence mode; quote-faithfulness activation + critic upgrade are gated on τ-calibration (expected Day 8).
- **Nothing re-chunked yet** — this baseline stays valid as the pre-rechunk reference for the Priority 4 comparison.

---

## 2026-06-02 — Day 6: Eval harness baseline

### Results — run `day06-baseline` (tag), 2026-06-02T17:50:07Z

Full golden set, all 30 cases, 0 errors, 0 zero-citation answers.
Report: `evals/results/latest.md` → symlink to `20260602T175007Z-day06-baseline.md`.

**Baseline label:** key-existence only (ADR 0010 Day 6). See P1/P2 below before reading these numbers as "good."

| Scorer | Mean | Pass rate | Gate | Threshold |
|---|---|---|---|---|
| `citation_validity` | **1.000** | 100.0% | HARD | 0.95 |
| `key_fact_coverage` | **N/A** | N/A | warn | 0.80 |
| `position_quality` | **0.947** | 93.3% (28/30) | warn | 4.0 (raw) |

#### Per-difficulty breakdown — `position_quality` (normalized 0–1; threshold 0.800 = 4/5 raw)

| Band | n | Mean | Passes |
|---|---|---|---|
| Easy | 10 | 0.940 | 9/10 |
| Medium | 15 | 0.960 | 15/15 |
| Hard | 5 | 0.920 | 4/5 |

`citation_validity` and `key_fact_coverage` are uniform across bands (1.000 and N/A respectively for all 30).

### Weakness 1 (critical): `key_fact_coverage` produced zero signal

All 30 cases returned `score=None` (N/A). The scorer is correctly wired; the gap is judge output format: Haiku wraps its JSON reply in prose ("Here is the JSON: ..."), strict `json.loads` rejects it, and both retry attempts fail → `score=None` for every case.

This matters beyond cosmetics. `key_fact_coverage` is the designated D1 backstop for the under-citing failure mode (ADR 0012 D1): if the analyst stops citing, `citation_validity` rises toward 1.0 (fewer citations to check) and the harness goes blind without this scorer to catch the content regression. Today that failure mode is absent (0/30 zero-citation answers) so the non-functional backstop is harmless — but it must be fixed before Day 7 introduces corpus changes that could shift citation behavior.

**Day-7 fix:** prefill the assistant turn with `{` (Anthropic prefill parameter) so the model is forced to open with raw JSON, or add a system-prompt instruction requiring pure JSON output with no surrounding prose.

### Weakness 2 (structural): `citation_validity = 1.000` is a coarse-ruler result, not a success

Key-existence only verifies that `(guidance_id, chunk_index)` resolves to a real `corpus.chunks` row. An analyst that cites real chunks but quotes them unfaithfully — or cites tangentially relevant chunks to pad citation count — scores 1.000 under this ruler. This is the ADR-0012-P1 finding stated explicitly: a passing Day 6 gate means the system doesn't hallucinate chunk indices, not that citations are faithful.

**Day-7 target:** activate quote-faithfulness matching in `check_citation` (ADR 0010), which requires resolving the resolution-vs-verification ordering question (does `_resolve_citations()` at the API layer run before or after the critic's pre-validation pass?). Also tied to the Day-7 re-chunk/boilerplate-cleaning work — corpus changes will reassign `chunk_index` values, so the key-existence baseline is not forward-comparable regardless (see P2 below).

### Example failures — `position_quality`

Two cases scored 3/5 (normalized 0.600), both well below the 4/5 threshold:

- **easy-003** — "What types of device modifications does FDA consider generally appropriate for inclusion in a PCCP for a non-AI hardware device?" Surprising for an easy case. Worth a Day-7 look at whether the analyst answer is thin/wrong or the judge is miscalibrated on PCCP scope. The general PCCP framework (180978) may produce less crisp answers than AI-specific PCCP (166704).
- **hard-003** — "What specific interoperability design requirements and consensus standards must InfusePro satisfy to connect to hospital EMR systems?" Score of 3/5 is expected: this is a deliberate gap case — the corpus contains only external references to an interoperability guidance that is not ingested (119933 #23 points outward; 153781 #20 asks the submitter to state what standards they use). The correct answer is partial + "dedicated guidance not in corpus." A 3/5 here may mean the analyst didn't flag the gap strongly enough.

Three medium cases (medium-005, medium-008, medium-010) scored 0.800 (4/5 raw) — exactly at threshold, passes.

### Day-7 target (required re-run)

Two items to fix, then a full fresh harness run on the same 30-case golden set:

1. Fix `key_fact_coverage` JSON parsing (prefill or system-prompt force)
2. Activate quote-faithfulness in `check_citation` (ADR 0010)

The Day-7 re-run must be a full fresh run on the same golden set. Day 6 → Day 7 comparison is triple-confounded: ruler changes (key-existence → quote-faithfulness), substrate changes (re-chunk → new chunk_index values), corpus changes (boilerplate cleaning). Do not compare Day 6 and Day 7 numbers directly (ADR 0012 P2).

**Cosmetic note (no correctness impact):** the `position_quality` aggregate table row shows normalized mean (0–1) against the raw threshold (4.0) — visually confusing but warn-only; gate logic uses the pre-computed `passed` flag, not the displayed mean. Fix in Day 7.

### Stop conditions met (Day 6 DoD)

Harness runs all 30 without crashing ✓. `latest.md` shows real numbers ✓. CI workflow committed (`evals.yml`, gate-only, `--fixture ci --no-llm-judges`) ✓. Per-difficulty breakdown shows non-uniform results (easy/medium/hard variance is real though narrow) ✓. ADR 0012 accepted ✓. Imports clean, golden set loads cleanly ✓.

---

## 2026-06-02 — Day 6: Golden-set design notes

Recorded here so the 30-question set is legible vs. accident when revisited after Day 7 corpus changes.

### Shape and scope

30 questions: 10 easy / 15 medium / 5 hard. Products: CardioWatch (AI/SaMD, 11 questions), InfusePro (connected infusion pump, 9), NeuroPath (digital therapeutic / De Novo, 9), plus 1 cross-cutting (multi-function device). 25 distinct guidances grounded across the set.

**73126 excluded:** confirmed 0 chunks in corpus (failed ingest). Not anchored anywhere.

**Thin tail avoided as anchors:** guidances with ≤7 chunks (89238=4, 72685=6, 72646=7, 72446=7) are excluded as question anchors. All primary anchors have ≥18 chunks; minimum among anchors is 72674 at 18 chunks (De Novo process mechanics, NeuroPath).

**141565 (PRO instrument development, 19 chunks) held optional:** thin doc, not anchored in any of the 30 questions. 77832 (47 chunks) is the PRO anchor. 141565 is in the corpus but plays no role in the current golden set.

### Cold-set rule

Questions were picked before running any query against the answer pipeline. All grounding came from reading `corpus.chunks` text directly via SQL. `run_graph` / the analyst was never invoked during question design. This is the discipline that makes the golden set an honest eval rather than a capability demonstration.

### medium-005 / hard-001 intentional pairing

Both questions use the same scenario: CardioWatch, AI model shows lower sensitivity in elderly patients.

- **medium-005** asks how to *frame the benefit-risk analysis* for that gap. This is answerable from the corpus (184856 + 99769 benefit-risk framework).
- **hard-001** asks what *quantitative subgroup performance-parity threshold* FDA requires. This is a refusal case: 184856 treats bias qualitatively throughout (recommends evaluating subgroup performance, names race/ethnicity/sex/age as relevant groups, calls for "control of bias" through TPLC) but specifies no numeric threshold anywhere. The correct answer is partial — the guidance expects subgroup evaluation but does not set a quantitative bar.

The pairing was deliberate: it tests whether the analyst distinguishes "I can answer this from the corpus" from "the corpus addresses this qualitatively but never sets the specific number you're asking for."

### Hard-five gap evidence

All five hard cases were verified against actual corpus chunks before being finalized, to confirm the gap is genuine and not "answer hiding in chunk N":

- **hard-001** (subgroup threshold): 184856 #4, #9, #24, #30, #31, #36 read — no numeric criterion anywhere.
- **hard-002** (generative-AI / LLM validation): probed entire corpus for `generative`, `large language model`, `LLM`, `foundation model` → zero matches. Corpus is silent on generative AI.
- **hard-003** (interoperability standards): 119933 #23 and 153781 #20 reference an external interoperability guidance not ingested; no corpus chunk states design requirements or consensus standards.
- **hard-004** (De Novo clinical evidence bar): 152657 is an acceptance checklist; 72674 is procedural. Neither sets a substantive evidence threshold for a digital therapeutic.
- **hard-005** (RWE as trial substitute): 190201 probed for `in lieu of`, `instead of`, `replace` → no matches. RWE doc frames evidence as supporting/informing decisions, not replacing premarket clinical investigation.

### Anti-trap check logged

PCCP-for-intended-use-change was considered as a hard candidate and **rejected**: 166704 #11 and #18 explicitly say major intended-use changes fall outside a PCCP and require a new submission, making it *answerable*, not a refusal case. Logged here so it isn't reintroduced as a "hard" question in a future refresh.

### Product balance (final)

CardioWatch 11 (4E / 5M / 2H), InfusePro 9 (3E / 5M / 1H), NeuroPath 9 (3E / 4M / 2H), cross-cutting 1 (1M). Consistent with the ADR-0007 corpus-scope rationale: CardioWatch anchors the AI/SaMD narrative and gets the heaviest coverage.

---

## 2026-06-02 — Day 6 pre-work: eval-harness scoring and CI policy (ADR 0012)

### What was decided (no code written)

Four scoring and CI decisions locked in ADR 0012 before implementation, so they are fixed and reviewable. All four govern concrete touchpoints in `src/rra/evals/`.

**D1 — Zero-citation answers are N/A for `citation_validity`, excluded from the mean.**
The current stub (`scorers.py:70`) returns 0.0 for zero-citation answers; that punishes correct hard-refusal answers. Exclusion is safe only because (a) the runner emits a prominent "N of 30 had zero citations" count, and (b) `key_fact_coverage` catches the non-citing failure mode. Without both backstops, a degraded analyst that stops citing would show a falsely *rising* `citation_validity` mean.

**D2 — CI runs `citation_validity` only, against a lightweight key fixture.**
No embeddings, no judge API calls in CI. Key-existence is deterministic and fast; judge scorers are non-deterministic, token-expensive, and need `ANTHROPIC_API_KEY` — wrong for per-PR gating. Full eval (both judges + full corpus) runs manually or nightly.

**D3 — `PositionQualityScorer` reads `POSITION_JUDGE_MODEL`, not `ANALYST_MODEL`.**
`run.py:191` currently stubs `model=os.environ["ANALYST_MODEL"]`. That must be corrected: the judge must be pinned independently of the system under test. Known caveat: Sonnet-judges-Sonnet has mild self-preference bias, mitigated (not eliminated) by passages-in-context design.

**D4 — The "watch CI fail" demo uses a planted bogus case in a CI-only fixture, never in `golden.jsonl`.**
The key-existence baseline is expected to be high (≥ 0.95) because well-behaved analysts rarely hallucinate chunk indices — the gate may pass naturally. A planted known-invalid `chunk_index` proves the gate bites without corrupting the ground-truth golden set.

### Two predictions to read Day 6 numbers correctly

**P1 — A passing Day 6 gate is not good news.** Key-existence only catches hallucinated chunk indices. An analyst that cites real chunks but quotes them unfaithfully will score near 1.0. The entire point of Day 7 is that a passing Day 6 gate reveals the ruler's limit, not that citations are fine.

**P2 — Day 6 → Day 7 comparison is triple-confounded.** Day 7 changes the ruler (activates quote-faithfulness), the substrate (re-chunk → new `chunk_index` values), and the corpus (boilerplate cleaning). These are not isolated. The only honest comparison re-runs the full harness on Day 7. Do not compare Day 6 and Day 7 numbers directly.

See ADR 0012 for full rationale and the related ADRs (0010 matching contract, 0006 span addressing, 0009 critic-loop policy).

---

## 2026-06-01 — Day 5: MCP server + check_citation

### What was built

Custom MCP server exposing four tools, wired into the agent pipeline (ADRs 0010, 0011). Tools live in `src/rra/mcp_server/tools.py` as plain importable Python; `server.py` is a thin MCP wrapper registering the same functions for external clients. Agents call the functions in-process — no subprocess-per-query.

Files: `src/rra/mcp_server/tools.py` (four tools + Pydantic models + ToolError), `src/rra/mcp_server/server.py` (FastMCP wrapper), `src/rra/config.py` (citation_match_threshold = 0.85). Wiring: `researcher.py` imports search_corpus from the tool layer; `critic.py` pre-validates every `[guid:idx]` citation via check_citation before the LLM call, injecting `<citation_checks>` XML.

The four tools: search_corpus (semantic retrieval, returns passages with guidance_id:chunk_index addresses), fetch_guidance (raw document reassembly from ordered chunks), check_citation (the distinctive one — verifies a citation address resolves and, when given quoted_text, that the quote faithfully appears via normalized matching), list_recent_guidances (ingest-date proxy for currency).

### Claude Desktop milestone — all four tools verified live

Connected the server to Claude Desktop (WSL2 → Windows via wsl.exe launch). Exercised all four tools by plain-language request; Claude Desktop selected the right tool unprompted each time (tool descriptions pass).

- search_corpus: retrieved the single on-point doc (guidance_id 99785, "Deciding When to Submit a 510(k) for a Software Change to an Existing Device") and synthesized a correctly-cited answer.
- fetch_guidance: returned the raw reassembled document with PDF artifacts present (raw is deliberate — see below).
- check_citation valid (chunk_index 4): verified=true, source_text returned, null span/score — key-existence mode, since no quoted_text was passed.
- check_citation invalid (chunk_index 99999): verified=false, empty source_text, no crash — fails closed (ADR-0010 NOT_FOUND-as-clean-result, verified live).
- list_recent_guidances: returned the guidance list with ingest dates.

server.py (0% unit coverage — the MCP protocol layer pytest can't reach) is verified by this manual Claude Desktop test, not unit tests.

### Cost / token impact (DoD)

Critic citation pre-validation adds ~2,453 input tokens (~27%, 8,971 → ~11,400) and ~$0.007 to the critic per query, from injecting source_text per citation into the critic's context. Truncating source_text to a window is a lever if cost matters; left full for Day 5.

Two cost profiles captured (for the Day 10 model):
- Clean approve, planner cached: ~18,568 total tokens, ~$0.07/query.
- Revise-once, planner cold: ~47,287 total tokens (~2.5× clean) — reruns the analyst + critic. The revise rate across the Day 6 eval set will be a key cost driver.

Cache caveat: the planner's 21-token input only holds on a prompt-cache hit (~5-min TTL). A cold planner is ~443 tokens. The Day 10 cost model must state the cache-hit-rate assumption.

### Matching engine: built but dormant in Day 5

The normalized-matching algorithm (whitespace-normalize → substring + whitespace-flexible regex for document-level span via char_start → SequenceMatcher coverage-ratio fallback ≥ τ) is built and unit-tested but NOT exercised by the live pipeline in Day 5. Reason (per ADR 0010): quoted_text is resolved post-graph by _resolve_citations() at the API layer, so it's unavailable at the critic's point in the graph. Every Day 5 check_citation call runs in key-existence mode (quoted_text=None). The Claude Desktop demo confirmed this directly — valid check returned null span/score because no quote was supplied. Activation is a Day 7 question (resolution-before-critic vs. post-resolution verification pass).

Day 6 interpretation: the first citation_validity run measures key-existence, not quote-faithfulness. Label the baseline accordingly so post-Day-7 comparison isn't a false improvement.

### First live query — clean pre-validation

On the first post-wiring query the critic loop ran normally: one revision (note_count=2, content-driven), resolved on rev1 (note_count=0, approve), no cap_hit. Citations all resolved in key-existence mode; no spurious revises from tool errors. Confirms the citation pre-validation is non-disruptive — the ADR-0010 retryable-error invariant (transient failure → inconclusive, never a forced revise) held.

### Findings worth keeping

- The client masks corpus dirt. Claude Desktop, on its own, cleaned fetch_guidance's raw output into a readable document for presentation. The dirty-corpus problem is therefore invisible at the chat layer — a human eyeballing output would never see it. This is the argument for an automated grounding eval (Day 6): it measures the raw grounding the user's screen hides.
- The demo independently surfaced the chunk-boundary issue. Claude Desktop noted chunk 4 begins mid-sentence (the boundary split a sentence starting in chunk 3) — the same root cause as the citation-precision problem. Confirms the Day 7 ingest re-chunk targets the right thing.
- Be wary of client editorializing on retrieved content. Claude Desktop volunteered a QMSR "now in effect" gloss on top of the retrieved text. The tool grounds; the model interprets — keep the distinction clear when framing the demo.

### Known issues

- psycopg_pool DeprecationWarning (`open` parameter default changing) is pre-existing, not introduced in Day 5. Address when pinning psycopg_pool.
- critic unit tests now reach the connection pool (check_citation queries Postgres before the LLM call), so they have a DB dependency they lacked in Day 4. They pass with Postgres up; consider mocking check_citation in the pure-unit critic tests for offline isolation.

### Stop conditions met

All four tools callable from Claude Desktop ✓. Agents use the MCP tool layer, not direct retrieval calls ✓. Langfuse trace shows check_citation child spans under the critic ✓. 113 tests pass (gate off), mypy clean ✓. ADRs 0010 (matching contract) and 0011 (in-process transport) accepted.

---

## 2026-06-01 — Day 4: LangGraph multi-agent orchestrator

### What was built

Four-node LangGraph state machine replacing the Day 3 single-shot Anthropic call:

```
START → planner → researcher → analyst → critic
                                   ↑           │
                                   │  route_after_critic()
                               revise+count<cap │
                                   └───────────┘ approve/escalate/cap_hit → END
```

**Files created:**
- `src/rra/agents/types.py` — CriticNote, CriticOutput internal types
- `src/rra/agents/planner.py` — Sonnet; tool-based decomposition (PlannerOutput)
- `src/rra/agents/researcher.py` — Haiku; query reformulation + direct search_corpus call; chunk dedup
- `src/rra/agents/analyst.py` — Sonnet; synthesis + edit-in-place revision; _format_user_prompt moved here
- `src/rra/agents/critic.py` — Sonnet; context-match citation check; sets revision_count and cap_hit
- `src/rra/graph.py` — GraphState TypedDict (13 fields), PostgresSaver checkpointer, run_graph()

**Files updated:**
- `src/rra/api.py` — replaced Anthropic call block with run_graph(); kept _resolve_citations unchanged
- `src/rra/schemas.py` — added QueryResponse.warning: str | None (ADR 0008 additive extension)
- `src/rra/config.py` — planner/analyst/critic model defaults updated to claude-sonnet-4-6
- `tests/test_api.py` — updated mocking layer to patch rra.api.run_graph; all assertions unchanged

**Tests added:** test_graph.py (4 routing scenarios), test_agents.py (per-agent contract tests).

### Decisions made

**test_api.py mocking update:** The task asked for test_api.py to "pass unchanged" but the old patches (`rra.api.search_corpus`, `rra.api.Anthropic`) target imports that no longer exist in api.py after Day 4. Updated the mocking layer to patch `rra.api.run_graph` instead. All HTTP contract assertions (status codes, response schema, citation resolution, auth) are unchanged. The "unchanged" constraint means contract preservation, not frozen test internals.

**Prompt caching placement:**
- Planner: system prompt includes 3 few-shot examples (~680 tokens) to push past the 1024-token cache threshold. `cache_control=ephemeral` applied.
- Analyst: system prompt ~500 tokens with formatting rules; estimated ~500 tokens. Applied cache_control; may not cache on every call if under threshold in some environments. The system prompt is stable across all calls (only the user message changes per query).
- Critic: system prompt ~450 tokens with audit instructions. Applied cache_control; same reasoning as analyst.

**Token_usage reducer:** Used `Annotated[dict[str, int], _merge_token_usage]` in GraphState TypedDict so each agent's token keys are merged without node functions needing to read prior state. Keys are unique per agent (e.g., `planner_input`, `analyst_input_rev1`).

**PostgresSaver initialization:** `lru_cache(maxsize=1)` singleton backed by `get_pool()` from rra.db (ADR 0004). `setup()` is idempotent (creates tables if missing, runs pending migrations).

**Graph cache reset in tests:** `_graph` is a module-level singleton. Tests use an `autouse` fixture to reset it to `None` and use `MemorySaver` (LangGraph in-memory checkpointer) via `patch("rra.graph._get_checkpointer", return_value=MemorySaver())`. This avoids DB dependency in unit tests.

**Cap-hit written by critic node:** The design doc mentioned a "thin wrapping node" for cap_hit. Implemented more cleanly: the critic node itself computes `cap_hit = (new_revision_count >= settings.max_critic_revisions)` after incrementing, then `route_after_critic` reads `state["cap_hit"]` directly. One less node in the graph; same semantics.

### Surprises / open items

- The planner system prompt may not reliably exceed 1024 tokens in all configurations since token count varies by exact prompt text. If cache hit rate is low on the planner, consider adding more few-shot examples in Day 7 (when retrieval recall evals run).
- LangGraph 0.2.50 passes state as `dict[str, Any]` to node functions at runtime even when `StateGraph[GraphState]` is used, requiring `# type: ignore[type-var]` on `add_node` calls. This is a known limitation of LangGraph's TypedDict typing.
- The `_format_user_prompt` move from api.py to analyst.py is a breaking change for any caller that imported it from api.py directly. Exported as `format_user_prompt` from `rra.agents.analyst` with the same signature.

Per-agent token cost (real query — unforced approve path)
Measured from Langfuse trace 7a0cc767... (query: "When does a software
change to a cleared device require a new 510(k)?", Class II SaMD context).
Approved first pass, 7 citations, warning=null.
AgentModelInputOutputCostplannerSonnet 4.621225$0.00344researcher (4×)Haiku 4.51,01599$0.00151analystSonnet 4.67,1701,017$0.03677criticSonnet 4.68,97150$0.02766total—17,1771,391$0.069
Trace latency: 34.2s end-to-end.
Cost shape: analyst + critic are ~93% of spend; the Haiku researcher
(4 reformulation calls) is ~2%. The planner's input is only 21 tokens —
prompt caching is working: the ~680-token few-shot system prompt is cached,
so only the per-query delta is billed as fresh input. The critic is
expensive because it ingests the full draft + all retrieved passages to
verify citations (8,971 input tokens) but emits almost nothing (50 output).
Note: forced-verdict (CRITIC_FORCE_VERDICT) runs show the critic at 0
tokens (no LLM call) — never use forced-run traces for cost modeling.
This unforced trace is the canonical cost datum for the Day 10 model.
Langfuse trace structure (confirmed)
query (root span, 34.2s)
├── planner    (span → anthropic:planner generation, Sonnet)
├── researcher (span → 4× anthropic:researcher generations [Haiku] +
│                4× search_corpus RETRIEVER observations)
├── analyst    (span → anthropic:analyst generation, Sonnet)
└── critic     (span → anthropic:critic generation, Sonnet)
Each agent node is a SPAN; each LLM call is a nested GENERATION with token
usage; retrieval calls are RETRIEVER observations. The researcher's 4
reformulation+retrieval pairs are visible as distinct children — confirming
the Haiku query-reformulation design (not a Python passthrough).
Forced-verdict runs produce a bare critic SPAN with no GENERATION child
(no model call) — the loop is still visible, the cost is correctly zero.
Loop verified live (3 modes)

revise   → analyst runs 3× (initial + 2 revisions) → cap_hit at
revision_count=2 → warning "Analysis reached the maximum revision limit."
escalate → single analyst pass → immediate exit → warning "Query could
not be fully grounded in available guidance. Answer is best-effort."
unset      → normal first-pass approve → warning=null (gate is
production-invisible; enforced by test_force_verdict_default_is_none).

Revision passes cost more than the initial (the analyst receives prior
draft + critic notes on top of passages), confirming the edit-in-place
design from ADR 0009.
---

## 2026-06-01 — Expanded title-shape regex patterns; pathway-classification 68 → 44

Extended `DEVICE_SPECIFIC_TITLE_PATTERNS` from 11 to 21 patterns and added 12 entries to `DEVICE_SPECIFIC_HINTS`.

**Root cause of prior plateau at 68:** FDA uses `(510(k))` with parentheses as often as `[510(k)]` with brackets; the previous patterns only handled the bracket form. Other gaps: "Guidance Document for the Preparation of Premarket Notification for X" has no 510(k) token, mid-title forms need an unanchored pattern, and some device-specific titles (Biological Indicator, Intravascular Administration Sets) need keyword matching.

**New patterns:** parentheses variant of the Premarket Notification anchored/unanchored forms; "Guidance Document for the Preparation of Premarket Notification"; "Guidance on 510(k) Submissions for X"; "Guidance on ... of a Premarket Notification for X"; "Submission of Premarket Notifications for X"; "Recommendations for Premarket Notifications for X"; "X - Submission Guidance for a 510(k)"; unanchored "Premarket Notification [510(k)] Submissions for X"; "Content and Format for Abbreviated 510(k)s for X".

**New hints:** biological indicator, intravascular administration, spinal system, chorionic gonadotropin, phacofragmentation, retinal prosth, pulse oximeter, artificial pancreas, " gown", hypothermic, total artificial disc, medical laser.

**Also added `src/rra/py.typed`** (missing PEP 561 marker; caused mypy `import-untyped` error on `rra.rate_limit` under `--strict`).

**Before/after (--include-drafts --no-verify, live FDA index, 2026-06-01):**
- Before: 135 total candidates, 68 pathway-classification, 401 dropped
- After: 111 total candidates, 44 pathway-classification, 425 dropped

All required spot-checks passed: foundational docs (The 510(k) Program, Abbreviated 510(k), De Novo, Refuse-to-Accept, Determination of Intended Use, Real-Time PMA Supplements) survive; device-specific entries (Pulse Oximeters, Powered Suction Pump, Surgical Gowns, Aqueous Shunts) are absent from pathway-classification.

**Note:** The achievable floor is ~44, not the ~15-20 projected in the task spec. The remaining 44 entries (Benefit-Risk factors, FDA Actions on 510(k)/PMA/De Novo, User Fees, IDE guidance, Q-Submission, Safer Technologies, Breakthrough Devices, etc.) are genuinely cross-cutting pathway docs that structural patterns cannot filter without false positives.

## 2026-05-31 - "Day 3" draft:

## Day 3 — Phase 1 design surfaces

**Phase 1** review caught a correctness issue we'd otherwise have shipped:
ingest uses Voyage `input_type="document"`, so the query path must use
`input_type="query"`. Voyage 3 is asymmetric — symmetric embeddings on
both sides degrade retrieval quality measurably. Folded into ADR 0005
(query-time embeddings) so the rationale is locked.


**Phase 2**  smoke test results (5 queries)

Strong signals captured:

**Refusal works.** Two trap queries against topics the corpus doesn't cover
(SaMD definition, cybersecurity controls) produced informative refusals
that named the missing documents. This is the regulated-vertical refusal
behavior the spec §6.1 hard band tests for — already passing manually
before Day 6 evals. Examples:
- SaMD query: "...you would need to consult other FDA guidance documents
  specifically dedicated to that topic, such as FDA's guidance on
  'Software as a Medical Device (SAMD): Clinical Evaluation,' which is
  not among the passages provided."
- Cybersecurity query: distinguished retrieved "software documentation"
  passages from cybersecurity specifically.

**Synthesis works (mostly).** Multi-part query about 510(k) modification
decisions + Special vs Traditional pulled from 5 distinct guidances and
constructed a structured answer. Citations are approximately correct but
unverified — Day 5's check_citation tool will validate.

**Off-topic refusal works.** Python framework question returned reranker
scores 0.23-0.28 (vs 0.8+ for on-topic) and was refused. Possible future
optimization: skip LLM call when max score < 0.5.

**The diversity issue from the first query (multiple chunks from same
guidance) does NOT appear on the synthesis-type queries.** The reranker
surfaced diverse sources when the query naturally spanned topics. This
suggests future-work §12 (MMR/per-source cap) may be redundant once the
multi-agent on Day 4 generates multiple sub-queries — the planner
naturally creates topic diversity.

Token costs per query: ~3000-3500 input, ~300-400 output. ~$0.015-0.018
per query at Sonnet pricing. Latency 6-8s.

Day 3 confidence: high. Single-shot retrieval+answer endpoint produces
production-credible output on real questions with real refusal behavior.

## 2026-05-31 — Day 2 postmortem: schema drift + systemic ingest hardening

### Root cause

`init-db/01-init.sql` and `_ensure_schema()` in `ingest.py` defined two
completely different tables. In the normal developer workflow (`docker compose up`
→ `rra-ingest`), init-db runs first. `_ensure_schema`'s `CREATE TABLE IF NOT
EXISTS` is then a no-op, so the table keeps init-db's schema. Every INSERT
immediately fails.

Three crash-level drifts, triggered in sequence as each was individually fixed:
1. `token_count` present in code's INSERT, missing from init-db table
2. `UNIQUE (guidance_id, chunk_index)` required by `ON CONFLICT`, missing from
   init-db table
3. `guidance_title TEXT NOT NULL` present in init-db, never written by code

### What was fixed (full enumeration)

**Schema:**
- `init-db/01-init.sql`: added `token_count INT NOT NULL`, `UNIQUE (guidance_id,
  chunk_index)`, changed `embedding` to `NOT NULL`. Added sync comment: both
  files must be updated together.
- `_ensure_schema()`: rewritten to match init-db exactly (full column list,
  same index names, same constraints).
- Running DB (no rows): applied three ALTER TABLE statements directly.
- `_ensure_schema` now called once in `main()` before any download/embed work,
  so schema problems surface before API costs are incurred.

**Ingest hardening:**
- `Chunk` gained `guidance_title` sourced from manifest `"title"` field.
  `_urls_from_manifest()` replaced by `_entries_from_manifest()` returning full
  entry dicts; `guidance_id` now comes from manifest `"id"`, not URL parsing.
- `DownloadedDoc(path, guidance_id, guidance_title)` dataclass threads identity
  through the download → ingest pipeline.
- `_download_one` accepts explicit `guidance_id` param (eliminated URL stem
  heuristic that would silently produce wrong IDs for non-standard URLs).
- `main()` per-doc loop: `parse_pdf → chunk_text → embed_chunks → write_to_postgres`
  wrapped in `try/except`; one bad doc logs an error and continues.
- `--truncate` flag added: TRUNCATEs `corpus.chunks` before ingesting. Use after
  schema changes for a clean-slate re-ingest.
- `_embed_batch` retry predicate narrowed from `Exception` (retried everything,
  including permanent auth and bad-input errors) to a whitelist of retryable
  Voyage error types: `RateLimitError`, `ServerError`, `ServiceUnavailableError`,
  `APIConnectionError`, `TryAgain`, `Timeout`.
- `NotImplementedError` for embedding count mismatch replaced with `RuntimeError`.

**Tests:**
- `tests/test_ingest_integration.py` added (two tests, `@pytest.mark.integration`):
  - `test_write_populates_all_columns`: asserts every column lands in the live DB
  - `test_write_is_idempotent`: two identical writes → exactly N rows
  These two tests would have caught every schema-drift crash before it reached
  a live ingest run.
- `@pytest.mark.integration` registered in `pyproject.toml`.
- Unit tests updated for new `Chunk.guidance_title` field and `DownloadedDoc`
  return type.

### Systemic lesson

The root failure was two files defining the same table independently with no
enforcement that they stayed in sync. The fixes:
1. Added a warning comment in init-db pointing at `_ensure_schema`.
2. Added integration tests that actually write to Postgres — unit tests mocking
   `_ensure_schema` cannot catch schema drift by construction.
3. `_ensure_schema` is now called at the start of `main()` before any
   download/embed work, so schema failures are cheap to discover.

### Open questions (carry forward to Day 6)

1. **Stale high-index rows on re-chunk.** If a document is re-chunked and
   produces fewer chunks than before, old high-index rows linger. The `--truncate`
   flag handles full re-ingests; per-doc cleanup would need a
   `DELETE FROM corpus.chunks WHERE guidance_id = %s` before each upsert batch.
   Accept or fix? Depends on whether re-chunking is needed before evals.

2. **`_embed_batch` creates a new `voyageai.Client` per call.** Fine for the
   batch job, but the query path (day 3+) should share a singleton client.

3. **Partial download file corruption.** `dest.write_bytes()` is atomic if
   the process runs to completion; a mid-write kill can leave a truncated file
   that the cache check (`if dest.exists()`) will accept as valid. Atomic
   write via tmp-file + `os.replace` is the fix. Low priority until a
   corruption event is actually observed.

## 2026-05-31 — Day 2 follow-up: ingest hardening

### Decisions

1. **Replaced `_CORPUS_URLS` with `_urls_from_manifest()`.** The hardcoded list
   was the root cause of 404 failures against real FDA URLs — no one had verified
   them. `_urls_from_manifest()` reads `data/corpus/manifest.json` and skips any
   entry where `verification.ok is False`. The manifest is now the single source
   of truth for which documents to ingest.

2. **Narrowed the tenacity retry predicate on `_download_one`.** The old
   `retry_if_exception_type((httpx.HTTPStatusError, httpx.TransportError))`
   retried 4xx responses, burning all 4 attempts on a permanent 404.  Switched to
   `retry_if_exception(_is_retryable)` which retries only on 5xx status codes
   and transient network exceptions (`TimeoutException`, `ConnectError`,
   `ReadError`).

3. **`download_guidances` now fault-tolerant.** One bad document no longer
   crashes the whole batch: each `_download_one` call is wrapped in try/except,
   failures are logged at error level and appended to a failure list, and only
   successful paths are returned. A summary log line (succeeded/failed counts)
   fires after every run.

### Open questions

1. **Manifest `verification.ok` is `"skipped"` for all current entries.** The
   scraper writes `{"ok": true, "reason": "skipped"}` without ever doing a live
   HEAD check. Real verification (HTTP HEAD → confirm 200 + Content-Type PDF)
   would let the filter actually do work. Worth a scraper pass before next ingest
   run.

2. **`--limit` now defaults to `None` (all entries).** The manifest has 20 entries
   in the current snapshot. If the manifest grows large, callers should pass
   `--limit` explicitly to avoid long ingest runs.

### Issues

1. **Voyage rate limit**

After fixing download resilience, hit the next failure mode: Voyage's
free-tier rate limit (3 RPM / 10K TPM). One rate-limit error crashed
the entire ingest, including the 5 successful downloads.

**Resolution:** added payment method to Voyage account (no charge — still
in the 200M free-token grant), unlocks 300 RPM / 1M TPM.

**Lesson:** same fault-tolerance gap as the 404 issue — embedding failure
should not lose download work. Deferred a retry-on-RateLimitError fix
in _embed_batch; the rate limit lift removes the immediate need but
the fault-tolerance issue stands.

**Decision:** keep the deferral on the radar. If Day 6 evals show recall
problems and we need to re-embed the corpus with different settings,
the retry logic becomes worth shipping.

## 2026-05-30 — Day 2: ingest pipeline

### What was built

**Phase 1 (design, Opus):** `docs/ingest-design.md` (266 lines) — full proposal
with function signatures, data carrier types, Postgres schema DDL, six key design
decisions, spec cross-references, and five open questions for the human.

**Phase 2 (implementation, Sonnet):** `src/rra/ingest.py` (348 lines) and
`tests/test_ingest.py` (334 lines).

Key functions in `ingest.py`:
- `download_guidances(limit)` + `_download_one` — httpx download with tenacity retry
- `parse_pdf(path)` — pypdf extraction + scanned-PDF detection (< 500 chars)
- `chunk_text(text, guidance_id)` → `list[Chunk]` — tiktoken sliding window
- `embed_chunks(chunks)` → `list[EmbeddedChunk]` — Voyage batches ≤ 128
- `_embed_batch(texts)` — tenacity-wrapped Voyage call
- `write_to_postgres(chunks)` + `_ensure_schema` — psycopg3 upsert, one txn/doc
- `main()` — argparse entry point; returns int exit code for CI use

18/18 tests pass; `uv run mypy src/rra/ingest.py` clean under strict mode.

### What was deferred and why

- **Natural boundary chunking** (RecursiveCharacterTextSplitter): `langchain-text-splitters`
  is not a declared project dependency and isn't pulled in by `langchain-anthropic`.
  The tiktoken sliding window is used instead. Per spec §4.4, if recall@10 stalls
  below 0.75 the chunking strategy is the first lever to pull — that's when to
  either declare `langchain-text-splitters` as a dependency or implement the
  recursive splitter directly.

- **`download_guidances` for real**: the hardcoded `_CORPUS_URLS` list (8 URLs)
  has never been verified against the live FDA server. The actual download path
  is not covered by tests. See Open Questions #1 below.

- **Connection pooling**: `write_to_postgres` opens a fresh `psycopg.connect()`
  on every call (one per document). For the batch ingest job this is fine;
  the query path will need a shared pool. Noted but deferred — no `get_conn()`
  helper exists anywhere in `src/rra` yet.

### Decisions made unilaterally

1. **Tiktoken windowing instead of RecursiveCharacterTextSplitter.** The design
   doc proposed langchain_core's splitter but that class lives in
   `langchain-text-splitters`, which isn't installed. The tiktoken window produces
   identical chunk sizes and overlaps; the only loss is structural-boundary
   preference. Added a comment in `chunk_text` pointing to the spec §4.4 reopen
   condition so the deviation is visible.

2. **All DDL in a single transaction with the data write.** The design doc showed
   `_ensure_schema` committing separately. Since all DDL is `CREATE IF NOT EXISTS`
   (idempotent), merging it into the document transaction simplifies the code
   without changing semantics.

3. **`_embed_batch` creates a new `voyageai.Client` per call.** Slightly inefficient 
   (~50ms per batch of overhead) but acceptable for the once-a-week ingest job. 
   A lazy module-level singleton via functools.lru_cache would be cleaner and is the right fix; 
   deferred because the query-path code (day 3+) will need a shared client and is the 
   natural place to introduce the helper.

4. **PDF filename stem as `guidance_id`.** Used `path.stem` (e.g., `"72674"` for
   `72674.pdf`). See Open Question #2 for the readability tradeoff.

5. **8-URL hardcoded corpus, marked TODO.** Represents enough variety
   (SaMD, De Novo, 510k, design controls, software validation, cybersecurity)
   to bootstrap the retrieval eval. URLs have NOT been live-verified.

### Open questions to resolve next session

1. **Are the `_CORPUS_URLS` in `ingest.py` correct?** None were verified against
   the live FDA server (`https://www.fda.gov/media/{id}/download`). Before running
   ingest for real, check each URL returns a PDF (not a 404 or redirect to an HTML
   page). Add a `TODO(verify-urls)` search-and-fix pass to the Day 3 checklist.

2. **`guidance_id` = filename stem (e.g., `"72674"`) vs. human-readable title.**
   Numeric IDs are stable and collision-free but opaque in citations. A manifest
   file (`data/corpus/manifest.json`) mapping `id → {url, title, date}` would
   make citations readable without changing the schema — worth doing if you demo
   the system to a non-technical audience.

3. **psycopg directly vs. SQLAlchemy ORM.** The design doc flagged this.
   `write_to_postgres` uses psycopg3 directly for the bulk upsert. If the rest
   of the app (graph.py, MCP server) goes through SQLAlchemy Core/ORM, there may
   be a reason to standardize. No decision forced yet.

4. **Stale high-index rows on re-chunk.** If a re-run produces fewer chunks than
   the previous run (e.g., chunking strategy change), rows with high `chunk_index`
   values linger. The current upsert does not delete orphan rows. Accept or add a
   `DELETE ... WHERE guidance_id = %s` before the insert?

5. **`RecursiveCharacterTextSplitter` boundary preference.** Add
   `langchain-text-splitters` as a declared dependency and swap the chunker after
   baseline recall numbers are in hand (spec §4.4 trigger: recall@10 < 0.75).

### Commands blocked by hooks

None — no hook restrictions were encountered during this session.
