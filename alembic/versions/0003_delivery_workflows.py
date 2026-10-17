"""Persist delivery workflow executions and resource maps.

Revision ID: 0003_workflows
Revises: 0002_lifecycle
Create Date: 2026-09-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_workflows"
down_revision: str | None = "0002_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_workflow_run",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("execution_arn", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_plan", sa.JSON(), nullable=True),
        sa.Column("resource_map", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["delivery_outbox.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_delivery_workflow_run_delivery_created",
        "delivery_workflow_run",
        ["delivery_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_workflow_run_delivery_created",
        table_name="delivery_workflow_run",
    )
    op.drop_table("delivery_workflow_run")