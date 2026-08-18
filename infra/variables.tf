variable "aws_profile" {
  description = "AWS CLI profile to deploy with"
  type        = string
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for every resource name/tag this stack creates"
  type        = string
  default     = "eo-rag"
}

variable "image_tag" {
  description = "ECR image tag the ECS task definition should run"
  type        = string
}

variable "ecr_repository_url" {
  description = "Output of infra/persistent (terraform -chdir=infra/persistent output -raw ecr_repository_url) - this stack never creates its own ECR repo, so an image survives every destroy/apply cycle"
  type        = string
}

# Secret. Pass via TF_VAR_anthropic_api_key, never -var, so it never lands in shell
# history or a process list.
variable "anthropic_api_key" {
  description = "Anthropic API key, stored in Secrets Manager and injected into the task"
  type        = string
  sensitive   = true
}

variable "claude_model" {
  description = "Matches app/config.py's default; override only to pin a different model"
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "embedding_model" {
  description = "Bedrock Titan embedding model id - also used to scope the task role's bedrock:InvokeModel resource"
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dim" {
  description = "Must match the dimension the doc_chunks table's vector column was created with"
  type        = number
  default     = 1024
}

# db.t4g.micro / 20GB / single-AZ: the "small/cheap, single instance" tier this stack is
# built for. Bump these, not the architecture, if traffic grows.
variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

# No default on purpose - confirm a real supported 16.x version with
# `aws rds describe-db-engine-versions` (see the deploy runbook's Step 0) before applying,
# rather than hardcoding one that may not exist in the target region.
variable "db_engine_version" {
  description = "RDS PostgreSQL engine version, e.g. 16.4 - confirm availability first"
  type        = string
}

variable "db_name" {
  type    = string
  default = "eorag"
}

variable "db_username" {
  type    = string
  default = "eorag"
}

variable "container_port" {
  type    = number
  default = 8000
}

# 512 CPU units / 1024 MiB = 0.5 vCPU / 1GB, the smallest Fargate combination that
# comfortably runs uvicorn + the LangGraph agent + rasterio for compute_index.
variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "log_retention_days" {
  type    = number
  default = 14
}
