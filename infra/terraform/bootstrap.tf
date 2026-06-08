# ─────────────────────────────────────────────────────────────────────────────
# One-off corpus-bootstrap task (ADR 0017).
#
# RDS is private (rds.tf: publicly_accessible = false), so a fresh instance has
# no `vector` extension, no corpus.chunks, and no embedded corpus — and the
# laptop can't reach the endpoint to seed it. This registers a Fargate task
# that runs `python -m rra.ingest` from the bootstrap IMAGE (Dockerfile
# `bootstrap` target: source tree + data/corpus). Run it ONCE, by hand, after
# apply + image push:
#
#   aws ecs run-task ...   (the exact command is in `terraform output bootstrap_run_task_command`)
#
# It reuses everything the app task already has:
#   - the ecs_tasks SG — RDS already trusts it on 5432 (security.tf), and NAT
#     egress reaches FDA (PDF download) + Voyage (embeddings);
#   - the execution role — pulls the image, writes logs, reads the runtime secrets;
#   - Secrets Manager — ANTHROPIC_API_KEY + VOYAGE_API_KEY (both required by the
#     config singleton at import) + the generated DB password.
# No new network rule, repo, or standing resource. The task runs ~1–2 min and
# exits; nothing keeps charging. Idempotent: safe to re-run (CREATE ... IF NOT
# EXISTS + upsert ON CONFLICT in rra.ingest).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "bootstrap" {
  name              = "/ecs/${local.name}-bootstrap"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-bootstrap-logs" }
}

resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${local.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.bootstrap_cpu
  memory                   = var.bootstrap_memory
  # Same roles as the app task: execution role pulls the image + reads secrets;
  # the (empty) task role is the ingest process's own identity — it calls no AWS
  # APIs, only Postgres/FDA/Voyage over the network.
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "bootstrap"
      image     = "${aws_ecr_repository.app.repository_url}:${var.bootstrap_image_tag}"
      essential = true

      # Overrides the image CMD. Ingest module is the ENTRYPOINT (Dockerfile).
      command = var.bootstrap_ingest_command

      # Same DB wiring as the app task — config.py assembles the DSN from these
      # pieces; POSTGRES_HOST points at the private RDS endpoint. No Langfuse:
      # ingest does not trace.
      environment = [
        { name = "POSTGRES_HOST", value = aws_db_instance.main.address },
        { name = "POSTGRES_PORT", value = tostring(aws_db_instance.main.port) },
        { name = "POSTGRES_DB", value = var.db_name },
        { name = "POSTGRES_USER", value = var.db_username },
      ]

      # Ingest itself only calls Voyage (embeddings) + Postgres. But config.py
      # instantiates the Settings() singleton at import time (config.py:150) and
      # ANTHROPIC_API_KEY is a REQUIRED field with no default — so it must be
      # present or `import rra.config` fails fast before ingest runs. VOYAGE is
      # likewise required. RRA_API_KEY and POSTGRES_PASSWORD have defaults, so
      # only the two LLM keys + the real DB password are injected here.
      secrets = [
        {
          name      = "ANTHROPIC_API_KEY"
          valueFrom = aws_secretsmanager_secret.app["ANTHROPIC_API_KEY"].arn
        },
        {
          name      = "VOYAGE_API_KEY"
          valueFrom = aws_secretsmanager_secret.app["VOYAGE_API_KEY"].arn
        },
        {
          name      = "POSTGRES_PASSWORD"
          valueFrom = aws_secretsmanager_secret.db_password.arn
        },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.bootstrap.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "bootstrap"
        }
      }
    },
  ])

  tags = { Name = "${local.name}-bootstrap-taskdef" }
}
