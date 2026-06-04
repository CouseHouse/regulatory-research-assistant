# ─────────────────────────────────────────────────────────────────────────────
# Secrets Manager. The application reads every secret at RUNTIME — the ECS task
# definition (ecs.tf) injects them as the container's `secrets`, so they are
# never baked into the image and never sit in plaintext env vars.
#
# The DB password is GENERATED here (random_password) and stored in Secrets
# Manager; RDS (rds.tf) consumes the same value. No human-set DB password ever
# enters the codebase. API keys come from sensitive variables.
# ─────────────────────────────────────────────────────────────────────────────

resource "random_password" "db" {
  length  = 32
  special = true
  # RDS forbids '/', '@', '"', and spaces in the master password.
  override_special = "!#$%&*()-_=+[]{}:?"
}

locals {
  # Secret env-var names → drives for_each. These keys are STATIC and
  # non-sensitive, so for_each never receives a sensitive value (a Terraform
  # restriction). The toggle is a plain bool, so app_secret_names stays
  # non-sensitive even though it gates the optional Langfuse keys.
  #
  # Langfuse is optional and only injected when enable_langfuse is true —
  # Secrets Manager rejects an empty secret_string, so we must NOT create the
  # Langfuse secrets when their values are blank (the default). When absent the
  # app reads no key → config.langfuse_enabled is False → tracing no-ops.
  required_secret_names = [
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "RRA_API_KEY",
  ]
  langfuse_secret_names = var.enable_langfuse ? ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"] : []
  app_secret_names      = concat(local.required_secret_names, local.langfuse_secret_names)

  # Values keyed by name (sensitive values OK here — this map never feeds a
  # for_each; it is only indexed by the static keys above).
  app_secret_values = {
    ANTHROPIC_API_KEY   = var.anthropic_api_key
    VOYAGE_API_KEY      = var.voyage_api_key
    RRA_API_KEY         = var.rra_api_key
    LANGFUSE_PUBLIC_KEY = var.langfuse_public_key
    LANGFUSE_SECRET_KEY = var.langfuse_secret_key
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each = toset(local.app_secret_names)

  name = "${local.name}/${each.key}"
  # Demo convenience: 0 = delete immediately (no 7–30 day recovery window) so a
  # destroy + re-apply doesn't collide with a "scheduled for deletion" secret.
  recovery_window_in_days = 0

  tags = { Name = "${local.name}-${lower(each.key)}" }
}

resource "aws_secretsmanager_secret_version" "app" {
  for_each = toset(local.app_secret_names)

  secret_id     = aws_secretsmanager_secret.app[each.key].id
  secret_string = local.app_secret_values[each.key]
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = "${local.name}/POSTGRES_PASSWORD"
  recovery_window_in_days = 0

  tags = { Name = "${local.name}-postgres-password" }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db.result
}

locals {
  # Every secret ARN the ECS execution role must be allowed to read.
  all_secret_arns = concat(
    [for s in aws_secretsmanager_secret.app : s.arn],
    [aws_secretsmanager_secret.db_password.arn],
  )
}
