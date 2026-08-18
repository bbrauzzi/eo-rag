#!/bin/bash
# Take the app offline: terraform destroy the ephemeral stack (RDS, ALB, ECS, secrets,
# IAM roles). The image in ECR and both Terraform states (infra/persistent) are
# untouched, so the next `deploy.sh` skips straight to `terraform apply` with no rebuild.
#
# -auto-approve is deliberate here: this script exists specifically to make destroy fast
# and one-command. RDS has skip_final_snapshot=true, so its data is genuinely gone after
# this runs - by design (see infra/rds.tf) - not a mistake to double check each time.
set -euo pipefail
cd "$(dirname "$0")"

: "${TF_VAR_aws_profile:?run: source infra/env.sh}"
: "${TF_VAR_aws_region:?run: source infra/env.sh}"
: "${TF_VAR_anthropic_api_key:?run: source infra/env.sh}"
: "${TF_VAR_ecr_repository_url:?run: source infra/env.sh}"
: "${TF_VAR_image_tag:?run: source infra/env.sh}"
: "${TF_VAR_db_engine_version:?run: source infra/env.sh}"

terraform destroy -auto-approve

echo "Torn down. ECR image and both Terraform states are untouched - next deploy.sh needs no rebuild."
