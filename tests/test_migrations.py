import subprocess
import sys
from pathlib import Path


def test_alembic_head_renders_postgres_sql_with_all_p0_tables():
    repository_root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=repository_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    sql = result.stdout
    for table in (
        "content",
        "route",
        "route_output",
        "route_event",
        "operator_audit",
        "idempotency_key",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "ALTER TABLE source ADD COLUMN capability_id" in sql
    assert "ALTER TABLE destination ADD COLUMN capability_id" in sql
    assert "ALTER TABLE delivery ADD COLUMN route_id" in sql
    assert "CREATE INDEX ix_delivery_route_id" in sql
    for index in (
        "uq_source_customer_name",
        "uq_source_legacy_name",
        "uq_destination_customer_name",
        "uq_destination_legacy_name",
    ):
        assert f"CREATE UNIQUE INDEX {index}" in sql
    assert "WHERE capability_id IS NOT NULL" in sql
    assert "WHERE capability_id IS NULL" in sql
