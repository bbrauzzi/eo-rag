# Deploying eo-rag to AWS

Two Terraform stacks, on purpose:

- **`infra/persistent/`** — the S3 state bucket (created by hand) and the ECR repo.
  Created **once, ever**. Nothing here is destroyed between demos.
- **`infra/`** — RDS, the ALB, ECS (cluster/task/service), Secrets Manager, IAM. This is
  the **ephemeral** stack: `./deploy.sh` / `./undeploy.sh` create and destroy all of it,
  cheaply and often, without ever touching the pushed image.

Architecture: ECS Fargate (one task, no autoscaling) + RDS PostgreSQL/pgvector
(single-AZ) + an internet-facing ALB (HTTP only, no custom domain) + ECR, all in the
account's default VPC.

No auth sits in front of `/ask` — the app's own guardrails (`MAX_CONVERSATION_TURNS`,
`MAX_CONVERSATION_COST_USD`, `RATE_LIMIT_ASK_PER_MINUTE`) are what bound cost exposure.
Fine for a demo; add real auth before this is anything more than that.

## One-time setup (do this once, ever)

```bash
export PROFILE=<your-aws-cli-profile>
export REGION=<your-region>            # e.g. us-east-1
ACCOUNT_ID=$(aws sts get-caller-identity --profile $PROFILE --query Account --output text)

# Confirm a real, currently-supported engine version - there's no hardcoded default
aws rds describe-db-engine-versions --engine postgres --profile $PROFILE --region $REGION \
  --query "DBEngineVersions[?starts_with(EngineVersion,'16.')].EngineVersion" --output table
```

Also confirm in the Bedrock console (**Model access**) that `amazon.titan-embed-text-v2:0`
is enabled in `$REGION` — no reliable one-shot CLI enable for this; calls fail with
`AccessDeniedException` until it is.

**State bucket** (not Terraform-managed, shared by both stacks):

```bash
aws s3api create-bucket --bucket eo-rag-tfstate-${ACCOUNT_ID} --region $REGION --profile $PROFILE \
  $( [ "$REGION" != "us-east-1" ] && echo --create-bucket-configuration LocationConstraint=$REGION )
aws s3api put-bucket-versioning --bucket eo-rag-tfstate-${ACCOUNT_ID} \
  --versioning-configuration Status=Enabled --profile $PROFILE
aws s3api put-bucket-encryption --bucket eo-rag-tfstate-${ACCOUNT_ID} \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
  --profile $PROFILE
```

Edit the `backend "s3"` block in **both** `provider.tf` (root) and `persistent/provider.tf`:
replace `REPLACE_WITH_eo-rag-tfstate-<ACCOUNT_ID>` with `eo-rag-tfstate-${ACCOUNT_ID}` and
`REPLACE_WITH_REGION` with `$REGION` (literal values only — a backend block can't
reference a variable).

**Persistent stack** (ECR repo):

```bash
cd infra/persistent
terraform init
terraform apply -var="aws_profile=$PROFILE" -var="aws_region=$REGION"
ECR_URL=$(terraform output -raw ecr_repository_url)
```

**Build and push the image once:**

```bash
cd ../..
aws ecr get-login-password --profile $PROFILE --region $REGION | \
  docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com
docker build --platform linux/amd64 -t eo-rag:demo .
docker tag eo-rag:demo ${ECR_URL}:demo
docker push ${ECR_URL}:demo
```

`--platform linux/amd64` matters on non-x86 build hosts (e.g. Apple Silicon) — the task
definition stays X86_64. Only redo this build/push step later if you actually change the
app's code — the tag `:demo` is reused as-is by every `deploy.sh` cycle otherwise.

**Fill in your env file:**

```bash
cp infra/env.sh.example infra/env.sh
# edit infra/env.sh: aws_profile, aws_region, anthropic_api_key, ecr_repository_url
# (= $ECR_URL from above), image_tag=demo, db_engine_version (from the describe call above)
```

## The repeatable cycle (before/after an interview)

```bash
source infra/env.sh
./infra/deploy.sh      # terraform apply + wait for the service + re-ingest the STAC doc
#   ... share the printed http://<alb-dns-name> link, do the interview ...
./infra/undeploy.sh    # terraform destroy - RDS, ALB, ECS all gone, billing stops
```

That's it — two commands. `deploy.sh` prints the live URL and a couple of curl commands
to sanity-check it; `undeploy.sh` tears everything ephemeral down. Both scripts read
their config from the `TF_VAR_*` variables `env.sh` exports, so nothing needs to be typed
per run.

Three things worth knowing about this cycle, since it trades speed for statelessness:

- **RDS is destroyed and recreated every time** (`skip_final_snapshot = true`, on
  purpose — see `rds.tf`), so `deploy.sh` always re-runs ingestion. The doc is 65KB and
  ingestion takes seconds; RDS provisioning itself is the slow part of `deploy.sh`
  (several minutes) — budget more like 5–10 minutes end to end than "a few," especially
  the first `deploy.sh` after any gap.
- **The ALB's DNS name changes on every `deploy.sh`.** There's no static domain (see the
  Domain/TLS decision in the project's deployment plan), so re-share the link each time
  rather than bookmarking one.
- **The image doesn't rebuild.** `infra/persistent` and the `:demo` tag in ECR are
  untouched by `undeploy.sh`, which is the entire point of the stack split — without it,
  every cycle would also pay for a Docker build + push of a `rasterio`/GDAL image.

If you change app code between interviews, rebuild and push before your next
`deploy.sh` (repeat the "Build and push the image once" step above, keeping the same
`:demo` tag so `env.sh` doesn't need editing).

## Verify (also printed by deploy.sh)

```bash
DNS=$(terraform -chdir=infra output -raw alb_dns_name)   # while the ephemeral stack is up
curl -s http://$DNS/health
# {"status":"ok"}

curl -s -X POST http://$DNS/ask -H "Content-Type: application/json" \
  -d '{"question":"What is a STAC Item?"}' | jq .
# a real answer, sources citing "stac-spec.md", and a conversation_id
```

If `/ask` 500s: `aws logs tail /ecs/eo-rag --follow --profile $PROFILE --region $REGION`
— usual culprits are a bad `ANTHROPIC_API_KEY` in `env.sh` or Bedrock model access not
enabled in `$REGION`.

## Full teardown (including the persistent stack)

Only do this if you're done with the project entirely — it deletes the ECR repo and its
image, so the next deploy would need a full rebuild:

```bash
source infra/env.sh
./infra/undeploy.sh                 # ephemeral stack, if still up

cd infra/persistent
terraform destroy -var="aws_profile=$PROFILE" -var="aws_region=$REGION"

aws s3 rm s3://eo-rag-tfstate-${ACCOUNT_ID} --recursive --profile $PROFILE
aws s3api delete-bucket --bucket eo-rag-tfstate-${ACCOUNT_ID} --profile $PROFILE --region $REGION
```
