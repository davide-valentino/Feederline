# ---------------------------------------------------------------------------
# Security groups. Default deny: nothing is open from the Internet.
#
#   rds        5432 from app + lambda SGs only
#   app        future ECS api+worker tasks; egress only
#   lambda     pilot activity Lambda (fed into the SAM child stack)
#   endpoints  not used: NAT covers egress; interface endpoints skipped
# ---------------------------------------------------------------------------
resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "Feederline development RDS"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "PostgreSQL from app tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }

  ingress {
    description     = "PostgreSQL from pilot activity Lambda"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    description = "Deny-by-default is implicit; explicit allow all egress for NTP/updates"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-rds" })
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "Feederline development ECS api + outbox worker tasks"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Egress via NAT: RDS, Secrets Manager, EventBridge, ECR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-app" })
}

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Feederline pilot activity Lambda (SAM child stack)"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Egress via NAT: RDS, Secrets Manager, MediaConnect control plane"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-lambda" })
}
