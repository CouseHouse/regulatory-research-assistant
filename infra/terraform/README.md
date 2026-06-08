# Infrastructure — ECS Fargate + RDS + ALB (Terraform)

Terraform for the "deploy, demo, destroy" cloud run of the Regulatory Research
Assistant (see [`docs/spec.md` §4.10](../../docs/spec.md) and
[`docs/plan/day09.md`](../../docs/plan/day09.md)).

```
            internet
               │  HTTP :80
        ┌──────▼───────┐  public subnets (2 AZ)
        │     ALB      │
        └──────┬───────┘
               │  target group (ip)
        ┌──────▼───────┐  private subnets (2 AZ)
        │ ECS Fargate  │  rra.api:app on :8000  (1 task)
        │   service    │  secrets ← Secrets Manager (runtime inject)
        └──────┬───────┘
               │  :5432
        ┌──────▼───────┐  private subnets (2 AZ)
        │ RDS Postgres │  app DB + pgvector (ADR 0002)
        │      16      │  encrypted, not public
        └──────────────┘
  egress to Anthropic/Voyage/ECR/Secrets via a single NAT gateway
  image pulled from ECR · logs → CloudWatch
```

## File map

| File | What it provisions |
|---|---|
| `main.tf` | Providers (`aws ~>5`, `random ~>3`), region, default tags, AZ data, locals |
| `variables.tf` | All inputs; secrets marked `sensitive` |
| `vpc.tf` | VPC, 2 public + 2 private subnets, IGW, NAT gateway + EIP, route tables |
| `security.tf` | Security groups: ALB → ECS → RDS chain |
| `ecr.tf` | ECR repository + lifecycle policy (the `docker push` target) |
| `secrets.tf` | Secrets Manager entries (API keys) + generated DB password |
| `rds.tf` | DB subnet group, parameter group, RDS Postgres 16 instance |
| `ecs.tf` | Log group, IAM roles, cluster, task definition, ALB, target group, listener, service |
| `bootstrap.tf` | One-off corpus-ingest task def + log group for seeding the private RDS (ADR 0017) |
| `outputs.tf` | ALB URL, ECR URL, RDS endpoint, cluster/service names, bootstrap run-task command, etc. |
| `terraform.tfvars.example` | Template for your (gitignored) `terraform.tfvars` |

The app image is built from the repo-root [`Dockerfile`](../../Dockerfile); the
[`.dockerignore`](../../.dockerignore) keeps `.env` and secrets out of the image.

## Prerequisites

- Terraform ≥ 1.5, Docker, AWS CLI with credentials (`aws sts get-caller-identity`).
- An AWS Budget alert at **$5** before you `apply` (the NAT gateway + RDS are the
  resources that bleed money if you forget to destroy).

## Usage

```bash
cd infra/terraform

# Validate (no AWS creds needed):
terraform init
terraform validate
terraform fmt -check -recursive

# Configure secrets (NEVER commit terraform.tfvars):
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars        # set anthropic_api_key, voyage_api_key, rra_api_key

# Plan / apply (creates ~40 resources; RDS is the slow one, ~6 min):
terraform plan
terraform apply
```

Secrets can also come from the environment instead of `terraform.tfvars`:

```bash
export TF_VAR_anthropic_api_key=sk-ant-...
export TF_VAR_voyage_api_key=pa-...
export TF_VAR_rra_api_key=...
```

### Build + push the images (Day 10)

Two images share one ECR repo: the minimal **serving** image (`latest`) and the
**bootstrap** image (`bootstrap`), built from the `bootstrap` target — it carries
the source tree + `data/corpus/` so `rra.ingest` can run in-VPC (ADR 0017).

```bash
ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region "$(terraform output -raw 2>/dev/null; echo us-east-1)" \
  | docker login --username AWS --password-stdin "${ECR_URL%/*}"

# Serving image (default target):
docker build -t "$ECR_URL:latest" ../..              # build context = repo root
docker push "$ECR_URL:latest"

# Bootstrap image (separate target, same repo):
docker build --target bootstrap -t "$ECR_URL:bootstrap" ../..
docker push "$ECR_URL:bootstrap"
# Then re-apply (or force a new deployment) so ECS pulls the pushed tags.
```

### Bootstrap the database (ADR 0017)

