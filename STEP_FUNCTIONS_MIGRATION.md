# Airflow to Step Functions Standard

## Recommendation

Use AWS Step Functions Standard for the POC's media provisioning workflows. Keep delivery
scheduling in the database outbox and EventBridge; do not keep a Standard execution open for the
whole lifetime of an open-ended delivery. A `PipelineLink` or `PipelineUnlink` event starts a
workflow execution, while planned start/stop events remain lifecycle triggers.

## Current Airflow Responsibilities

The DAGs in `repositories/arqade-media-pipeline/app/dags` are provisioning and teardown workflows,
not data-processing pipelines. Media Pipeline supplies a `pipeline_plan`; DAG tasks resolve named
plan steps, call AWS through shared operators, wait for resource state, capture a resource map, and
signal success/failure through SQS. Link failures can trigger a separate unlink DAG as compensation.
The operators contain service-specific waiters and AWS request shaping; this behavior must move with
the workflows, not just the DAG graph.

| Family | Link behavior | Unlink behavior |
| --- | --- | --- |
| `direct_same_region` | Attach planned output or entitlement to an existing MediaConnect source flow. | Remove the output or revoke the entitlement. |
| `direct_same_region_redundant` | Attach outputs to primary and secondary source flows in parallel. | Remove both outputs; report based on both branches. |
| `hls_same_region` | Create MediaLive input and channel, wait for readiness, then start the channel. | Stop/delete the channel, then delete its input. |
| `dash_same_region` | Create MediaLive input, MediaPackage v2 channel and DASH endpoint, configure the channel, then start MediaLive. | Stop/delete MediaLive resources, then remove MediaPackage v2 resources. |
| `s3_cross_region_with_router` | Create a MediaLive router input, router output and link, activate the output, then create/start the channel. | Stop/delete the channel, stop/delete the router output, then delete its input. |

Redundant HLS/DASH/router DAGs add parallel resource branches and larger resource maps. Shared
workflow utilities implement plan lookup, success/failure branching, SQS notifications, and resource
map deserialization. AWS operators include bounded state waiters (for example, MediaLive waiters
use 60 attempts with a 3-second delay).

## POC Boundary Implemented

`app.outbox` now supports `EVENT_PUBLISHER=stepfunctions`. It preserves EventBridge publication for
all domain events. For `PipelineLink` and `PipelineUnlink`, it additionally calls
`StartExecution` on `WORKFLOW_STATE_MACHINE_ARN` with this input shape:

```json
{
  "schemaVersion": 1,
  "eventId": "<outbox UUID>",
  "eventType": "PipelineLink",
  "detail": {
    "eventId": "<outbox UUID>",
    "deliveryId": "<delivery UUID>"
  }
}
```

Execution names are `link-<eventId>` or `unlink-<eventId>`. This makes retries of an outbox row
idempotent; `ExecutionAlreadyExists` is treated as successful dispatch. Step Functions failures
other than that are left pending by the existing outbox retry path. The configured state machine
must be **Standard** and must branch on `eventType`.

The adapter publishes to EventBridge before starting the workflow. If the second call fails, an
outbox retry can publish the same domain event again; consumers must deduplicate using `eventId`.
This is at-least-once delivery, not a distributed transaction between EventBridge and Step Functions.

The POC now has the direct-flow state-machine definition, activity Lambda, resolver contract, and
workflow-run persistence. The SAM template has passed local validation but has not been deployed to
an AWS account, so live MediaConnect behavior and private RDS connectivity remain unverified. The
current direct pilot does not represent the other DAG families as migrated.

## Direct Pilot Contract

The POC direct pilot now defines this narrow resolver input:

- `Source.source_type` is `mediaconnectsource`, `Source.region` equals `Destination.region`, and
  `Source.config.flow_arn` is the existing MediaConnect flow ARN.
- `Destination.config.mediaconnect_output` contains one MediaConnect output request with
  `Protocol`, `Destination`, and `Port`; the resolver supplies a stable output name derived from
  the delivery UUID.
- Redundant deliveries are rejected. Entitlement grants and multi-output destinations are not
  included in this pilot.

The resolver freezes its output as `resolved_plan` on the `delivery_workflow_run` row. Each
`PipelineLink`/`PipelineUnlink` event gets a run row in the same DB transaction as the outbox event.
The dispatcher records the execution ARN/status. The link activity persists the exact flow/output
ARN map before lifecycle success; unlink reads that saved map and removes the exact output. Activity
retries discover an already-created output by its deterministic name.

## Deploying the Pilot

Apply the POC schema first, then build/deploy the SAM template. The database secret must be a URL
string or JSON containing `DB_URI` (or `database_url`). Lambda subnets need network paths to RDS and
Secrets Manager, normally via a Secrets Manager VPC endpoint, and to the MediaConnect control plane
through NAT or an available service endpoint. The worker needs `events:PutEvents` on its event bus
and `states:StartExecution` on the deployed state machine.

```bash
cd poc/feederline
DB_URI="$POC_DATABASE_URL" poetry run alembic upgrade head
sam build --template-file infra/template.yaml
sam deploy --guided --template-file .aws-sam/build/template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    DatabaseSecretArn="$DATABASE_SECRET_ARN" \
    PrivateSubnetIds="$PRIVATE_SUBNET_IDS" \
    LambdaSecurityGroupId="$LAMBDA_SECURITY_GROUP_ID"
```

Set the outbox worker's `EVENT_PUBLISHER=stepfunctions` and `WORKFLOW_STATE_MACHINE_ARN` to the
stack output. The SAM activity role currently grants the three required MediaConnect actions on
`*`; before production use, scope these permissions to the pilot account and approved regions.

## Remaining Rollout Work

The direct pilot now resolves a single output, polls the source flow up to 60 times at 10-second
intervals, attaches the output with a deterministic name, and persists its ARN before reporting
link success. Link failures run compensating removal and record both the original and compensation
failure in the execution error path. Unlink loads the link run's saved map and removes that output
before completing the delivery lifecycle.

Before migrating another family, validate this implementation against AWS with failure injection
after mutation, lost activity responses, repeated outbox delivery, stop during `STARTING`, and
cleanup failure. The next family should be router or DASH, each of which adds dependent resources
and stricter teardown ordering.

## Rollout Gates

- Extend resource contracts and activities for each additional pipeline family.
- Test every retry and compensation path with AWS clients stubbed, then inject failures in AWS.
- Scope pilot IAM permissions and add alarms for executions that fail before activity-level error
  recording.
- Add a durable recovery route for EventBridge or `StartExecution` dispatch failures; the current
  outbox worker retries but has no dead-letter or operator notification path.
- Validate the direct same-region lifecycle in the new AWS account, including duplicate events,
  partial failures, and cleanup.
- Only then migrate router or DASH and compare emitted lifecycle events/resource maps with the
  existing Airflow contract.

The POC now includes a deployable definition and tested direct same-region workflow slice, but
remains undeployed and is not yet a complete Airflow replacement or production media orchestrator.