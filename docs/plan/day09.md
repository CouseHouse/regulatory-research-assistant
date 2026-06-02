# Day 9 — Deploy, demo, destroy

## Goal

Run the system in AWS end-to-end. Capture proof. Tear it down same day.

This is a single working session, not a multi-day deployment project. Plan for 3-4 hours.

## Sequence

### 1. Pre-flight (10 min)
- AWS Budget alert set at $5 (verify in console)
- AWS credentials configured: `aws sts get-caller-identity` returns your account
- Container image ready: either build locally and `docker push` to ECR, OR use a public image for v1 demo

### 2. Apply (15 min)
```bash
cd infra/terraform
terraform apply
```
Takes ~6 minutes. RDS is the slowest resource. Watch for errors; the most common is IAM permission gaps.

### 3. Bootstrap the database (15 min)
- Connect to RDS from a bastion or the ECS task: `psql -h <rds-endpoint> -U postgres`
- Run `init-db/01-init.sql` to create schemas + pgvector extension
- Run ingest job pointed at cloud DB: `DATABASE_URL=<rds-dsn> uv run python -m rra.ingest --limit 50`
  - Use `--limit 50`, not the full 200 — cloud demo doesn't need the full corpus
  - This step is the most likely to fail; budget time for it

### 4. Smoke test (15 min)
- Get ALB DNS from `terraform output`
- One query:
```bash
curl -X POST http://<alb-dns>/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key-from-secrets-manager>" \
  -d '{"query":"...","product_context":"..."}'
```
- Verify response includes citations + Langfuse trace URL
- Open the trace in your local Langfuse — confirms the cloud deployment publishes to Langfuse correctly

### 5. Eval subset (20 min)
- Run 5 questions from `evals/golden.jsonl` against the cloud endpoint
- Capture results to `evals/results/cloud-day9.md`
- Don't expect identical scores to local — slight variation is normal

### 6. Capture proof (15 min)
- Screenshot the AWS console showing the running ECS task
- Screenshot the curl response
- Screenshot the Langfuse trace
- Save all to `docs/cloud-demo/` (gitignore the screenshots if they leak account IDs)
- Save the `terraform apply` output

### 7. Destroy (10 min)
```bash
terraform destroy
```
Verify in AWS console:
- ECS cluster gone
- RDS gone (NOT just stopped — destroyed)
- ALB gone
- NAT gateway gone (~$0.045/hour adds up)
- EIPs released
- Secrets Manager entries can stay (they're a few cents/month and rebuilding them is friction)

### 8. Cost check (5 min)
- AWS Cost Explorer → today's spend
- Expected: $1-5 total
- Note actual cost in dev-log; this is real interview material ("I deployed and destroyed for under $5")

## Stop conditions

- One real query worked against the cloud deployment
- Eval subset captured
- All resources destroyed
- Dev-log entry written with cost number

## When to abort

If `terraform apply` fails twice with different errors, stop and fix the IaC tomorrow. Don't try to debug in AWS console — the IaC is the source of truth.

If the cloud DB ingest takes >30 minutes, kill it. Cloud ingestion isn't the demo; you can ingest 5 docs manually for the demo to work.

## Don't do yet

- Multi-region
- Auto-scaling
- CI/CD pipeline for deployments
- The Loom video (day 13)

## Definition of done

Dev-log entry shows: actual AWS cost, the smoke-test response, what broke during apply (there's always something), and confirmation that destroy succeeded.

## If you're skipping cloud deploy entirely

If the cut-list says skip day 8-9: instead of this work, take screenshots of `terraform plan` output and write a `docs/cloud-design.md` describing what would deploy. Less impressive but honest.