RDS is private and ships without the `vector` extension, schema, or corpus. The
laptop can't reach it — so seed it with the one-off Fargate task (defined in
`bootstrap.tf`) that runs `rra.ingest` from inside the VPC. `rra.ingest` is
schema-sufficient: it creates the extension + `corpus.chunks` + indexes, then
embeds the corpus. Idempotent — safe to re-run.

```bash
# Kick off the one-off ingest task (uses the private subnets + ecs_tasks SG;
# the RDS SG already trusts that SG, NAT egress reaches FDA + Voyage):
eval "$(terraform output -raw bootstrap_run_task_command)"

# Tail it (find the task ARN from the run-task output, or via list-tasks):
aws logs tail "/ecs/$(terraform output -raw ecs_cluster_name)-bootstrap" --follow
# Done when you see `corpus.ingest.complete ingested=<n>`.
```

To ingest the full corpus or reset first, override the args via
`var.bootstrap_ingest_command` (e.g. `[]` for all docs, `["--truncate"]` to wipe
`corpus.chunks` before re-ingesting) and re-apply before running the task.

> `init-db/01-init.sql` additionally creates `app.query_audit` (offline analysis).
> It is **not** created by this flow and **not** required to serve `/query` — the
> app never writes it at runtime. Add it later if/when that table is wired up.

### Smoke test

```bash
BASE=$(terraform output -raw alb_url)
curl "$BASE/health"                                   # {"status":"ok"}  (liveness; DB-free)
curl "$BASE/readyz"                                   # {"status":"ready"} — confirms RDS is
                                                      # reachable BEFORE the first /query (W1).
                                                      # 503 here = DB connectivity problem
                                                      # (SG/subnet/creds), not an app bug.
curl -X POST "$BASE/query" -H "Content-Type: application/json" \
  -H "X-API-Key: <rra_api_key>" \
  -d '{"query":"...","product_context":"..."}'
```

### Destroy

```bash
terraform destroy
```

Confirm in the console that the **NAT gateway, EIP, RDS instance, and ALB** are
gone — those are the ones that keep charging.

## Cost (us-east-1, rough)

| Resource | 24/7 (~/mo) | 2–3 hr demo |
|---|---|---|
| NAT gateway | ~$32 + data | ~$0.10 |
| RDS db.t4g.micro + 20 GB gp3 | ~$14 | ~$0.05 |
| ALB | ~$16 + LCU | ~$0.05 |
| Fargate 0.5 vCPU / 1 GB | ~$18 | ~$0.05 |
| Secrets Manager (6 secrets) | ~$2.40 | ~$0 |
| **Total** | **~$80–100/mo** | **well under $5** |

The whole point of Fargate + per-second billing: deploy, demo, destroy for the
price of a coffee. **The NAT gateway is the silent budget killer — destroy it.**

## Security notes

- **No secrets in the image.** `.dockerignore` excludes `.env`; the Dockerfile
  copies only `pyproject.toml`, `uv.lock`, `README.md`, and `src/`. Secrets are
  injected at runtime from Secrets Manager via the task definition `secrets`
  block — never plaintext env, never baked in.
- **DB password is generated** (`random_password`) and stored in Secrets Manager;
  no human-set password enters the codebase. RDS consumes the same value.
- **Least-exposure SGs:** only the ALB is reachable from the ingress CIDR; ECS
  accepts traffic only from the ALB SG; RDS only from the ECS SG.
- **RDS** is `storage_encrypted = true`, `publicly_accessible = false`, in private
  subnets, with `rds.force_ssl = 1`.
- Lock `alb_ingress_cidr` to your IP for a non-public demo.

## Intentionally deferred (v1 demo scope)

- **HTTPS / ACM certificate** — the demo uses HTTP :80 (day09 plan curls `http://`).
  Add an ACM cert + :443 listener for anything real.
- **Multi-AZ RDS** and **one NAT per AZ** — single-AZ / single-NAT for demo cost.
- **VPC endpoints** (ECR/Secrets/Logs) — would remove NAT dependency and cost in
  steady state; NAT is simpler for a short demo.
- **Autoscaling** — `desired_count = 1`.
- **Remote state backend** — local backend here; an S3 + lock-table example is
  commented in `main.tf`.

> **Status:** authored and `terraform validate`-clean. NOT applied in the build
> session (no AWS credentials, by design) — `apply` is the Day-10 step.
