# AGENTS.md

FastAPI + PostgreSQL control plane for scheduling live video deliveries. One app, one
DB, one Alembic history. Reads like a small repo — the traps are in tooling, not size.

## Dev commands — Poetry

Poetry 1.8.2 does not read PEP 621 `[project]` metadata, so `pyproject.toml` is written
in Poetry's classic `[tool.poetry]` format. The normal commands work:

```bash
poetry install
poetry run alembic upgrade head
poetry run uvicorn app.main:app --reload
poetry run python -m app.outbox
poetry run pytest
```

- Single test: `poetry run pytest tests/test_outbox.py::test_name`
- Dependencies live in `pyproject.toml` (`[tool.poetry.dependencies]`), **not**
  `requirements.txt` — the latter is incomplete.
- `poetry.lock` is valid; run `poetry lock --no-update` only after changing dependencies.
- A plain `.venv` still works if you prefer not to use Poetry; install the same
  dependencies from `pyproject.toml`.

## Testing

- **Tests need no external services.** `tests/conftest.py` builds
  `sqlite+pysqlite:///:memory:` engines; the `client` / `test_session_factory` fixtures
  create and drop all tables per test. Ignore Docker/Postgres for test runs.
- Schema in tests comes from `Base.metadata.create_all`, **not** Alembic — migrations and
  ORM models can drift without tests catching it. Verify both when touching the schema.
- `tests/test_state_machine.py` parses `infra/direct_same_region.asl.json` and asserts the
  set of Lambda `action` names. If you add/rename an action in the ASL, you must update
  the matching dispatch in `app/workflows/direct_same_region.py` and this test.
- Full suite is fast and passes. There is no CI — `pytest` locally is the gate.

## Architecture

- Entrypoints:
  - API: `app.main:app` (run `uvicorn app.main:app --reload`)
  - Outbox worker: `python -m app.outbox` (polls due events, publishes, sleeps)
  - Step Functions Lambda: `app.workflows.direct_same_region.lambda_handler`
- `app/domains/` holds the execution resources (sources, destinations,
  stream_profiles, deliveries) as SQLAlchemy models + FastAPI routers in one file each,
  plus the P0 intent layer: `content.py`, `connections.py` (feeds/outputs), `routes.py`,
  `route_lifecycle.py`, `route_events.py`, `operator.py`, and `control_room.py`.
  Deliveries owns the state machine and the transactional outbox rows.
- P0 customer vocabulary lives on `/v1/content`, `/v1/feeds`, `/v1/outputs`,
  `/v1/routes`, `/v1/capabilities`, `/v1/quality-presets`, and `/v1/control-room`.
  Customer responses are **capability summaries**: they never expose
  `source_type`/`destination_type`, `config`, ARNs/provider ids, delivery ids or a
  resolved plan. Full plans, catalogue recipes and execution types live under
  `/internal/*` (`/internal/capabilities`, `/internal/routes/dry-run`,
  `/internal/plans/{route_id}`).
  The legacy `/v1/sources`, `/v1/destinations`, `/v1/stream-profiles` and
  `/v1/deliveries` routes remain for one release but now require the operator token.
- The capability catalog is code-defined in `app/capabilities/catalog.py` and served
  read-only. `GET /v1/capabilities` returns the public projection; the full projection
  (including `legacy_*`, `fixed_config`, recipes and preset profiles) is
  `/internal/capabilities`. `source_type`/`destination_type` are derived from a
  capability's `legacy_*` field; clients send `capability_id`.
- Event flow: `deliveries.py` writes outbox rows in the same transaction as state changes;
  `outbox.py` publishes them. `EVENT_PUBLISHER` selects the mode — `log` (default, local,
  no AWS), `eventbridge`, or `stepfunctions`. **Default is `log`**: nothing touches AWS
  unless you explicitly configure a mode + credentials.
- `infra/template.yaml` (SAM) + `infra/direct_same_region.asl.json` (state machine) are
  written but **never deployed**. Don't assume live AWS behavior matches them.

## Conventions & gotchas

- Brand is **Feederline** (renamed from Arqade Core). EventBridge source is
  `tv.feederline`; DB tables carry **no prefix** (`delivery`, `source`, `destination`,
  `stream_profile`, `delivery_outbox`, `delivery_workflow_run`). Constraint/index names
  follow the table (`ck_delivery_*`, `ix_delivery_*`). Don't reintroduce a `core_` prefix.
- The **directory is still named `arqade-core`**; only the contents say Feederline. This is
  intentional (user chose not to rename the working dir) — don't "fix" it.
- Migrations 0001–0003 were edited in place during the rename (revision `0001_initial`).
  There is no deployed DB, so `alembic upgrade head` from scratch is the whole story —
  don't write an upgrade-in-place migration for the old `core_*` names.
- `STEP_FUNCTIONS_MIGRATION.md` references `repositories/arqade-media-pipeline/`, a
  **separate external repo**. It's not a path in this tree — don't chase it.
- Known latent bug: `_record_failure` in `app/workflows/direct_same_region.py` uses
  `DeliveryOutboxEvent` but never imports it → `NameError` at runtime on the workflow
  failure path. Tests don't hit it (they never exercise `record_failure`). Fix only if
  asked; if you touch that function, the import is the one-liner.
- Customer endpoints are local-only and unauthenticated. Every `/internal/*` route
  requires the static `OPERATOR_TOKEN` (via `X-Operator-Token` or
  `Authorization: Bearer`); this is the P0 boundary, not RBAC. There is no default
  token: when `OPERATOR_TOKEN` is unset the boundary fails closed with 503
  ("Operator boundary is not configured").
- Infra deploy (when reached) is `sam build` / `sam deploy` per `STEP_FUNCTIONS_MIGRATION.md`;
  the worker then needs `EVENT_PUBLISHER=stepfunctions` + `WORKFLOW_STATE_MACHINE_ARN`.
