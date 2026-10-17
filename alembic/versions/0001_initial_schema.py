"""Create the initial Feederline tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stream_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("profile_type", sa.String(length=64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_stream_profile_name"),
    )
    op.create_table(
        "source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("availability_zone", sa.String(length=64), nullable=True),
        sa.Column("stream_profile_id", sa.Uuid(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["stream_profile_id"], ["stream_profile.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_source_name"),
    )
    op.create_table(
        "destination",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("destination_type", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("availability_zone", sa.String(length=64), nullable=True),
        sa.Column("stream_profile_id", sa.Uuid(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["stream_profile_id"], ["stream_profile.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_destination_name"),
    )
    op.create_table(
        "delivery",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("destination_id", sa.Uuid(), nullable=False),
        sa.Column("redundant_source_id", sa.Uuid(), nullable=True),
        sa.Column("redundant_destination_id", sa.Uuid(), nullable=True),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_open_ended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stream_profile_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("media_options", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "(redundant_source_id IS NULL) = (redundant_destination_id IS NULL)",
            name="ck_delivery_redundant_pair",
        ),
        sa.CheckConstraint(
            "(is_open_ended AND planned_end IS NULL) OR "
            "(NOT is_open_ended AND planned_end IS NOT NULL)",
            name="ck_delivery_open_ended_schedule",
        ),
        sa.CheckConstraint(
            "planned_end IS NULL OR planned_end > planned_start",
            name="ck_delivery_end_after_start",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["source.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["destination_id"], ["destination.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["redundant_source_id"], ["source.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["redundant_destination_id"], ["destination.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("delivery")
    op.drop_table("destination")
    op.drop_table("source")
    op.drop_table("stream_profile")
