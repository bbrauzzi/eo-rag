resource "aws_ecs_cluster" "this" {
  name = "${var.project_name}-cluster"
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_security_group" "task" {
  name_prefix = "${var.project_name}-task-"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

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

resource "aws_ecs_task_definition" "app" {
  family                   = var.project_name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "app"
      image     = "${var.ecr_repository_url}:${var.image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "AWS_REGION", value = var.aws_region },
        { name = "CLAUDE_MODEL", value = var.claude_model },
        { name = "EMBEDDING_MODEL", value = var.embedding_model },
        { name = "EMBEDDING_DIM", value = tostring(var.embedding_dim) },

        # The ALB is a proxy we control and sets X-Forwarded-For - without this every
        # client is keyed on the ALB's own IP and the per-client rate limiter is useless.
        { name = "RATE_LIMIT_TRUST_PROXY_HEADER", value = "true" },

        # The Dockerfile only installs the `mcp` extra, not `observability` - leaving
        # this true with no Langfuse keys/extra installed would silently no-op the
        # exporter and look configured when it isn't.
        { name = "LANGFUSE_ENABLED", value = "false" },

        { name = "MCP_HTTP_ENABLED", value = "true" },
        # A direct reference to the ALB's own DNS name. This is what resolves the
        # ordering problem in a single `terraform apply`: Terraform's dependency graph
        # creates the ALB before this task definition because the JSON literally
        # depends on aws_lb.this.dns_name. Without it, the MCP SDK's Host-header check
        # (localhost/127.0.0.1 only, by default) 421s every /mcp request once traffic
        # actually arrives via the ALB's hostname.
        # app/config.py's mcp_allowed_hosts is a list[str] - pydantic-settings decodes a
        # plain-string env var for a list field as JSON, not a bare/comma-separated
        # string (confirmed live: a bare hostname here crashes Settings() at import with
        # json.decoder.JSONDecodeError, taking the whole container down before uvicorn
        # ever starts - not just an /mcp-specific failure).
        { name = "MCP_ALLOWED_HOSTS", value = jsonencode([aws_lb.this.dns_name]) },
      ]

      secrets = [
        { name = "ANTHROPIC_API_KEY", valueFrom = aws_secretsmanager_secret.anthropic_api_key.arn },
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "app"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "app" {
  name            = "${var.project_name}-svc"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default_public.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener.http]
}
