from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base, CreatedAtMixin, UTCDateTime, get_session
from app.domains.destinations import Destination
from app.domains.sources import Source


class DeliveryState(str, Enum):
    PENDING = "PENDING"
    STARTING = "STARTING"
    IN_PROGRESS = "IN_PROGRESS"
    STOPPING = "STOPPING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    DELETED = "DELETED"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"


class WorkflowRunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Delivery(Base, CreatedAtMixin):
    __tablename__ = "delivery"
    __table_args__ = (
        CheckConstraint(
            "(redundant_source_id IS NULL) = (redundant_destination_id IS NULL)",
            name="ck_delivery_redundant_pair",
        ),
        CheckConstraint(
            "(is_open_ended AND planned_end IS NULL) OR "
            "(NOT is_open_ended AND planned_end IS NOT NULL)",
            name="ck_delivery_open_ended_schedule",
        ),
        CheckConstraint(
            "planned_end IS NULL OR planned_end > planned_start",
            name="ck_delivery_end_after_start",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    destination_id: Mapped[UUID] = mapped_column(
        ForeignKey("destination.id", ondelete="RESTRICT"), nullable=False
    )
    redundant_source_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT")
    )
    redundant_destination_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("destination.id", ondelete="RESTRICT")
    )
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    planned_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_open_ended: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    stream_profile_locked: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    media_options: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DeliveryState.PENDING.value
    )
    stop_reason: Mapped[str | None] = mapped_column(String(32))


class DeliveryOutboxEvent(Base, CreatedAtMixin):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        Index("ix_delivery_outbox_status_due_at", "status", "due_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OutboxStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DeliveryWorkflowRun(Base, CreatedAtMixin):
    __tablename__ = "delivery_workflow_run"
    __table_args__ = (
        Index("ix_delivery_workflow_run_delivery_created", "delivery_id", "created_at"),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_outbox.id", ondelete="CASCADE"), primary_key=True
    )
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_arn: Mapped[str | None] = mapped_column(Text())
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WorkflowRunStatus.QUEUED.value
    )
    resolved_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON())
    resource_map: Mapped[dict[str, Any] | None] = mapped_column(JSON())
    last_error: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class DeliveryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    destination_id: UUID
    redundant_source_id: UUID | None = None
    redundant_destination_id: UUID | None = None
    planned_start: datetime
    planned_end: datetime | None = None
    is_open_ended: bool = False
    stream_profile_locked: bool = False
    media_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schedule(self):
        if (self.redundant_source_id is None) != (self.redundant_destination_id is None):
            raise ValueError("redundant source and destination must be provided together")
        for value in (self.planned_start, self.planned_end):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("schedule timestamps must include a timezone")
        if self.is_open_ended == (self.planned_end is not None):
            raise ValueError("open-ended deliveries must omit planned_end; others require it")
        if self.planned_end is not None and self.planned_end <= self.planned_start:
            raise ValueError("planned_end must be later than planned_start")
        return self


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: UUID
    destination_id: UUID
    redundant_source_id: UUID | None
    redundant_destination_id: UUID | None
    planned_start: datetime
    planned_end: datetime | None
    is_open_ended: bool
    stream_profile_locked: bool
    media_options: dict[str, Any]
    state: DeliveryState
    stop_reason: str | None
    created_at: datetime


router = APIRouter(prefix="/v1/deliveries", tags=["deliveries"])
internal_router = APIRouter(prefix="/internal/deliveries", tags=["delivery events"])


@router.post("", response_model=DeliveryRead, status_code=status.HTTP_201_CREATED)
def create_delivery(
    payload: DeliveryCreate, session: Session = Depends(get_session)
) -> Delivery:
    references = (
        ("Source", Source, payload.source_id),
        ("Destination", Destination, payload.destination_id),
        ("Redundant source", Source, payload.redundant_source_id),
        ("Redundant destination", Destination, payload.redundant_destination_id),
    )
    for label, model, resource_id in references:
        if resource_id is None:
            continue
        resource = session.get(model, resource_id)
        if resource is None or resource.is_deleted:
            raise HTTPException(status_code=404, detail=f"{label} not found")

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("LOCK TABLE delivery IN SHARE ROW EXCLUSIVE MODE"))

    destination_ids = {
        resource_id
        for resource_id in (
            payload.destination_id,
            payload.redundant_destination_id,
        )
        if resource_id is not None
    }
    overlap_conditions = [
        Delivery.destination_id.in_(destination_ids),
        Delivery.redundant_destination_id.in_(destination_ids),
    ]
    overlap_filters = [
        Delivery.state.in_(
            [
                DeliveryState.PENDING.value,
                DeliveryState.STARTING.value,
                DeliveryState.IN_PROGRESS.value,
                DeliveryState.STOPPING.value,
            ]
        ),
        or_(Delivery.planned_end.is_(None), Delivery.planned_end > payload.planned_start),
        or_(*overlap_conditions),
    ]
    if payload.planned_end is not None:
        overlap_filters.append(Delivery.planned_start < payload.planned_end)
    overlap_statement = select(Delivery.id).where(*overlap_filters)
    if session.scalar(overlap_statement.limit(1)) is not None:
        raise HTTPException(status_code=409, detail="Destination overlaps an existing delivery")

    delivery = Delivery(**payload.model_dump())
    session.add(delivery)
    session.flush()
    session.add(
        DeliveryOutboxEvent(
            delivery_id=delivery.id,
            event_type="DeliveryStart",
            due_at=payload.planned_start,
            payload={"deliveryId": str(delivery.id)},
        )
    )
    if payload.planned_end is not None:
        session.add(
            DeliveryOutboxEvent(
                delivery_id=delivery.id,
                event_type="DeliveryStop",
                due_at=payload.planned_end,
                payload={"deliveryId": str(delivery.id)},
            )
        )
    session.commit()
    session.refresh(delivery)
    return delivery


