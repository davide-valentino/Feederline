# AGENTS.md

FastAPI + PostgreSQL control plane for scheduling live video deliveries. One app, one
DB, one Alembic history. Reads like a small repo — the traps are in tooling, not size.

## Dev commands — Poetry is broken, do not use it

`pyproject.toml` declares deps via PEP 621 `[project]`, but the installed Poetry is
1.8.2, which only reads `[tool.poetry]`. So `poetry install` and `poetry run pytest`
— the commands `README.md` tells you to run — **fail** with:

```
The Poetry configuration is invalid:
  - The fields ['authors', 'description', 'name', 'version'] are required in package mode.
```

Do not try to fix this by adding `[tool.poetry]` fields unless asked; it changes
packaging semantics. Instead bypass Poetry with a plain venv:

```bash
python3 -m venv .venv
.venv/bin/pip install "alembic>=1.14,<2.0" "boto3>=1.35,<2.0" "fastapi>=0.115,<1.0" \
  "psycopg[binary]>=3.2,<4.0" "python-dotenv>=1.0,<2.0" "sqlalchemy>=2.0,<3.0" \
  "uvicorn[standard]>=0.32,<1.0" "httpx>=0.27,<1.0" "pytest>=8.0,<9.0"
.venv/bin/python -m pytest
```

- Single test: `.venv/bin/python -m pytest tests/test_outbox.py::test_name`
- Deps come from `pyproject.toml`, **not** `requirements.txt` — the latter is incomplete
  (missing fastapi, alembic, uvicorn, and all dev deps). Trust `pyproject.toml`.
- `poetry.lock` exists but is unusable while Poetry can't parse the manifest.

## Testing

- **Tests need no external services.** `tests/conftest.py` builds
  `sqlite+pysqlite:///:memory:` engines; the `client` / `test_session_factory` fixtures
  create and drop all tables per test. Ignore Docker/Postgres for test runs.
- Schema in tests comes from `Base.metadata.create_all`, **not** Alembic — migrations and
  ORM models can drift without tests catching it. Verify both when touching the schema.
- `tests/test_state_machine.py` parses `infra/direct_same_region.asl.json` and asserts the
  set of Lambda `action` names. If you add/rename an action in the ASL, you must update
  the matching dispatch in `app/workflows/direct_same_region.py` and this test.
- Full suite is fast (<1s) and all 23 pass. There is no CI — `pytest` locally is the gate.

## Architecture

- Entrypoints:
  - API: `app.main:app` (run `uvicorn app.main:app --reload`)
  - Outbox worker: `python -m app.outbox` (polls due events, publishes, sleeps)
  - Step Functions Lambda: `app.workflows.direct_same_region.lambda_handler`
- `app/domains/` holds the four resources (sources, destinations, stream_profiles,
  deliveries) as SQLAlchemy models + FastAPI routers in one file each. Deliveries owns the
  state machine and the transactional outbox rows.
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
- The API has **no auth** and is local-only. Any auth/ingress assumption is out of scope.
- Infra deploy (when reached) is `sam build` / `sam deploy` per `STEP_FUNCTIONS_MIGRATION.md`;
  the worker then needs `EVENT_PUBLISHER=stepfunctions` + `WORKFLOW_STATE_MACHINE_ARN`.
