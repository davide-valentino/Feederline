#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build and deploy the SAM pilot child stack (Step Functions + activity Lambda)
# using Terraform platform outputs, strictly on the feederline-development
# profile. The stack stays a plain CloudFormation stack managed by SAM (not
# imported into Terraform state) per the infra split: TF owns the platform,
# SAM owns the pilot app.
#
# Usage:  infra/deploy_pilot.sh [-y]
#   -y  skip changeset confirmation (for CI / scripted runs)
# ---------------------------------------------------------------------------
set -euo pipefail

AWS_PROFILE=feederline-development
AWS_REGION=eu-west-1
STACK_NAME=feederline-pilot-direct
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIRM=true
[[ "${1:-}" == "-y" ]] && CONFIRM=false

export AWS_PROFILE AWS_REGION

# --- 1. Platform outputs -----------------------------------------------------
STATUS="$(terraform -chdir="$REPO_ROOT/infra/platform" output -json 2>/dev/null)" || {
  echo "error: terraform -chdir infra/platform output failed — run terraform apply first" >&2
  exit 1
}
DATABASE_SECRET_ARN="$(echo "$STATUS" | jq -r '.db_secret_arn.value')"
PRIVATE_SUBNET_IDS="$(echo "$STATUS" | jq -r '[.private_subnet_ids.value[]] | join(",")')"
LAMBDA_SECURITY_GROUP_ID="$(echo "$STATUS" | jq -r '.lambda_security_group_id.value')"
TFSTATE_BUCKET="$(echo "$STATUS" | jq -r '.tfstate_bucket // empty')"

# tfstate bucket lives in the bootstrap stack; look it up by naming convention.
if [[ -z "$TFSTATE_BUCKET" ]] || [[ "$TFSTATE_BUCKET" == "null" ]]; then
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  TFSTATE_BUCKET="feederline-development-tfstate-${ACCOUNT_ID}"
fi

[[ "$DATABASE_SECRET_ARN" == "null" || -z "$DATABASE_SECRET_ARN" ]] && { echo "error: db_secret_arn output missing" >&2; exit 1; }
[[ "$LAMBDA_SECURITY_GROUP_ID" == "null" || -z "$LAMBDA_SECURITY_GROUP_ID" ]] && { echo "error: lambda_security_group_id output missing" >&2; exit 1; }
SUB_COUNT=$(echo "$PRIVATE_SUBNET_IDS" | awk -F, '{print NF}')
[[ "$SUB_COUNT" -lt 2 ]] && { echo "error: expected 2 private subnets, got '$PRIVATE_SUBNET_IDS'" >&2; exit 1; }

echo "==> target: account $(aws sts get-caller-identity --query Account --output text) / $AWS_REGION"
echo "==> secret: $DATABASE_SECRET_ARN"
echo "==> subnets: $PRIVATE_SUBNET_IDS"
echo "==> lambda SG: $LAMBDA_SECURITY_GROUP_ID"

# --- 2. Stage + build --------------------------------------------------------
# Build from a staging copy so the Lambda zip gets app/ only (no .venv, .git).
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$REPO_ROOT/app" "$STAGE/app"
mkdir -p "$STAGE/infra"
cp "$REPO_ROOT/infra/template.yaml" "$REPO_ROOT/infra/direct_same_region.asl.json" "$STAGE/infra/"

echo "==> sam build"
# --build-dir must sit outside the CodeUri root or CopySource recurses into itself
SAM_BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$SAM_BUILD_DIR"' EXIT
sam build --template-file "$STAGE/infra/template.yaml" --base-dir "$STAGE/infra" --build-dir "$SAM_BUILD_DIR"

echo "==> sam package (bucket: $TFSTATE_BUCKET, key prefix: pilot/)"
sam package \
  --template-file "$SAM_BUILD_DIR/template.yaml" \
  --s3-bucket "$TFSTATE_BUCKET" \
  --s3-prefix pilot \
  --output-template-file "$STAGE/packaged.yaml" >/dev/null

# --- 3. Deploy ----------------------------------------------------------------
if $CONFIRM; then
  echo "==> about to deploy stack '$STACK_NAME' — press enter to continue, ctrl-c to abort"
  read -r
fi

sam deploy \
  --template-file "$STAGE/packaged.yaml" \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE" \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --resolve-s3 \
  --parameter-overrides \
    "DatabaseSecretArn=$DATABASE_SECRET_ARN" \
    "PrivateSubnetIds=$PRIVATE_SUBNET_IDS" \
    "LambdaSecurityGroupId=$LAMBDA_SECURITY_GROUP_ID" \
    "StateMachineName=feederline-direct-media"

# --- 4. Hand outputs back for the app/worker env ------------------------------
echo "==> stack outputs (WORKFLOW_STATE_MACHINE_ARN etc. for .env)"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}' \
  --output yaml
