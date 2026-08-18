data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS itself uses to pull the image, write logs, and fetch secrets
# before the container ever starts. Distinct from the task role below on purpose - this
# one never runs application code.
resource "aws_iam_role" "ecs_task_execution" {
  name               = "${var.project_name}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_secrets" {
  statement {
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.anthropic_api_key.arn,
      aws_secretsmanager_secret.database_url.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_secrets.json
}

# Task role: what the application code itself assumes - this is how Bedrock embedding
# calls in app/rag/embeddings.py authenticate, via boto3's default credential chain,
# with no static AWS keys anywhere in the task definition.
resource "aws_iam_role" "ecs_task" {
  name               = "${var.project_name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    # Foundation-model ARNs are account-agnostic - scoped to exactly the embedding
    # model this app calls, nothing broader.
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/${var.embedding_model}"]
  }
}

resource "aws_iam_role_policy" "bedrock_invoke" {
  name   = "bedrock-invoke-titan-embed"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.bedrock_invoke.json
}