@router.get("", response_model=list[DeliveryRead])
def list_deliveries(
    state: DeliveryState | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[Delivery]:
    statement = (
        select(Delivery)
        .where(Delivery.state != DeliveryState.DELETED.value)
        .order_by(Delivery.planned_start)
        .limit(limit)
        .offset(offset)
    )
    if state is not None:
        statement = statement.where(Delivery.state == state.value)
    return list(session.scalars(statement).all())


@router.get("/{delivery_id}", response_model=DeliveryRead)
def get_delivery(delivery_id: UUID, session: Session = Depends(get_session)) -> Delivery:
    delivery = session.get(Delivery, delivery_id)
    if delivery is None or delivery.state == DeliveryState.DELETED.value:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


def _get_delivery_or_404(delivery_id: UUID, session: Session) -> Delivery:
    delivery = session.get(Delivery, delivery_id)
    if delivery is None or delivery.state == DeliveryState.DELETED.value:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


def _lock_delivery_or_404(delivery_id: UUID, session: Session) -> Delivery:
    delivery = session.scalar(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    )
    if delivery is None or delivery.state == DeliveryState.DELETED.value:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


def _cancel_pending_events(session: Session, delivery_id: UUID) -> None:
    events = session.scalars(
        select(DeliveryOutboxEvent).where(
            DeliveryOutboxEvent.delivery_id == delivery_id,
            DeliveryOutboxEvent.status == OutboxStatus.PENDING.value,
        )
    )
    for event in events:
        event.status = OutboxStatus.CANCELLED.value


def _queue_event(
    session: Session, delivery: Delivery, event_type: str, due_at: datetime
) -> None:
    event = DeliveryOutboxEvent(
        delivery_id=delivery.id,
        event_type=event_type,
        due_at=due_at,
        payload={"deliveryId": str(delivery.id)},
    )
    session.add(event)
    if event_type in ("PipelineLink", "PipelineUnlink"):
        session.flush()
        session.add(
            DeliveryWorkflowRun(
                event_id=event.id,
                delivery_id=delivery.id,
                event_type=event_type,
            )
        )


def _queue_event_once(
    session: Session, delivery: Delivery, event_type: str, due_at: datetime
) -> None:
    existing_event_id = session.scalar(
        select(DeliveryOutboxEvent.id)
        .where(
            DeliveryOutboxEvent.delivery_id == delivery.id,
            DeliveryOutboxEvent.event_type == event_type,
        )
        .limit(1)
    )
    if existing_event_id is None:
        _queue_event(session, delivery, event_type, due_at)


def complete_pipeline_link(session: Session, delivery: Delivery) -> None:
    if delivery.state == DeliveryState.STARTING.value:
        delivery.state = DeliveryState.IN_PROGRESS.value
        if delivery.planned_end is not None and delivery.planned_end <= datetime.now(timezone.utc):
            delivery.state = DeliveryState.STOPPING.value
            delivery.stop_reason = delivery.stop_reason or "SCHEDULED_END"
            _queue_event_once(session, delivery, "PipelineUnlink", datetime.now(timezone.utc))
    elif delivery.state == DeliveryState.STOPPING.value:
        _queue_event_once(session, delivery, "PipelineUnlink", datetime.now(timezone.utc))
    elif delivery.state != DeliveryState.IN_PROGRESS.value:
        raise HTTPException(status_code=409, detail="Delivery is not starting")


@internal_router.post("/{delivery_id}/start", response_model=DeliveryRead)
def start_delivery(delivery_id: UUID, session: Session = Depends(get_session)) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state in (DeliveryState.STARTING.value, DeliveryState.IN_PROGRESS.value):
        return delivery
    if delivery.state != DeliveryState.PENDING.value:
        raise HTTPException(status_code=409, detail="Delivery is not pending")
    delivery.state = DeliveryState.STARTING.value
    _queue_event(session, delivery, "PipelineLink", datetime.now(timezone.utc))
    session.commit()
    session.refresh(delivery)
    return delivery


@internal_router.post("/{delivery_id}/stop", response_model=DeliveryRead)
def stop_delivery(delivery_id: UUID, session: Session = Depends(get_session)) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state == DeliveryState.PENDING.value:
        _cancel_pending_events(session, delivery.id)
        delivery.state = DeliveryState.CANCELLED.value
    elif delivery.state in (DeliveryState.STARTING.value, DeliveryState.IN_PROGRESS.value):
        _cancel_pending_events(session, delivery.id)
        was_starting = delivery.state == DeliveryState.STARTING.value
        delivery.state = DeliveryState.STOPPING.value
        delivery.stop_reason = delivery.stop_reason or "SCHEDULED_END"
        if not was_starting:
            _queue_event_once(session, delivery, "PipelineUnlink", datetime.now(timezone.utc))
    elif delivery.state not in (DeliveryState.STOPPING.value, DeliveryState.CANCELLED.value):
        raise HTTPException(status_code=409, detail="Delivery cannot be stopped")
    session.commit()
    session.refresh(delivery)
    return delivery


@router.patch("/{delivery_id}/cancel", response_model=DeliveryRead)
def cancel_delivery(delivery_id: UUID, session: Session = Depends(get_session)) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state == DeliveryState.PENDING.value:
        _cancel_pending_events(session, delivery.id)
        delivery.state = DeliveryState.CANCELLED.value
    elif delivery.state in (DeliveryState.STARTING.value, DeliveryState.IN_PROGRESS.value):
        _cancel_pending_events(session, delivery.id)
        was_starting = delivery.state == DeliveryState.STARTING.value
        delivery.state = DeliveryState.STOPPING.value
        delivery.stop_reason = "CANCELLED"
        if not was_starting:
            _queue_event_once(session, delivery, "PipelineUnlink", datetime.now(timezone.utc))
    elif delivery.state not in (DeliveryState.STOPPING.value, DeliveryState.CANCELLED.value):
        raise HTTPException(status_code=409, detail="Delivery can no longer be cancelled")
    session.commit()
    session.refresh(delivery)
    return delivery


@router.post("/{delivery_id}/pipeline-started", response_model=DeliveryRead)
def mark_pipeline_started(
    delivery_id: UUID, session: Session = Depends(get_session)
) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    complete_pipeline_link(session, delivery)
    session.commit()
    session.refresh(delivery)
    return delivery


@router.post("/{delivery_id}/pipeline-stopped", response_model=DeliveryRead)
def mark_pipeline_stopped(
    delivery_id: UUID, session: Session = Depends(get_session)
) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state in (DeliveryState.COMPLETE.value, DeliveryState.CANCELLED.value):
        return delivery
    if delivery.state != DeliveryState.STOPPING.value:
        raise HTTPException(status_code=409, detail="Delivery is not stopping")
    delivery.state = (
        DeliveryState.CANCELLED.value
        if delivery.stop_reason == "CANCELLED"
        else DeliveryState.COMPLETE.value
    )
    session.commit()
    session.refresh(delivery)
    return delivery


@router.post("/{delivery_id}/pipeline-failed", response_model=DeliveryRead)
def mark_pipeline_failed(
    delivery_id: UUID, session: Session = Depends(get_session)
) -> Delivery:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state in (
        DeliveryState.COMPLETE.value,
        DeliveryState.CANCELLED.value,
        DeliveryState.DELETED.value,
    ):
        raise HTTPException(status_code=409, detail="Terminal delivery cannot fail")
    _cancel_pending_events(session, delivery.id)
    delivery.state = DeliveryState.ERROR.value
    session.commit()
    session.refresh(delivery)
    return delivery


@router.delete("/{delivery_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_delivery(delivery_id: UUID, session: Session = Depends(get_session)) -> None:
    delivery = _lock_delivery_or_404(delivery_id, session)
    if delivery.state in (
        DeliveryState.STARTING.value,
        DeliveryState.IN_PROGRESS.value,
        DeliveryState.STOPPING.value,
    ):
        raise HTTPException(status_code=409, detail="Stop the delivery before deleting it")
    _cancel_pending_events(session, delivery.id)
    delivery.state = DeliveryState.DELETED.value
    session.commit()
