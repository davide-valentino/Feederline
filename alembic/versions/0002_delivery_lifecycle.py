"""Add delivery lifecycle state and transactional outbox.

Revision ID: 0002_lifecycle
Revises: 0001_initial
Create Date: 2026-09-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0002_lifecycle"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("delivery", sa.Column("stop_reason", sa.String(length=32), nullable=True))
    op.execute("UPDATE delivery SET state = UPPER(state)")
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delivery_outbox_status_due_at",
        "delivery_outbox",
        ["status", "due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_outbox_status_due_at", table_name="delivery_outbox")
    op.drop_table("delivery_outbox")
    op.drop_column("delivery", "stop_reason")
