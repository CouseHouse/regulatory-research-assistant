# ─────────────────────────────────────────────────────────────────────────────
# ECR repository — the push target for the app image (Dockerfile at repo root).
# The Day-10 `docker push` and the ECS task definition (ecs.tf) both depend on
# this; without it the deploy has nowhere to pull the image from.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  # Demo: allow `terraform destroy` to remove the repo even if it still holds
  # images. Drop this for production so images aren't deleted by accident.
  force_delete = true

  tags = { Name = "${local.name}-ecr" }
}

# Keep the repo tidy: expire untagged images after 7 days.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images older than 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
    ]
  })
}
