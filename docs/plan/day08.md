# Day 8 — Terraform / IaC

## Goal

Infrastructure-as-code for the cloud demo. Don't deploy yet — get the config right first. Tomorrow is `apply + smoke test + destroy`.

## Deliverables

- `infra/terraform/main.tf`: provider config, locals, common tags
- `infra/terraform/variables.tf`: aws_region, project_name, anthropic_api_key (sensitive), voyage_api_key (sensitive)
- `infra/terraform/vpc.tf`: VPC, 2 public subnets (ALB), 2 private subnets (ECS + RDS), NAT gateway
- `infra/terraform/database.tf`: RDS Postgres 16 with pgvector extension enabled
- `infra/terraform/ecs.tf`: cluster, task definition, service, ALB, target group, listeners
- `infra/terraform/secrets.tf`: Secrets Manager entries for API keys, DB password
- `infra/terraform/security.tf`: security groups for ALB, ECS, RDS
- `infra/terraform/outputs.tf`: ALB DNS name, RDS endpoint
- `infra/terraform/README.md`: cost breakdown, deploy/destroy instructions, what each file does
- `infra/terraform/.gitignore`: excludes `.tfstate*`, `.terraform/`, `*.tfvars` (except `.example`)
- `infra/terraform/terraform.tfvars.example`: template for variables file
- `Dockerfile`: app container (Python 3.11, uv, the rra package, FastAPI/uvicorn entrypoint)
- `.dockerignore`: excludes `.env`, `__pycache__`, `.venv`, `.git`, `infra/`, `docs/`

## Design constraints

- ECS Fargate, NOT EKS (spec §4.10)
- Multi-agent task timeout: Fargate task can run as long as needed; ALB idle timeout set high (300s)
- RDS Postgres 16 — pgvector extension needs to be enabled via `parameter_group` and `CREATE EXTENSION` in app startup
- All secrets via Secrets Manager, NOT environment variables in plain text
- Container image: **build the app image** (see Decision 6 below) — public images demonstrate infra scaffolding, not the actual system

## Decisions to make

1. RDS instance size: `db.t4g.micro` is cheapest (~$15/mo); `db.t4g.small` is safer (~$30/mo). For a 2-hour demo, either is fine.
2. Single-AZ or Multi-AZ RDS: single-AZ is fine for demo; multi-AZ is production-shaped but doubles cost.
3. Fargate task size: 0.5 vCPU / 1GB RAM is enough for the agent; 1 vCPU / 2GB if memory pressure during multi-agent runs.
4. ALB scheme: public (internet-facing) for the demo; internal for production would be cleaner but you can't curl it from your laptop.
5. NAT gateway: ~$32/mo if running 24/7. For a 2-hour demo it's ~$0.10. Just remember to destroy.
6. **Public image vs. build the app image — resolved: build the app image.** A public image (nginx, python:3.11-slim, etc.) can demonstrate that ECS, ALB, and RDS are wired together, but it cannot run the rra multi-agent system. To demo the actual product on Day 9 — the strong portfolio story — the Dockerfile must package the app and be pushed to ECR. Build time is ~5 minutes once; the Day 9 apply + smoke test depends on it. Secrets consequence: the `.env` file and API keys must NOT be baked into the image. They are injected at runtime via ECS task definition environment variables sourced from Secrets Manager (the existing `secrets.tf` deliverable). The `Dockerfile` must not copy `.env` (enforced by `.dockerignore`).

## Stop conditions

- `terraform init` succeeds
- `terraform plan` runs clean — review the output carefully:
  - Nothing accidentally public except the ALB
  - All EBS/RDS storage encrypted
  - No security groups with 0.0.0.0/0 except ALB 80/443
  - Secrets Manager values flagged as sensitive in the plan output
- Plan output count looks sane (probably 30-50 resources)
- DO NOT apply yet

## Don't do yet

- `terraform apply` (day 9)
- A CI pipeline that builds the container (day 12 stretch)
- Multi-region anything
- Auto-scaling — single task is fine for the demo

## Cost guard (read before day 9)

Set an AWS Budget alert at $5 before tomorrow's apply. The worst-case outcome is forgetting a NAT gateway or RDS instance and getting a $40 surprise three weeks later.

## Definition of done

`terraform plan` output saved to `infra/terraform/plan-day8.txt` for your reference. Dev-log entry shows: resource count, estimated hourly cost (`terraform plan` doesn't give this; use the AWS calculator or just note rough estimates per resource), the most expensive resources, and any choices you'd revisit.
