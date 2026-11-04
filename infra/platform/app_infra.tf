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
  name        = "${local.name}-ecs-execution"
  description = "Pull images and read container secrets (Fargate)"
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

# Execution-role secret reads: containers get DB_URI (JSON key selector on the
# DB secret) and OPERATOR_TOKEN injected by ECS at task launch.
resource "aws_iam_role_policy" "ecs_execution_db_secret" {
  name = "db-secret"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow",
      Action = "secretsmanager:GetSecretValue",
      Resource = [
        aws_secretsmanager_secret.db.arn,
        aws_secretsmanager_secret.operator_token.arn,
      ]
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name        = "${local.name}-ecs-task"
  description = "Runtime permissions for api + outbox worker (EventBridge / Step Functions)"
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
# EventBridge Publish, secret read (DB_SECRET_ARN fallback), Step Functions
# start/describe on the pilot state machine, and ECS Exec debugging
# (ssmmessages). The state machine belongs to the SAM child stack, so it is
# looked up by name here and never re-declared in Terraform. The lookup is
# gated by sam_stack_deployed so the FIRST apply after a teardown can run
# before `infra/deploy_pilot.sh` recreates the child stack.
variable "sam_stack_deployed" {
  description = "Set false for the first apply after a teardown; deploy the SAM stack, then apply again with the default"
  type        = bool
  default     = true
}

data "aws_sfn_state_machine" "direct_media" {
  count = var.sam_stack_deployed ? 1 : 0
  name  = "feederline-direct-media"
}

locals {
  # '' when the SAM stack is absent (try() absorbs the index error; no data
  # source is read at all once count == 0)
  sfn_arn = try(data.aws_sfn_state_machine.direct_media[0].arn, "")
}

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
      },
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution",
          "states:DescribeExecution",
        ]
        Resource = local.sfn_arn != "" ? local.sfn_arn : "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      }
    ]
  })
}
