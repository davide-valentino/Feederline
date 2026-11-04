# ---------------------------------------------------------------------------
# App-tier scaffolding: ECR + ECS cluster + IAM, created now so the
# container milestone only adds task definitions/services.
#
# No services are defined yet: there is no Dockerfile in the repo, and a
# task definition pinned to a non-existent ECR image would make the platform
# apply fail. Task defs (api + outbox worker, ARM, private subnets, DB Secret
# grant below) land in the next milestone.
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "api" {
  name         = "${local.name}/api"
  force_delete = true
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = merge(local.tags, { Name = "${local.name}/api" })
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # pilot: keep billable extras off
  }
  tags = merge(local.tags, { Name = local.name })
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  description        = "Pull images and read container secrets (Fargate)"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = merge(local.tags, { Name = "${local.name}-ecs-execution" })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task read access to the DB secret (mirrors the Lambda grant in template.yaml).
resource "aws_iam_role_policy" "ecs_execution_db_secret" {
  name = "db-secret"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow",
      Action   = "secretsmanager:GetSecretValue",
      Resource = aws_secretsmanager_secret.db.arn
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name}-ecs-task"
  description        = "Runtime permissions for api + outbox worker (EventBridge / Step Functions)"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = merge(local.tags, { Name = "${local.name}-ecs-task" })
}

# Runtime permission set mirrors app/config.py + app/outbox.py needs:
# EventBridge Publish + StepFunctions StartExecution on the pilot stack output.
# ARNs of the state machine are injected when the app tier milestone wires
# services: stack output DirectMediaWorkflowArn -> map into this policy here.
resource "aws_iam_role_policy" "ecs_task_publish" {
  name = "publish-events"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = aws_cloudwatch_event_bus.main.arn
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.db.arn
      }
    ]
  })
}
