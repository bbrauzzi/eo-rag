resource "random_password" "db" {
  length  = 32
  special = false # avoids characters that would need URL-encoding inside DATABASE_URL
}

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "${var.project_name}/anthropic-api-key"

  # Immediate delete on destroy, not the default 30-day hold - so a `destroy` followed
  # shortly by a fresh `apply` doesn't collide with a same-named secret still pending
  # deletion. Fine for a disposable dev deployment; reconsider before this is anything
  # longer-lived.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "anthropic_api_key" {
  secret_id     = aws_secretsmanager_secret.anthropic_api_key.id
  secret_string = var.anthropic_api_key
}

# One secret, one env var: ECS `secrets` injection is a single ARN -> a single env var,
# and app/config.py reads DATABASE_URL directly - so the full connection string is
# assembled here rather than shipping host/user/password as separate pieces the task
# would have to reassemble itself.
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.project_name}/database-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg://${var.db_username}:${urlencode(random_password.db.result)}@${aws_db_instance.this.address}:5432/${var.db_name}"
}
