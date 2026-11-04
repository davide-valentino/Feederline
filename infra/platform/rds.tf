# ---------------------------------------------------------------------------
# RDS for PostgreSQL — the evaluated pilot spec:
#   db.t4g.small (2 vCPU, 2 GiB, Graviton2), Single-AZ, gp3 20 GB
#   private subnets in 2 AZs, no public access, encrypted storage, TLS
#
# Pilot-grade settings (explicit, revisit at production):
#   deletion_protection = false, skip_final_snapshot = true
#   so test-deploy cycles and teardown stay frictionless.
# ---------------------------------------------------------------------------
data "aws_rds_engine_version" "postgres" {
  engine       = "postgres"
  default_only = true
}

resource "aws_db_subnet_group" "main" {
  name        = "${local.name}-db"
  description = "Private subnets for the Feederline development database"
  subnet_ids  = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  tags        = merge(local.tags, { Name = "${local.name}-db" })
}

resource "aws_db_instance" "postgres" {
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version # pinned to the AWS default version, resolved at apply
  instance_class = "db.t4g.small"

  db_name  = "feederline"
  username = "feederline"
  password = random_password.db_password.result
  port     = 5432

  allocated_storage     = 20
  storage_type          = "gp3"
  max_allocated_storage = 100 # pilot autoscale ceiling

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false # Single-AZ per evaluation; migrate on first SLA need
  availability_zone      = local.azs[0]

  storage_encrypted     = true
  copy_tags_to_snapshot = true

  backup_retention_period = 7
  backup_window           = "01:00-02:00"
  maintenance_window      = "sun:02:00-sun:04:00"

  auto_minor_version_upgrade   = true
  performance_insights_enabled = false # skip cost and keep minimal

  deletion_protection       = false # pilot: allow test-deploy teardown
  skip_final_snapshot       = true
  final_snapshot_identifier = null

  tags = merge(local.tags, {
    Name      = "${local.name}-db"
    component = "database"
  })
}
