from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, ForeignKey, Index, String, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.auth import require_operator
from app.database import Base, CreatedAtMixin, get_session
from app.domains.stream_profiles import StreamProfile


class DestinationType(str, Enum):
    IP = "ipdestination"
    MEDIACONNECT = "mediaconnectdestination"
    ZIXI_PUSH = "zixipushdestination"
    ZIXI_PULL = "zixipulldestination"
    SRT_LISTENER = "srtlistenerdestination"
    SRT_CALLER = "srtcallerdestination"
    RTMP = "rtmpdestination"
    HLS = "hlsdestination"
    DASH = "dashdestination"
    S3_ARCHIVE = "s3archivedestination"
    MEDIA_SHUTTLE = "mediashuttledestination"


class Destination(Base, CreatedAtMixin):
    __tablename__ = "destination"
    __table_args__ = (
        Index(
            "uq_destination_customer_name",
            "name",
            unique=True,
            sqlite_where=text("capability_id IS NOT NULL"),
            postgresql_where=text("capability_id IS NOT NULL"),
        ),
        Index(
            "uq_destination_legacy_name",
            "name",
            unique=True,
            sqlite_where=text("capability_id IS NULL"),
            postgresql_where=text("capability_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    destination_type: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str | None] = mapped_column(String(64))
    availability_zone: Mapped[str | None] = mapped_column(String(64))
    stream_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("stream_profile.id", ondelete="RESTRICT")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_deleted: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")


class DestinationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    destination_type: DestinationType
    region: str | None = Field(default=None, max_length=64)
    availability_zone: str | None = Field(default=None, max_length=64)
    stream_profile_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class DestinationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    region: str | None = Field(default=None, max_length=64)
    availability_zone: str | None = Field(default=None, max_length=64)
    stream_profile_id: UUID | None = None
    config: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class DestinationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    destination_type: str
    capability_id: str | None
    region: str | None
    availability_zone: str | None
    stream_profile_id: UUID | None
    config: dict[str, Any]
    created_at: datetime


router = APIRouter(
    prefix="/v1/destinations",
    tags=["destinations"],
    dependencies=[Depends(require_operator)],
)


@router.post("", response_model=DestinationRead, status_code=status.HTTP_201_CREATED)
def create_destination(
    payload: DestinationCreate, session: Session = Depends(get_session)
) -> Destination:
    if payload.stream_profile_id and session.get(StreamProfile, payload.stream_profile_id) is None:
        raise HTTPException(status_code=404, detail="Stream profile not found")

    destination_data = payload.model_dump()
    destination = Destination(**destination_data)
    session.add(destination)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Destination name already exists") from error
    session.refresh(destination)
    return destination


@router.get("", response_model=list[DestinationRead])
def list_destinations(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[Destination]:
    statement = (
        select(Destination)
        .where(Destination.is_deleted.is_(False))
        .order_by(Destination.name)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement).all())


@router.get("/{destination_id}", response_model=DestinationRead)
def get_destination(
    destination_id: UUID, session: Session = Depends(get_session)
) -> Destination:
    destination = session.get(Destination, destination_id)
    if destination is None or destination.is_deleted:
        raise HTTPException(status_code=404, detail="Destination not found")
    return destination


@router.patch("/{destination_id}", response_model=DestinationRead)
def update_destination(
    destination_id: UUID,
    payload: DestinationUpdate,
    session: Session = Depends(get_session),
) -> Destination:
    destination = session.get(Destination, destination_id)
    if destination is None or destination.is_deleted:
        raise HTTPException(status_code=404, detail="Destination not found")
    updates = payload.model_dump(exclude_unset=True)
    if "stream_profile_id" in updates and updates["stream_profile_id"] is not None:
        profile = session.get(StreamProfile, updates["stream_profile_id"])
        if profile is None or profile.is_deleted:
            raise HTTPException(status_code=404, detail="Stream profile not found")
    for field, value in updates.items():
        setattr(destination, field, value)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Destination name already exists") from error
    session.refresh(destination)
    return destination


@router.delete("/{destination_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_destination(destination_id: UUID, session: Session = Depends(get_session)) -> None:
    destination = session.get(Destination, destination_id)
    if destination is None or destination.is_deleted:
        raise HTTPException(status_code=404, detail="Destination not found")

    from app.domains.deliveries import Delivery, DeliveryState

    active_states = [
        DeliveryState.PENDING.value,
        DeliveryState.STARTING.value,
        DeliveryState.IN_PROGRESS.value,
        DeliveryState.STOPPING.value,
    ]
    active_delivery = session.scalar(
        select(Delivery.id)
        .where(
            Delivery.state.in_(active_states),
            or_(
                Delivery.destination_id == destination_id,
                Delivery.redundant_destination_id == destination_id,
            ),
        )
        .limit(1)
    )
    if active_delivery is not None:
        raise HTTPException(status_code=409, detail="Destination is used by an active delivery")
    destination.is_deleted = True
    session.commit()
