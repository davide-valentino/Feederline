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

## Teardown (development account, test cycles)

```bash
aws cloudformation delete-stack --stack-name feederline-pilot-direct
terraform -chdir=infra/platform apply -destroy -auto-approve
terraform -chdir=infra/bootstrap destroy
```
(RDS deletes without a final snapshot per the pilot flags; the secret has
`recovery_window_in_days=0`.)
