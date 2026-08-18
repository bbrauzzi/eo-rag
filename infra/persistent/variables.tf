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
