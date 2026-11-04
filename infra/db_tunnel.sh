#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Open localhost:<LOCAL_PORT> -> RDS:5432 via SSM Session Manager port
# forwarding through the development bastion, then optionally run a command
# against the DB while the tunnel is up (e.g. psql, alembic).
#
# Usage:
#   infra/db_tunnel.sh                # list tables + pg version, then close
#   infra/db_tunnel.sh --keep-open    # stay open; ctrl-c to close
#   infra/db_tunnel.sh -- village-local-expert "SELECT 1"   # custom SQL
#
# Requires: profile feederline-development, SSH via SSO not needed — the
# session rides the instance's SSM agent. The tunnel exists only while this
# script runs.
# ---------------------------------------------------------------------------
set -euo pipefail

AWS_PROFILE=feederline-development
AWS_REGION=eu-west-1
LOCAL_PORT=${LOCAL_PORT:-15432}
KEEP_OPEN=false
SQL=""

if [[ "${1:-}" == "--keep-open" ]]; then KEEP_OPEN=true; shift; fi
if [[ "${1:-}" == "--" ]]; then shift; SQL="$*"; fi

export AWS_PROFILE AWS_REGION

terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" init >/dev/null 2>&1 || true
DB_HOST=$(terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" output -json db_endpoint | jq -r . | cut -d: -f1)
BASTION_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=feederline-development-bastion" "Name=instance-state-name,Values=running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

[[ "$BASTION_ID" == "None" || -z "$BASTION_ID" || "$BASTION_ID" == "null" ]] && {
  echo "bastion is stopped — starting it (costs ~\$0.004/h while up)"
  INSTANCE_ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=feederline-development-bastion" \
      "Name=instance-state-name,Values=stopping,stopped,pending" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]] && { echo "no bastion instance found"; exit 1; }
  aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
  echo "waiting for instance + SSM agent to register (may take ~60s on first boot)"
  aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID"
  BASTION_ID="$INSTANCE_ID"
}

echo "tunnel: localhost:$LOCAL_PORT -> $DB_HOST:5432 (via $BASTION_ID)"

AWS_CONNECTION_PARAMETERS="--document-name AWS-StartPortForwardingSessionToRemoteHost \
 --parameters {\"host\":[\"$DB_HOST\"],\"portNumber\":[\"5432\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"

if $KEEP_OPEN; then
  exec aws ssm start-session --target "$BASTION_ID" $AWS_CONNECTION_PARAMETERS
fi

# One-shot mode: open tunnel detached (streams to a log so the plugin can't
# hold our stdout), run the verification, then reap the session dead-ends.
aws ssm start-session --target "$BASTION_ID" $AWS_CONNECTION_PARAMETERS \
  > /tmp/opencode/ssm_tunnel.log 2>&1 < /dev/null &
TUNNEL_PID=$!
trap 'pkill -P $TUNNEL_PID 2>/dev/null; kill $TUNNEL_PID 2>/dev/null' EXIT

for _ in $(seq 1 30); do
  python3 -c "import socket; socket.create_connection(('127.0.0.1', $LOCAL_PORT), 1).close()" 2>/dev/null && break
  sleep 2
done

# Credentials come straight from Secrets Manager, never typed; the host part
# only is swapped for the tunnel endpoint.
SECRET_ARN=$(terraform -chdir="$(dirname "${BASH_SOURCE[0]}")/platform" output -json db_secret_arn | jq -r .)
poetry run python - "$SECRET_ARN" "$LOCAL_PORT" "$SQL" <<'PYEOF'
import json
import sys

import boto3
import sqlalchemy as sa

secret_arn, local_port, sql = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
db_uri = json.loads(
    boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
)["DB_URI"]
# keep user:secret from the deployed secret verbatim; only the host:port is
# swapped for the tunnel endpoint
url = f"postgresql+psycopg://{db_uri.split('://')[1].split('@', 1)[0]}@127.0.0.1:{local_port}/feederline"

# RDS is provisioned with rds.force_ssl=1 — TLS is mandatory, so the client
# must not fall back to plaintext.
engine = sa.create_engine(url, connect_args={"connect_timeout": 10, "sslmode": "require"})
with engine.connect() as conn:
    version = conn.execute(sa.text("SELECT version()")).scalar_one()
    rows = conn.execute(sa.text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name")).scalars().all()
    print("connected to:", version.split(",")[0])
    print(f"public tables ({len(rows)}):")
    for t in rows:
        print("  ", t)
    if sql:
        print("result of:", sql)
        for row in conn.execute(sa.text(sql)):
            print("  ", row)
PYEOF

kill $TUNNEL_PID 2>/dev/null
