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
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "eo-rag"
      ManagedBy = "terraform"
    }
  }
}
