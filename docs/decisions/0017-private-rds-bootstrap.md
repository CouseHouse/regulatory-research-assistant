# 0017 — Private-RDS bootstrap via a one-off in-VPC ingest task

**Status:** Active
**Date:** 2026-06-08
**Owner:** Kyle Couse

## Context

RDS is `publicly_accessible = false` (`infra/terraform/rds.tf:51`), so a fresh instance has no
`vector` extension, no `corpus.chunks` schema, and no embedded corpus — yet nothing in the IaC
creates them, and the laptop cannot reach the private endpoint to run `init-db/01-init.sql` or
`rra.ingest` (finding B1). `/health` is DB-free by design (`src/rra/api.py:27-35`), so the task
deploys "healthy" and the first `/query` 500s on `relation "corpus.chunks" does not exist`. The
serving image cannot self-bootstrap: `.dockerignore:27` strips `data/`, so `data/corpus/manifest.json`
(`src/rra/ingest.py:44,89`) is absent, and `PROJECT_ROOT` (`src/rra/config.py:37`) resolves to the
wheel path, not the repo root.

## Decision

We will bootstrap the private RDS with a one-off ECS Fargate `run-task` that runs `python -m rra.ingest`
from a dedicated **bootstrap image** (repo source + `data/corpus/` + editable install), launched into
the private subnets on the existing ECS-task security group — RDS never becomes publicly accessible.

## Alternatives considered

- **Bastion host / SSM port-forward, then run ingest from the laptop** — Rejected. Adds a long-lived
  host and SSH/SSM surface to a 42-resource demo, and the bootstrap then runs from an un-pinned local
  environment. The regulated-vertical narrative wants the DB reachable only from inside the VPC.
- **Temporarily flip `publicly_accessible = true` + open the RDS SG to my IP, run from laptop, revert** —
  Rejected. Transiently exposes the regulated datastore to the internet and depends on a manual revert;
  exactly the posture this project's SG chain (`security.tf`) exists to forbid.
- **ECS Exec into the running app task** — Rejected as non-viable: the serving image lacks the corpus
  data and manifest, `PROJECT_ROOT` mis-resolves in the wheel layout, and `enable_execute_command` +
  task-role SSM perms are not set. Making it work means shipping corpus data into the serving image —
  which the minimal-image design (ADR-adjacent, `.dockerignore`) deliberately rejects.
- **Run ingest on app-container startup (entrypoint migration)** — Rejected. Couples a slow, paid Voyage
  embed job into every cold start, bloats the serving image with the corpus, and stalls liveness behind
  ingestion. Bootstrap is a once-per-environment event, not a per-task one.

## Consequences

**Enables:**
- A reproducible, in-VPC bootstrap that reuses what already exists: the `ecs_tasks` SG (the RDS SG
  already trusts it, `security.tf:70-75`), NAT egress for FDA + Voyage, and Secrets Manager wiring.
- Idempotent re-runs — `_ensure_schema()` is `CREATE ... IF NOT EXISTS` and writes upsert `ON CONFLICT`
  (`src/rra/ingest.py:602-633,546-560`), so the task is safe to re-run and doubles as a re-ingest path.
- `rra.ingest` is schema-sufficient on its own: it creates the `vector` extension, the `corpus`/`app`
  schemas, `corpus.chunks`, and the indexes — `app.query_audit` (from `01-init.sql`) is not written at
  runtime, so it is not on the `/query` critical path.

**Constrains:**
- Adds a second image (bootstrap) and a second task definition to build and maintain; the corpus data
  ships in the bootstrap image (never the serving image).
- Bootstrap is a deliberate operator step (`aws ecs run-task`), not part of `terraform apply`. The
  deploy runbook must call it out between apply and smoke test.

**Reopen if:**
- The corpus moves to object storage (S3) or a managed migration tool is adopted — either changes how
  the bootstrap sources data and may fold it into apply.
- Ingest stops being a one-off (e.g. scheduled corpus refresh) — then it wants a scheduled job, not a
  manual `run-task`.
- Policy ever makes RDS publicly reachable — the in-VPC justification weakens.

## Related

- Finding B1 (pre-deploy static analysis, 2026-06-08): the gap this ADR closes.
- `infra/terraform/README.md:89-98` — the manual bootstrap note this ADR replaces with a codified task.
- ADR 0002 (pgvector in Postgres), ADR 0004 (psycopg pool), ADR 0007 (corpus scope).
- spec.md §4.10 (deployment architecture).
