# Feederline control plane image: api (uvicorn app.main:app) and outbox
# worker (python -m app.outbox) share one image — the command is chosen at
# the ECS task-definition level.
#
# Built for linux/arm64 to match the Fargate runtime platform (see
# infra/platform/ecs.tf).
#
#   docker buildx build --platform linux/arm64 -t <ecr>/feederline-development/api:vX .

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry==1.8.2

WORKDIR /build

# Dependencies first so the layer survives app-only changes.
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-ansi

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --system --uid 10001 --no-create-home feederline

COPY --from=builder /build/.venv /opt/venv
COPY --from=builder /build/app /app/app
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini

WORKDIR /app
USER feederline
EXPOSE 8000

# Overridden to ["python","-m","app.outbox"] by the worker task definition.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
