# ─────────────────────────────────────────────────────────────────────────────
# Security groups — a strict three-tier chain:
#   clients ──HTTP:80──▶ ALB ──:container_port──▶ ECS tasks ──:5432──▶ RDS
# The ALB is the ONLY group exposed to the ingress CIDR. ECS accepts traffic
# only from the ALB SG; RDS accepts traffic only from the ECS SG. Nothing else
# can reach the database (docs/plan/day08.md design constraints).
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "ALB: HTTP from clients"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP from allowed clients"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.alb_ingress_cidr]
  }

  egress {
    description = "All outbound (to ECS targets)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "ecs_tasks" {
  name_prefix = "${local.name}-ecs-"
  description = "ECS tasks: app port from the ALB only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "App port from the ALB SG only"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "All outbound (Anthropic/Voyage APIs, ECR, Secrets Manager, RDS via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-ecs-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "rds" {
  name_prefix = "${local.name}-rds-"
  description = "RDS: Postgres from ECS tasks only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from the ECS task SG only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs_tasks.id]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-rds-sg" }

  lifecycle {
    create_before_destroy = true
  }
}
