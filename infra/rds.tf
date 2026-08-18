resource "aws_db_subnet_group" "this" {
  name       = "${var.project_name}-db-subnets"
  subnet_ids = data.aws_subnets.default_public.ids
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.project_name}-rds-"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Only the ECS task's own security group may reach Postgres - not the subnet's CIDR,
# not the world. Combined with publicly_accessible = false below, this is what actually
# isolates the database despite sitting in a subnet that routes to an IGW.
resource "aws_security_group_rule" "rds_from_task" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.rds.id
  source_security_group_id = aws_security_group.task.id
}

resource "aws_db_instance" "this" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 1

  # Disposable "get it live" deployment: no final-snapshot safety net so `terraform
  # destroy` completes in one pass. Flip to false + set final_snapshot_identifier before
  # this is anything longer-lived than a demo.
  skip_final_snapshot = true
  deletion_protection = false
  apply_immediately   = true
}
