# Infra — layout & operating flow

Two tools with explicit ownership boundaries (see the IaC decision):

| Directory | Tool | Owns | Managed where |
| --- | --- | --- | --- |
| `bootstrap/` | Terraform (local state) | TF state bucket + artifact lifecycle | applied once by hand |
| `platform/` | Terraform | VPC/subnets/NAT/SGs, RDS, DB secret, ECR + ECS cluster + IAM, EventBridge bus | remote state `s3://feederline-development-tfstate-<account>/platform/...` |
| `template.yaml` + `direct_same_region.asl.json` | SAM | Step Functions state machine + activity Lambda (the "pilot child stack") | plain CloudFormation stack `feederline-pilot-direct`, **not** in TF state |

Wiring contract (do not change names on either side):

- Terraform outputs → SAM parameters:
  `db_secret_arn → DatabaseSecretArn`, `private_subnet_ids → PrivateSubnetIds`,
  `lambda_security_group_id → LambdaSecurityGroupId`
- SAM outputs → worker/app env:
  `DirectMediaWorkflowArn → WORKFLOW_STATE_MACHINE_ARN`,
  EventBridge bus `tv.feederline → EVENT_BUS_NAME`,
  DB secret → `DB_SECRET_ARN` (read by `app/config.py`)

## Rules of engagement

1. Every AWS call uses `AWS_PROFILE=feederline-development`, region `eu-west-1`,
   account 716357364765. Bootstrap and platform provider blocks enforce this;
   `deploy_pilot.sh` exports it.
2. SAM builds/deployments must go through `infra/deploy_pilot.sh`, never a manual
   `sam deploy` — the script stages only `app/` into the Lambda zip (no `.venv`,
   no `.git`) and passes Terraform outputs as parameters.
3. IAM stays whole per side: task/execution roles in Terraform, activity and
   state-machine roles in SAM. Never split one role across both halves.
4. Pilot-grade RDS flags (`deletion_protection=false`, `skip_final_snapshot=true`,
   Single-AZ) are intentional for test-deploy cycles; flip them at production.

## Apply order

```bash
# once
terraform -chdir=infra/bootstrap init && terraform -chdir=infra/bootstrap apply

# after changing platform/
terraform -chdir=infra/platform init
terraform -chdir=infra/platform plan -out=/tmp/tfplan
terraform -chdir=infra/platform apply /tmp/tfplan

# after changing template.yaml / ASL / workflow code
infra/deploy_pilot.sh -y
```

## DB verification (no bastion SSH, no public IP)

The platform includes a sleeping SSM bastion (`infra/platform/bastion.tf`,
t4g.nano, `keep-stopped` tag — ~$0.7/mo idle, $0.004/h while in use). It is
reached via `infra/db_tunnel.sh`, which starts the instance if stopped, opens
an SSM port-forward tunnel (`localhost:15432 -> RDS:5432`), pulls credentials
from Secrets Manager and introspects the schema:

```bash
infra/db_tunnel.sh                 # one-shot: list tables + pg version
infra/db_tunnel.sh --keep-open     # hold the tunnel; ctrl-c to close
infra/db_tunnel.sh -- "SELECT 1"   # custom SQL in one-shot mode

# with the tunnel open, normal local tooling works against 127.0.0.1:15432
psql -h localhost -p 15432 -U feederline -d feederline
AWS_PROFILE=feederline-development \
DB_URI="postgresql+psycopg://feederline:<pw-set from secret>@127.0.0.1:15432/feederline?sslmode=require" \
  poetry run alembic upgrade head   # TLS is mandatory (rds.force_ssl=1)
```

Stop the instance after use (`aws ec2 stop-instances --instance-ids <id>`); it
bills while running. Credentials never appear in the scripts — they are pulled
from the DB secret at run time.

## Teardown (development account, test cycles)

```bash
aws cloudformation delete-stack --stack-name feederline-pilot-direct
terraform -chdir=infra/platform apply -destroy -auto-approve
terraform -chdir=infra/bootstrap destroy
```
(RDS deletes without a final snapshot per the pilot flags; the secret has
`recovery_window_in_days=0`.)
