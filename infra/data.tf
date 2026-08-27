data "aws_caller_identity" "current" {}

# Deliberately the account's default VPC rather than a purpose-built one: see the
# NAT Gateway cost note in infra/README.md. Its public subnets host both the Fargate task
# (with a public IP, for egress to Bedrock/Anthropic/the STAC catalog) and RDS (with
# publicly_accessible = false, so it never actually gets a public IP despite sitting in
# a subnet that routes to an IGW).
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default_public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
