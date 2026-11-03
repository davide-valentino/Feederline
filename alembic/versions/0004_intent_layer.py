"""Add the customer intent layer: content, routes, events, operator audit.

Revision ID: 0004_intent_layer
Revises: 0003_workflows
Create Date: 2026-11-03
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_intent_layer"
down_revision: str | None = "0003_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("content_type", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_content_name"),
    )

    op.add_column("source", sa.Column("capability_id", sa.String(length=64), nullable=True))
    op.add_column(
        "destination", sa.Column("capability_id", sa.String(length=64), nullable=True)
    )

    # Customer connections and legacy execution rows are separate name domains. Keep
    # uniqueness inside each domain without letting a customer name collide with (and
    # therefore probe for) a hidden legacy row.
    op.drop_constraint("uq_source_name", "source", type_="unique")
    op.drop_constraint("uq_destination_name", "destination", type_="unique")
    op.create_index(
        "uq_source_customer_name",
        "source",
        ["name"],
        unique=True,
        postgresql_where=sa.text("capability_id IS NOT NULL"),
    )
    op.create_index(
        "uq_source_legacy_name",
        "source",
        ["name"],
        unique=True,
        postgresql_where=sa.text("capability_id IS NULL"),
    )
    op.create_index(
        "uq_destination_customer_name",
        "destination",
        ["name"],
        unique=True,
        postgresql_where=sa.text("capability_id IS NOT NULL"),
    )
    op.create_index(
        "uq_destination_legacy_name",
        "destination",
        ["name"],
        unique=True,
        postgresql_where=sa.text("capability_id IS NULL"),
    )

    op.create_table(
        "route",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("feed_id", sa.Uuid(), nullable=False),
        sa.Column("redundant_feed_id", sa.Uuid(), nullable=True),
        sa.Column("quality_preset", sa.String(length=32), nullable=False),
        sa.Column("resilience", sa.String(length=16), nullable=False),
        sa.Column("desired_state", sa.String(length=16), nullable=False),
        sa.Column("observed_state", sa.String(length=16), nullable=False),
        sa.Column("health", sa.String(length=16), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_open_ended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("encrypted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("resolved_plan", sa.JSON(), nullable=True),
        sa.Column("plan_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "planned_end IS NULL OR planned_end > planned_start",
            name="ck_route_end_after_start",
        ),
        sa.CheckConstraint(
            "(is_open_ended AND planned_end IS NULL) OR "
            "(NOT is_open_ended AND planned_end IS NOT NULL)",
            name="ck_route_open_ended_schedule",
        ),
        sa.CheckConstraint(
            "resilience = 'single' OR redundant_feed_id IS NOT NULL",
            name="ck_route_redundant_feed_required",
        ),
        sa.CheckConstraint(
            "resilience IN ('single', 'redundant', 'geo-redundant')",
            name="ck_route_resilience",
        ),
        sa.ForeignKeyConstraint(["content_id"], ["content.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["feed_id"], ["source.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["redundant_feed_id"], ["source.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_desired_state_planned_start",
        "route",
        ["desired_state", "planned_start"],
    )
    op.create_index("ix_route_content_id", "route", ["content_id"])

    op.create_table(
        "route_output",
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("output_id", sa.Uuid(), nullable=False),
        sa.Column("redundant_output_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "redundant_output_id IS NULL OR redundant_output_id <> output_id",
            name="ck_route_output_distinct",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["route.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["output_id"], ["destination.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["redundant_output_id"], ["destination.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("route_id", "output_id"),
    )
    op.create_index("ix_route_output_output_id", "route_output", ["output_id"])

    op.create_table(
        "route_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["route_id"], ["route.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_route_event_route_created",
        "route_event",
        ["route_id", "created_at"],
    )

    op.create_table(
        "operator_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("request_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "idempotency_key",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("route_id", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["route_id"], ["route.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("key"),
    )

    op.add_column("delivery", sa.Column("route_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_delivery_route_id", "delivery", "route", ["route_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_delivery_route_id", "delivery", ["route_id"])


def downgrade() -> None:
    op.drop_index("ix_delivery_route_id", table_name="delivery")
    op.drop_constraint("fk_delivery_route_id", "delivery", type_="foreignkey")
    op.drop_column("delivery", "route_id")

    op.drop_table("idempotency_key")
    op.drop_table("operator_audit")
    op.drop_index("ix_route_event_route_created", table_name="route_event")
    op.drop_table("route_event")
    op.drop_index("ix_route_output_output_id", table_name="route_output")
    op.drop_table("route_output")
    op.drop_index("ix_route_content_id", table_name="route")
    op.drop_index("ix_route_desired_state_planned_start", table_name="route")
    op.drop_table("route")

    for index_name, table_name in (
        ("uq_source_customer_name", "source"),
        ("uq_source_legacy_name", "source"),
        ("uq_destination_customer_name", "destination"),
        ("uq_destination_legacy_name", "destination"),
    ):
        op.drop_index(index_name, table_name=table_name)
    op.drop_column("destination", "capability_id")
    op.drop_column("source", "capability_id")
    op.create_unique_constraint("uq_source_name", "source", ["name"])
    op.create_unique_constraint("uq_destination_name", "destination", ["name"])
    op.drop_table("content")
