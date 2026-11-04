# ---------------------------------------------------------------------------
# ECS app tier: api (uvicorn) + outbox worker (python -m app.outbox), one
# image, Fargate ARM64, private subnets, app SG to RDS. No load balancer:
# during the pilot the API is reached through the SSM bastion port-forward
# (same pattern as infra/db_tunnel.sh); an internal ALB can be added when
# there is a real consumer.
#
# EVENT_PUBLISHER starts as "eventbridge" (outbox -> tv.feederline proven
# first) and flips to "stepfunctions" once the publish path is verified —
# then the worker also starts the DirectMediaWorkflow executions itself, so
# delivery_workflow_run.mapping stays accurate (see infra/README.md).
# ---------------------------------------------------------------------------
variable "image_tag" {
  description = "ECR image tag for both api and worker containers"
  type        = string
  default     = "v0.1.0"
}

variable "event_publisher" {
  description = "Outbox publisher mode: log | eventbridge | stepfunctions"
  type        = string
  default     = "eventbridge"
}

variable "api_desired_count" {
  description = "API tasks to keep running (worker always 1)"
  type        = number
  default     = 1
}

locals {
  image = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"

  shared_environment = [
    { name = "PYTHONUNBUFFERED", value = "1" },
    { name = "EVENT_PUBLISHER", value = var.event_publisher },
    { name = "EVENT_BUS_NAME", value = aws_cloudwatch_event_bus.main.name },
    { name = "WORKFLOW_STATE_MACHINE_ARN", value = data.aws_sfn_state_machine.direct_media.arn },
    # Not confidential (an ARN): app/config.py fetches the DB URI with the
    # task role at import. Avoids ECS's Secrets Manager JSON-key selector,
    # which rejects path-style secret names ("unexpected ARN format").
    { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db.arn },
  ]

  shared_secrets = [
    # Plain ARN (whole-secret reference) — the token is a single string, so
    # no selector is needed.
    { name = "OPERATOR_TOKEN", valueFrom = aws_secretsmanager_secret.operator_token.arn },
  ]

  log_options = {
    awslogs-group         = "/ecs/${local.name}"
    awslogs-region        = "eu-west-1"
    awslogs-stream-prefix = "app"
  }
}

# --- CloudWatch log group (30 d retention; execution role has awslogs perms) --
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
  tags              = merge(local.tags, { Name = "${local.name}-app-logs" })
}

# --- Task definitions ---------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256 # 0.25 vCPU
  memory                   = 512 # MB
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.image
      essential = true
      portMappings = [
        { containerPort = 8000, protocol = "tcp" }
      ]
      environment = local.shared_environment
      secrets     = local.shared_secrets
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz', timeout=5)\""]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      logConfiguration = {
        logDriver = "awslogs"
        options   = local.log_options
      }
    }
  ])

  tags = merge(local.tags, { Name = "${local.name}-api", component = "app" })
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true
      command   = ["python", "-m", "app.outbox"]
      environment = concat(local.shared_environment, [
        { name = "OUTBOX_POLL_SECONDS", value = "2" }
      ])
      secrets          = local.shared_secrets
      logConfiguration = { logDriver = "awslogs", options = local.log_options }
    }
  ])

  tags = merge(local.tags, { Name = "${local.name}-worker", component = "app" })
}

# --- Services (1 task each, private subnets, app SG) ---------------------------
resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_groups = [aws_security_group.app.id]
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Ad-hoc debugging / smoke probes inside the container (task role has
  # ssmmessages perms). Off by default in production-like stacks.
  enable_execute_command = true

  depends_on = [aws_cloudwatch_log_group.app]
  tags       = merge(local.tags, { Name = "${local.name}-api" })
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_groups = [aws_security_group.app.id]
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  enable_execute_command = true

  depends_on = [aws_cloudwatch_log_group.app]
  tags       = merge(local.tags, { Name = "${local.name}-worker" })
}

# --- Outputs consumed by smoke scripts ----------------------------------------
output "app_image" {
  description = "Image tag both containers run"
  value       = local.image
}

output "publisher_mode" {
  value = var.event_publisher
}