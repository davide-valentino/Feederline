"""Operator audit trail.

Every mutating ``/internal/*`` route writes one row in the same transaction as the
change it describes.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base, CreatedAtMixin


class OperatorAudit(Base, CreatedAtMixin):
    __tablename__ = "operator_audit"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )


def record_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: UUID,
    request_metadata: dict[str, Any] | None = None,
) -> OperatorAudit:
    audit = OperatorAudit(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_metadata=request_metadata or {},
    )
    session.add(audit)
    return audit
