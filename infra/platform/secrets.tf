# ---------------------------------------------------------------------------
# DB credential secret. app/config.py reads DB_SECRET_ARN -> Secrets Manager
# -> JSON containing DB_URI, so the secret value shape matters, not the name.
#
# SecretString:
#   {"DB_URI": "postgresql+psycopg://feederline:<password>@<endpoint>:5432/feederline"}
#
# Read access is granted by the SAM child stack's activity role (template.yaml)
# against the exported ARN. ECS tasks will get the same grant when the app tier
# is wired up.
# ---------------------------------------------------------------------------
resource "random_password" "db_password" {
  length           = 24
  special          = false
  override_special = "" # keep it URL-safe without escaping
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "feederline/development/db"
  description             = "Feederline development database connection URL"
  recovery_window_in_days = 0 # pilot: delete immediately on destroy
  tags                    = merge(local.tags, { Name = "${local.name}-db-secret" })
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    DB_URI = "postgresql+psycopg://${aws_db_instance.postgres.username}:${random_password.db_password.result}@${aws_db_instance.postgres.address}:${aws_db_instance.postgres.port}/${aws_db_instance.postgres.db_name}"
  })
}
