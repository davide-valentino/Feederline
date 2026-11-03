# Feederline POC

Standalone backend proof of concept for the modular Feederline platform. It has one FastAPI
application, one PostgreSQL database, and one Alembic migration history. Domain
modules cover the P0 customer intent layer (content, feeds, outputs, routes,
capabilities) plus the existing execution kernel (sources, destinations, stream
profiles, deliveries).

Creating a route validates the capability catalog, checks region availability and output
contention, derives a read-only execution plan, and fans out to one delivery per output.
The delivery kernel still writes its start/stop events atomically to a transactional
outbox; the worker processes them when due. Local mode logs them; it does not provision
media resources or trigger MWAA. AWS provisioning and media execution workflows are not
ported yet. Customer endpoints are local-only and unauthenticated; every `/internal/*`
route requires the static `OPERATOR_TOKEN`. A future AWS deployment must add private
ingress and machine authentication before it is reachable.

Distribution contributions, packages, entitlements, users, organisations, UI, pricing,
billing, and reporting are outside this repository's scope. MediaPackage v2 remains a
planned media-execution capability; it is not the Distribution packaging model.

## Behavior changes (P0)

- Every `/internal/*` route now requires the static `OPERATOR_TOKEN`; requests without a
  valid `X-Operator-Token` (or `Authorization: Bearer`) return 401.
- The legacy `PATCH /v1/deliveries/{id}/cancel` and `POST /internal/deliveries/{id}/stop`
  still reject `STOPPING`, `COMPLETE`, `ERROR` and `CANCELLED` deliveries with 409, as
  before. `POST /v1/routes/{id}/cancel|stop` stay idempotent through the shared stop
  helper.
- Route sync maps a route whose deliveries are all `CANCELLED` to observed `completed`,
  desired `cancelled` and one `route.cancelled` event. Natural completion still yields
  desired `stopped` and `route.completed`.

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

P0 customer surface:

- `POST`, `GET /v1/content`; `GET`, `PATCH`, `DELETE /v1/content/{id}`
- `POST`, `GET /v1/feeds`; `GET`, `PATCH`, `DELETE /v1/feeds/{id}`
- `POST`, `GET /v1/outputs`; `GET`, `PATCH`, `DELETE /v1/outputs/{id}`
- `GET /v1/capabilities`, `GET /v1/capabilities/{id}`, `GET /v1/quality-presets`
- `POST /v1/routes/dry-run`, `POST /v1/routes`, `GET /v1/routes`,
  `GET /v1/routes/{id}`, `POST /v1/routes/{id}/cancel|stop`,
  `GET /v1/routes/{id}/events|plan`
- `GET /v1/control-room/summary|activity|upcoming`

Customer reads are capability summaries: feeds/outputs return `capability` + `details`
(no execution types, provider identifiers or raw config), routes omit `resolved_plan`,
`plan_version` and `delivery_ids`, and the dry-run returns a `plan_summary` rather than
the full plan.

Legacy execution surface (kept for one release, now **operator-only**):

- `POST`, `GET /v1/sources`; `GET`, `PATCH`, `DELETE /v1/sources/{id}`
- `POST`, `GET /v1/destinations`; `GET`, `PATCH`, `DELETE /v1/destinations/{id}`
- `POST`, `GET /v1/stream-profiles`; `GET`, `PATCH`, `DELETE /v1/stream-profiles/{id}`
- `POST`, `GET /v1/deliveries` and `GET /v1/deliveries/{id}`
- `PATCH /v1/deliveries/{id}/cancel` and `DELETE /v1/deliveries/{id}`
- Pipeline lifecycle callbacks under `/v1/deliveries/{id}/pipeline-*`

Operator surface (requires `OPERATOR_TOKEN`):

- `GET /internal/deliveries`, `GET /internal/deliveries/{id}`
- `POST /internal/deliveries/{id}/retry|start|stop`
- `POST /internal/outbox/{id}/retry`, `GET /internal/outbox`
- `GET /internal/capabilities[/{id}]`, `GET /internal/quality-presets`
- `POST /internal/routes/dry-run` (full plan)
- `GET /internal/plans/{route_id}`, `GET /internal/audit`,
  `GET /internal/route-events`

```bash
curl -H "X-Operator-Token: $OPERATOR_TOKEN" http://127.0.0.1:8000/internal/deliveries
```

A route request carries a content id, a primary (and optional redundant) feed id,
`output_ids` (plus optional aligned `redundant_output_ids` for redundant/geo-redundant
routes), a quality preset, a resilience level and a schedule. The dry-run is authoritative:
`POST /v1/routes` re-runs the same checks and refuses to schedule an infeasible route.

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
