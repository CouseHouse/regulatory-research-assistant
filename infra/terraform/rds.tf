# ─────────────────────────────────────────────────────────────────────────────
# RDS Postgres 16 — application DB AND vector store (pgvector lives in the same
# Postgres, ADR 0002). Private subnets, not publicly accessible, encrypted at
# rest. pgvector is enabled at bootstrap with `CREATE EXTENSION vector` (run
# init-db/01-init.sql against the instance) — it needs no shared_preload_libraries.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${local.name}-db-subnets" }
}

# Parameter group hook for Postgres 16. pgvector itself only needs CREATE
# EXTENSION; this exists as the place to tune logging / SSL enforcement.
resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = { Name = "${local.name}-pg16" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.main.name

  publicly_accessible = false
  multi_az            = false # demo: single-AZ. Production: true.

  # Demo lifecycle: no final snapshot, deletion allowed, changes apply now.
  skip_final_snapshot = true
  deletion_protection = false
  apply_immediately   = true

  tags = { Name = "${local.name}-pg" }
}
