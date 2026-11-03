"""Customer-facing route event vocabulary and emission.

Raw ``DeliveryStart`` / ``PipelineLink`` / ``PipelineUnlink`` events stay operator-only.
This module is the single translation point, so it can be tested in isolation.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base, CreatedAtMixin, UTCDateTime


class RouteEventType(str, Enum):
    SCHEDULED = "route.scheduled"
    STARTED = "route.started"
    DEGRADED = "route.degraded"
    FAILED = "route.failed"
    COMPLETED = "route.completed"
    STOPPED = "route.stopped"
    CANCELLED = "route.cancelled"


class RouteEventLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    NEUTRAL = "neutral"


ROUTE_EVENT_TYPES = tuple(event.value for event in RouteEventType)


class RouteEvent(Base, CreatedAtMixin):
    __tablename__ = "route_event"
    __table_args__ = (
        Index("ix_route_event_route_created", "route_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )


def emit_route_event(
    session: Session,
    route_id: UUID,
    event_type: RouteEventType | str,
    *,
    level: RouteEventLevel | str,
    message: str,
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> RouteEvent:
    event = RouteEvent(
        route_id=route_id,
        event_type=event_type.value if isinstance(event_type, RouteEventType) else event_type,
        level=level.value if isinstance(level, RouteEventLevel) else level,
        message=message,
        event_metadata=metadata or {},
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(event)
    return event


# Metadata keys that are safe to show on the customer timeline. Delivery states and
# provider identifiers stay on the operator surface.
_PUBLIC_EVENT_METADATA = {
    "previous_state",
    "observed_state",
    "planned_start",
    "planned_end",
    "output_count",
    "resilience",
}


def route_event_to_dict(event: RouteEvent, *, public: bool = False) -> dict[str, Any]:
    metadata = event.event_metadata
    if public:
        metadata = {
            key: value
            for key, value in metadata.items()
            if key in _PUBLIC_EVENT_METADATA
        }
    return {
        "id": event.id,
        "route_id": event.route_id,
        "event_type": event.event_type,
        "level": event.level,
        "message": event.message,
        "metadata": metadata,
        "created_at": event.created_at,
    }
