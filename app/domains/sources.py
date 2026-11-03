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


class SourceType(str, Enum):
    IP = "ipsource"
    MEDIACONNECT = "mediaconnectsource"
    ZIXI_PUSH = "zixipushsource"
    SRT_LISTENER = "srtlistenersource"
    SRT_CALLER = "srtcallersource"


class Source(Base, CreatedAtMixin):
    __tablename__ = "source"
    __table_args__ = (
        # Customer Feeds and legacy Sources are separate name domains, so a customer
        # name cannot be used to probe for a hidden legacy row.
        Index(
            "uq_source_customer_name",
            "name",
            unique=True,
            sqlite_where=text("capability_id IS NOT NULL"),
            postgresql_where=text("capability_id IS NOT NULL"),
        ),
        Index(
            "uq_source_legacy_name",
            "name",
            unique=True,
            sqlite_where=text("capability_id IS NULL"),
            postgresql_where=text("capability_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_id: Mapped[str | None] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    availability_zone: Mapped[str | None] = mapped_column(String(64))
    stream_profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("stream_profile.id", ondelete="RESTRICT")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_deleted: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    source_type: SourceType
    region: str = Field(min_length=1, max_length=64)
    availability_zone: str | None = Field(default=None, max_length=64)
    stream_profile_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "region")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    region: str | None = Field(default=None, min_length=1, max_length=64)
    availability_zone: str | None = Field(default=None, max_length=64)
    stream_profile_id: UUID | None = None
    config: dict[str, Any] | None = None

    @field_validator("name", "region")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: str
    capability_id: str | None
    region: str
    availability_zone: str | None
    stream_profile_id: UUID | None
    config: dict[str, Any]
    created_at: datetime


router = APIRouter(
    prefix="/v1/sources",
    tags=["sources"],
    dependencies=[Depends(require_operator)],
)


@router.post("", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceCreate, session: Session = Depends(get_session)
) -> Source:
    if payload.stream_profile_id and session.get(StreamProfile, payload.stream_profile_id) is None:
        raise HTTPException(status_code=404, detail="Stream profile not found")

    source_data = payload.model_dump()
    source = Source(**source_data)
    session.add(source)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Source name already exists") from error
    session.refresh(source)
    return source


@router.get("", response_model=list[SourceRead])
def list_sources(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[Source]:
    statement = (
        select(Source)
        .where(Source.is_deleted.is_(False))
        .order_by(Source.name)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement).all())


@router.get("/{source_id}", response_model=SourceRead)
def get_source(source_id: UUID, session: Session = Depends(get_session)) -> Source:
    source = session.get(Source, source_id)
    if source is None or source.is_deleted:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.patch("/{source_id}", response_model=SourceRead)
def update_source(
    source_id: UUID,
    payload: SourceUpdate,
    session: Session = Depends(get_session),
) -> Source:
    source = session.get(Source, source_id)
    if source is None or source.is_deleted:
        raise HTTPException(status_code=404, detail="Source not found")
    updates = payload.model_dump(exclude_unset=True)
    if "stream_profile_id" in updates and updates["stream_profile_id"] is not None:
        profile = session.get(StreamProfile, updates["stream_profile_id"])
        if profile is None or profile.is_deleted:
            raise HTTPException(status_code=404, detail="Stream profile not found")
    for field, value in updates.items():
        setattr(source, field, value)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Source name already exists") from error
    session.refresh(source)
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: UUID, session: Session = Depends(get_session)) -> None:
    source = session.get(Source, source_id)
    if source is None or source.is_deleted:
        raise HTTPException(status_code=404, detail="Source not found")

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
            or_(Delivery.source_id == source_id, Delivery.redundant_source_id == source_id),
        )
        .limit(1)
    )
    if active_delivery is not None:
        raise HTTPException(status_code=409, detail="Source is used by an active delivery")
    source.is_deleted = True
    session.commit()
