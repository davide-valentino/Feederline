#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# End-to-end smoke against the deployed ECS app tier — no ALB: the API is
# reached through an SSM port-forward tunnel via the bastion to the api task
# ENI (same pattern as db_tunnel.sh).
#
#   infra/app_smoke.sh                 # health probes + create delivery +
#                                      # start it (queues PipelineLink)
#
# Operator credentials are pulled from Secrets Manager at runtime, never
# printed. Prints: delivery id, delivery state, outbox hint.
# ---------------------------------------------------------------------------
set -euo pipefail

AWS_PROFILE=feederline-development
AWS_REGION=eu-west-1
LOCAL_PORT=${LOCAL_PORT:-18000}
TERRAFORM_DIR="$(dirname "${BASH_SOURCE[0]}")/platform"

export AWS_PROFILE AWS_REGION

terraform -chdir="$TERRAFORM_DIR" init >/dev/null 2>&1 || true
CLUSTER=$(terraform -chdir="$TERRAFORM_DIR" output -json ecs_cluster | jq -r .)

# --- resolve the running api task ENI -----------------------------------------
TASK_ARN=$(aws ecs list-tasks --cluster "$CLUSTER" \
  --service-name "${CLUSTER}-api" --query 'taskArns[0]' --output text)
[[ "$TASK_ARN" == "None" ]] && { echo "no running api task"; exit 1; }

API_IP=$(aws ecs describe-tasks --cluster "$CLUSTER" --task "$TASK_ARN" \
  --query 'tasks[0].containers[0].networkInterfaces[0].privateIpv4Address' \
  --output text)
[[ "$API_IP" == "None" || -z "$API_IP" ]] && { echo "task has no ENI yet"; exit 1; }

# --- bastion: start if stopped -------------------------------------------------
BASTION_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${CLUSTER}-bastion" \
  "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
if [[ "$BASTION_ID" == "None" || -z "$BASTION_ID" || "$BASTION_ID" == "null" ]]; then
  BASTION_ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=${CLUSTER}-bastion" \
    "Name=instance-state-name,Values=stopped" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  echo "starting bastion $BASTION_ID (billed while running)"
  aws ec2 start-instances --instance-ids "$BASTION_ID" >/dev/null
  aws ec2 wait instance-status-ok --instance-ids "$BASTION_ID"
  sleep 15 # SSM agent registration on first boot
fi

# --- open the tunnel (detached; log to /tmp) -----------------------------------
echo "tunnel: localhost:$LOCAL_PORT -> $API_IP:8000 (via $BASTION_ID)"
aws ssm start-session --target "$BASTION_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$API_IP\"],\"portNumber\":[\"8000\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
  > /tmp/opencode/ssm_app_tunnel.log 2>&1 < /dev/null &
TUNNEL_PID=$!
trap 'pkill -P $TUNNEL_PID 2>/dev/null; kill $TUNNEL_PID 2>/dev/null' EXIT

for _ in $(seq 1 30); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $LOCAL_PORT), 1).close()" 2>/dev/null && break
  sleep 2
done

# --- probes ---------------------------------------------------------------------
poetry run python - "$LOCAL_PORT" "$CLUSTER" <<'PYEOF'
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import boto3
import httpx

local_port, cluster = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{local_port}"
suffix = time.strftime("%H%M%S")

operator_token = boto3.client("secretsmanager").get_secret_value(
    SecretId=f"feederline/development/operator-token"
)["SecretString"]
headers = {"X-Operator-Token": operator_token}

client = httpx.Client(base_url=base, headers=headers, timeout=30)

health = client.get("/healthz")
print("healthz:", health.status_code, health.json())
ready = client.get("/readyz")
print("readyz:", ready.status_code, ready.json())
assert ready.status_code == 200, "DB connectivity failed (readyz)"

profile = client.post("/v1/stream-profiles", json={
    "name": f"smoke-profile-{suffix}", "profile_type": "hd_1080p30", "config": {},
})
print("profile:", profile.status_code)
profile.raise_for_status()

source = client.post("/v1/sources", json={
    "name": f"smoke-source-{suffix}", "source_type": "ipsource",
    "region": "eu-west-1", "stream_profile_id": profile.json()["id"],
})
print("source:", source.status_code)
source.raise_for_status()

destination = client.post("/v1/destinations", json={
    "name": f"smoke-destination-{suffix}",
    "destination_type": "hlsdestination", "region": "eu-west-1",
})
print("destination:", destination.status_code)
destination.raise_for_status()

delivery = client.post("/v1/deliveries", json={
    "source_id": source.json()["id"],
    "destination_id": destination.json()["id"],
    "planned_start": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    "is_open_ended": True,
})
print("delivery:", delivery.status_code)
delivery.raise_for_status()
delivery_id = delivery.json()["id"]

started = client.post(f"/internal/deliveries/{delivery_id}/start")
print("delivery start:", started.status_code, "state:", started.json().get("state"))
started.raise_for_status()
print(f"\nSMOKE delivery_id={delivery_id}")
print("next: check outbox/workflow run rows via infra/db_tunnel.sh")
PYEOF

kill $TUNNEL_PID 2>/dev/null
