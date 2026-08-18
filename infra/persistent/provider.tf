# This stack holds the two things that should survive every deploy/destroy cycle of the
# ephemeral stack in infra/: the ECR repo (so a redeploy never has to rebuild the image)
# and, implicitly, the S3 state bucket both stacks share (created by hand, see README).
# Same replace-the-placeholders rule as infra/provider.tf - a backend block only accepts
# literals.
terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  backend "s3" {
    bucket = "REPLACE_WITH_eo-rag-tfstate-<ACCOUNT_ID>"
    key    = "eo-rag/persistent.tfstate"
    region = "REPLACE_WITH_REGION"
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "eo-rag"
      ManagedBy = "terraform"
      Stack     = "persistent"
    }
  }
}
