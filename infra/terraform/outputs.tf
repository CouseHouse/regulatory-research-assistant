# ─────────────────────────────────────────────────────────────────────────────
# Outputs consumed by the Day-10 deploy/smoke-test flow.
# ─────────────────────────────────────────────────────────────────────────────

output "alb_dns_name" {
  description = "Public DNS name of the ALB."
  value       = aws_lb.main.dns_name
}

output "alb_url" {
  description = "Base URL to curl (POST /query, GET /health)."
  value       = "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "ECR repo URL — the `docker push` target and the image source for ECS."
  value       = aws_ecr_repository.app.repository_url
}

output "rds_endpoint" {
  description = "RDS endpoint (host:port)."
  value       = aws_db_instance.main.endpoint
}

output "rds_address" {
  description = "RDS hostname (no port) — POSTGRES_HOST for bootstrap/ingest."
  value       = aws_db_instance.main.address
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "ECS service name."
  value       = aws_ecs_service.app.name
}

output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.main.id
}

output "log_group_name" {
  description = "CloudWatch log group for the app container."
  value       = aws_cloudwatch_log_group.app.name
}

output "db_password_secret_arn" {
  description = "ARN of the generated DB password secret in Secrets Manager."
  value       = aws_secretsmanager_secret.db_password.arn
}
