# State lives in S3, not local disk - see the "Terraform state" note in the deploy
# runbook. The bucket itself is created by hand (`aws s3api create-bucket ...`) before
# `terraform init`, because a backend block cannot reference a resource Terraform itself
# would manage. Replace the two placeholders below with the values from that step; a
# backend block only accepts literals, not variables or interpolation.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  backend "s3" {
    bucket = "REPLACE_WITH_eo-rag-tfstate-<ACCOUNT_ID>"
    key    = "eo-rag/terraform.tfstate"
    region = "REPLACE_WITH_REGION"
  }
}

provider "aws" {
  region = var.aws_region
  # null (not "") is what tells the provider "don't force profile-based resolution" -
  # an explicit profile takes priority over AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env
  # vars, which breaks any credential source that only sets those (e.g. a broker whose
  # `aws` CLI understands but the Go SDK doesn't, flattened via
  # `aws configure export-credentials`). Leave aws_profile empty to defer to the
  # standard env/instance-role chain instead.
  profile = var.aws_profile != "" ? var.aws_profile : null

  default_tags {
    tags = {
      Project   = "eo-rag"
      ManagedBy = "terraform"
    }
  }
}
