from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.auth import require_operator
from app.database import Base, CreatedAtMixin, get_session


class StreamProfile(Base, CreatedAtMixin):
    __tablename__ = "stream_profile"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    profile_type: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_deleted: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")


class StreamProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    profile_type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "profile_type")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class StreamProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
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


class StreamProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    profile_type: str
    config: dict[str, Any]
    created_at: datetime


router = APIRouter(
    prefix="/v1/stream-profiles",
    tags=["stream profiles"],
    dependencies=[Depends(require_operator)],
)


@router.post("", response_model=StreamProfileRead, status_code=status.HTTP_201_CREATED)
def create_stream_profile(
    payload: StreamProfileCreate, session: Session = Depends(get_session)
) -> StreamProfile:
    profile = StreamProfile(**payload.model_dump())
    session.add(profile)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Stream profile name already exists") from error
    session.refresh(profile)
    return profile


@router.get("", response_model=list[StreamProfileRead])
def list_stream_profiles(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[StreamProfile]:
    statement = (
        select(StreamProfile)
        .where(StreamProfile.is_deleted.is_(False))
        .order_by(StreamProfile.name)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement).all())


@router.get("/{profile_id}", response_model=StreamProfileRead)
def get_stream_profile(profile_id: UUID, session: Session = Depends(get_session)) -> StreamProfile:
    profile = session.get(StreamProfile, profile_id)
    if profile is None or profile.is_deleted:
        raise HTTPException(status_code=404, detail="Stream profile not found")
    return profile


@router.patch("/{profile_id}", response_model=StreamProfileRead)
def update_stream_profile(
    profile_id: UUID,
    payload: StreamProfileUpdate,
    session: Session = Depends(get_session),
) -> StreamProfile:
    profile = session.get(StreamProfile, profile_id)
    if profile is None or profile.is_deleted:
        raise HTTPException(status_code=404, detail="Stream profile not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(profile, field, value)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Stream profile name already exists") from error
    session.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_stream_profile(profile_id: UUID, session: Session = Depends(get_session)) -> None:
    profile = session.get(StreamProfile, profile_id)
    if profile is None or profile.is_deleted:
        raise HTTPException(status_code=404, detail="Stream profile not found")

    from app.domains.destinations import Destination
    from app.domains.sources import Source

    reference_exists = session.scalar(
        select(Source.id).where(Source.stream_profile_id == profile_id).limit(1)
    ) or session.scalar(
        select(Destination.id).where(Destination.stream_profile_id == profile_id).limit(1)
    )
    if reference_exists is not None:
        raise HTTPException(
            status_code=409,
            detail="Stream profile is referenced by a source or destination",
        )
    profile.is_deleted = True
    session.commit()
