#!/bin/bash
# Bring the app online: terraform apply the ephemeral stack, wait for the ECS service
# to stabilize, then re-run ingestion since RDS is created fresh every cycle (nothing
# persists across a destroy). Run `source infra/env.sh` first - see env.sh.example.
set -euo pipefail
cd "$(dirname "$0")"

: "${TF_VAR_aws_profile:?run: source infra/env.sh}"
: "${TF_VAR_aws_region:?run: source infra/env.sh}"
: "${TF_VAR_anthropic_api_key:?run: source infra/env.sh}"
: "${TF_VAR_ecr_repository_url:?run: source infra/env.sh}"
: "${TF_VAR_image_tag:?run: source infra/env.sh}"
: "${TF_VAR_db_engine_version:?run: source infra/env.sh}"

terraform init -input=false
terraform apply -auto-approve

CLUSTER=$(terraform output -raw ecs_cluster_name)
SERVICE=$(terraform output -raw ecs_service_name)
SUBNETS=$(terraform output -json public_subnet_ids | jq -r 'join(",")')
SG_TASK=$(terraform output -raw task_security_group_id)
DNS=$(terraform output -raw alb_dns_name)

echo "Waiting for the ECS service to stabilize..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" \
  --profile "$TF_VAR_aws_profile" --region "$TF_VAR_aws_region"

TASKDEF_ARN=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --profile "$TF_VAR_aws_profile" --region "$TF_VAR_aws_region" \
  --query "services[0].taskDefinition" --output text)

echo "Ingesting the STAC spec doc (RDS is empty on a fresh instance)..."
RUN_TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" --task-definition "$TASKDEF_ARN" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SG_TASK}],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"app","command":["python","-m","app.rag.ingest","data/stac-spec-core.md","--source","stac-spec.md"]}]}' \
  --profile "$TF_VAR_aws_profile" --region "$TF_VAR_aws_region" --query "tasks[0].taskArn" --output text)

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$RUN_TASK_ARN" \
  --profile "$TF_VAR_aws_profile" --region "$TF_VAR_aws_region"

EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$RUN_TASK_ARN" \
  --profile "$TF_VAR_aws_profile" --region "$TF_VAR_aws_region" \
  --query "tasks[0].containers[0].exitCode" --output text)

if [ "$EXIT_CODE" != "0" ]; then
  echo "Ingestion failed (exit code $EXIT_CODE)." >&2
  echo "Check: aws logs tail /ecs/eo-rag --profile $TF_VAR_aws_profile --region $TF_VAR_aws_region" >&2
  exit 1
fi

echo ""
echo "Live: http://$DNS"
echo "  curl http://$DNS/health"
echo "  curl -X POST http://$DNS/ask -H 'Content-Type: application/json' -d '{\"question\":\"What is a STAC Item?\"}'"
