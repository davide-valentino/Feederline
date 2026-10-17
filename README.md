# Feederline POC

Standalone backend proof of concept for the modular Feederline platform. It has one FastAPI
application, one PostgreSQL database, and one Alembic migration history. Domain
modules currently cover sources, destinations, stream profiles, and delivery intent.

This is an early implementation, not yet a production replacement. Creating a delivery
validates local references, rejects overlapping active schedules on a shared destination,
and atomically records start/stop events in a transactional outbox. The worker processes
events when due. Local mode logs them; it does not provision media resources or trigger
MWAA. Source/destination type-specific validation, AWS provisioning, and media execution
workflows are not ported yet. The API and internal lifecycle routes have no authentication
and must only be used locally. A future AWS deployment must add private ingress and
machine authentication before it is reachable.

Distribution contributions, packages, entitlements, users, organisations, UI, pricing,
billing, and reporting are outside this repository's scope. MediaPackage v2 remains a
planned media-execution capability; it is not the Distribution packaging model.

## Local setup

Requirements: Python 3.12, Poetry, and Docker Compose.

```bash
cp .env.example .env
docker compose up -d db
poetry install
poetry run alembic upgrade head
poetry run uvicorn app.main:app --reload
```

In another terminal, start the outbox worker:

```bash
cd poc/feederline
poetry run python -m app.outbox
```

The default `EVENT_PUBLISHER=log` is safe for local use. To publish into a configured
AWS EventBridge bus, set `EVENT_PUBLISHER=eventbridge`, `EVENT_BUS_NAME`, and `AWS_REGION`
in `.env`, and make sure AWS credentials and IAM permissions are configured. The emitted
events use source `tv.feederline` and include `schemaVersion: 1`; they are not routed to
the legacy services by default.

The API is available at `http://127.0.0.1:8000`; interactive docs are at
`http://127.0.0.1:8000/docs`. `/healthz` is process health and `/readyz` checks the
database connection.

Run the isolated API tests with:

```bash
poetry run pytest
```

## Current API surface

- `POST`, `GET /v1/sources`; `GET`, `PATCH`, `DELETE /v1/sources/{id}`
- `POST`, `GET /v1/destinations`; `GET`, `PATCH`, `DELETE /v1/destinations/{id}`
- `POST`, `GET /v1/stream-profiles`; `GET`, `PATCH`, `DELETE /v1/stream-profiles/{id}`
- `POST`, `GET /v1/deliveries` and `GET /v1/deliveries/{id}`
- `PATCH /v1/deliveries/{id}/cancel` and `DELETE /v1/deliveries/{id}`
- Internal start/stop and pipeline lifecycle callbacks under `/internal/deliveries` and
	`/v1/deliveries/{id}/pipeline-*`

Delivery requests carry source and destination IDs and schedule fields directly; they
do not require contribution, package, user, or organisation records. The outbox adapter
supports local logging, EventBridge publishing, and a Step Functions Standard pilot mode.
In pilot mode, all domain events are still published to EventBridge and `PipelineLink` /
`PipelineUnlink` also start a Standard workflow. The direct same-region state machine, activity
Lambda, resolver contract, and workflow-run persistence are implemented under `infra/`; they have
not been deployed to AWS. See [STEP_FUNCTIONS_MIGRATION.md](STEP_FUNCTIONS_MIGRATION.md) for the
current Airflow behavior map, source/destination config contract, and deployment steps.

To exercise the publisher against an already-deployed Standard state machine, set
`EVENT_PUBLISHER=stepfunctions`, `EVENT_BUS_NAME`, `WORKFLOW_STATE_MACHINE_ARN`, and
`AWS_REGION`. The worker role needs `events:PutEvents` on the configured bus and
`states:StartExecution` on the state machine. Keep `EVENT_PUBLISHER=log` for local-only runs.
