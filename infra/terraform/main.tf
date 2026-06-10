# ─────────────────────────────────────────────────────────────────────────────
# Provider, versions, and shared locals.
#
# Architecture (docs/spec.md §4.10): ECS Fargate behind an ALB, RDS Postgres 16
# (pgvector enabled at bootstrap), secrets in Secrets Manager, image in ECR.
# Networking is a 2-AZ VPC with public subnets (ALB) and private subnets
# (ECS tasks + RDS), egress via a single NAT gateway.
#
# Applied to AWS on 2026-06-08 (see terraform.tfstate, and the live-deploy ALB
# 503 post-mortem in docs/dev-log.md). Provisions the VPC / ECS Fargate / RDS
# Postgres / ALB / Secrets Manager stack per docs/spec.md §4.10.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  # Demo uses the default local backend. In production, use a remote backend with
  # state locking, e.g.:
  #
  #   backend "s3" {
  #     bucket         = "my-tfstate-bucket"
  #     key            = "rra/terraform.tfstate"
  #     region         = "us-east-1"
  #     dynamodb_table = "tf-locks"
  #     encrypt        = true
  #   }
}

provider "aws" {
  region = var.aws_region

  # Applied to every taggable resource; per-resource blocks add only `Name`.
  default_tags {
    tags = local.common_tags
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  # Resource name prefix, e.g. "rra-demo".
  name = "${var.project_name}-${var.environment}"

  # First two available AZs — RDS subnet groups and Fargate both need ≥2.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Component   = "regulatory-research-assistant"
  }
}
