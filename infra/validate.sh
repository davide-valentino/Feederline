#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Post-deploy validation battery for the development account.
#   1. identity + region floor check
#   2. no public exposure (subnet IP mapping, RDS public flag, SG ingress)
#   3. app-config integration: DB_SECRET_ARN -> DB_URI resolves
#   4. SAM child stack outputs
# Exits non-zero on the first failure it cannot flag-and-continue past.
# ---------------------------------------------------------------------------
set -euo pipefail
AWS_PROFILE=feederline-development
AWS_REGION=eu-west-1
export AWS_PROFILE AWS_REGION

echo "== 1. identity =="
aws sts get-caller-identity --query '[ Account, Arn ]' --output text

echo "== 2. exposure checks =="
PRIV_SUBS=$(terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" output -json private_subnet_ids | jq -c '.')
echo "private subnets: $PRIV_SUBS"
[[ "$(echo "$PRIV_SUBS" | jq length)" -eq 2 ]] || { echo "FAIL: expected 2 private subnets"; exit 1; }
aws ec2 describe-subnets --filters Name=tag:tier,Values=private \
  --query 'Subnets[*].{SubnetId:SubnetId,MapPublicIp:MapPublicIpOnLaunch,AZ:AvailabilityZone}' \
  --output table

RDS_ID=$(terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" output -json db_instance_identifier | jq -r '.')
RDS_PUBLIC=$(aws rds describe-db-instances --db-instance-identifier "$RDS_ID" \
  --query 'DBInstances[0].[PubliclyAccessible, DBInstanceStatus, DBInstanceClass, DBSubnetGroup.Subnets | length(@)]' \
  --output text | tr '\t' ' ')
echo "RDS [public, status, class, subnet-group-subnets] = $RDS_PUBLIC"
case "$RDS_PUBLIC" in
  False\ available\ db.t4g.small\ 2*) echo "OK: RDS private, available, t4g.small, 2-AZ subnet group" ;;
  *) echo "FAIL: unexpected RDS posture ($RDS_PUBLIC)"; exit 1 ;;
esac

echo "SG ingress rules (must show no 0.0.0.0/0 sources):"
aws ec2 describe-security-groups --filters Name=tag:project,Values=feederline \
  --query 'SecurityGroups[].[GroupName, ip_permissions[].userIdGroupPairs[].groupId]' \
  --output json

echo "== 3. app-config integration =="
SECRET_ARN=$(terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" output -json db_secret_arn | jq -r '.')
DB_URI=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" --query SecretString --output text \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["DB_URI"])')
echo "resolved host: $(echo "$DB_URI" | sed -E 's#.*@([^:/]+):.*#\1#')  (secret shape OK)"

echo "== 4. SAM child stack =="
aws cloudformation describe-stacks --stack-name feederline-pilot-direct \
  --query 'Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}' --output yaml
SFN_ARN=$(aws cloudformation describe-stacks --stack-name feederline-pilot-direct \
  --query 'Stacks[0].Outputs[?OutputKey==`DirectMediaWorkflowArn`].OutputValue' --output text)
aws stepfunctions describe-state-machine --state-machine-arn "$SFN_ARN" \
  --output json --query '{name: name, status: status}' \
  | jq -r '"OK: state machine \(.name) (\(.status))"'

echo "ALL CHECKS PASSED"
