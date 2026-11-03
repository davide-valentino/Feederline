"""Content registry: identity and ownership only.

Status is *derived* from the content's routes (scope D5). There is deliberately no
stored ``status`` column.
"""

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, Text, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.database import Base, CreatedAtMixin, get_session


class ContentType(str, Enum):
    CHANNEL = "channel"
    EVENT = "event"


class ContentStatus(str, Enum):
    LIVE = "live"
    SCHEDULED = "scheduled"
    COMPLETE = "complete"
    OFF_AIR = "off-air"


class Content(Base, CreatedAtMixin):
    __tablename__ = "content"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(16), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_deleted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )


class ContentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    content_type: ContentType
    owner: str = Field(min_length=1, max_length=128)
    description: str | None = None

    @field_validator("name", "owner")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    owner: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None

    @field_validator("name", "owner")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ContentRead(BaseModel):
    id: UUID
    name: str
    content_type: ContentType
    owner: str
    description: str | None
    status: ContentStatus
    created_at: datetime


router = APIRouter(prefix="/v1/content", tags=["content"])


def derive_content_status(session: Session, content_id: UUID) -> ContentStatus:
    """Return the content status using the D5 precedence order.

    The single implementation lives here so the API and tests cannot drift.
    """

    from app.domains.routes import Route

    routes = session.scalars(
        select(Route).where(Route.content_id == content_id, Route.is_deleted.is_(False))
    ).all()
    if not routes:
        return ContentStatus.OFF_AIR

    now = datetime.now(timezone.utc)
    if any(
        route.observed_state in {"provisioning", "live", "degraded"} for route in routes
    ):
        return ContentStatus.LIVE
    if any(
        route.desired_state == "scheduled" and route.planned_start > now
        for route in routes
    ):
        return ContentStatus.SCHEDULED
    if any(route.desired_state == "active" for route in routes):
        return ContentStatus.LIVE
    if all(route.observed_state == "completed" for route in routes):
        return ContentStatus.COMPLETE
    return ContentStatus.OFF_AIR


def _content_read(content: Content, content_status: ContentStatus) -> ContentRead:
    return ContentRead(
        id=content.id,
        name=content.name,
        content_type=ContentType(content.content_type),
        owner=content.owner,
        description=content.description,
        status=content_status,
        created_at=content.created_at,
    )


def _get_content_or_404(content_id: UUID, session: Session) -> Content:
    content = session.get(Content, content_id)
    if content is None or content.is_deleted:
        raise HTTPException(status_code=404, detail="Content not found")
    return content


@router.post("", response_model=ContentRead, status_code=status.HTTP_201_CREATED)
def create_content(payload: ContentCreate, session: Session = Depends(get_session)) -> ContentRead:
    content = Content(
        name=payload.name,
        content_type=payload.content_type.value,
        owner=payload.owner,
        description=payload.description,
    )
    session.add(content)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Content name already exists") from error
    session.refresh(content)
    return _content_read(content, ContentStatus.OFF_AIR)


@router.get("", response_model=list[ContentRead])
def list_content(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[ContentRead]:
    statement = (
        select(Content)
        .where(Content.is_deleted.is_(False))
        .order_by(Content.name)
        .limit(limit)
        .offset(offset)
    )
    return [
        _content_read(content, derive_content_status(session, content.id))
        for content in session.scalars(statement).all()
    ]


@router.get("/{content_id}", response_model=ContentRead)
def get_content(content_id: UUID, session: Session = Depends(get_session)) -> ContentRead:
    content = _get_content_or_404(content_id, session)
    return _content_read(content, derive_content_status(session, content.id))


@router.patch("/{content_id}", response_model=ContentRead)
def update_content(
    content_id: UUID, payload: ContentUpdate, session: Session = Depends(get_session)
) -> ContentRead:
    content = _get_content_or_404(content_id, session)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(content, field, value)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Content name already exists") from error
    session.refresh(content)
    return _content_read(content, derive_content_status(session, content.id))


@router.delete("/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_content(content_id: UUID, session: Session = Depends(get_session)) -> None:
    content = _get_content_or_404(content_id, session)

    from app.domains.routes import Route

    active_route = session.scalar(
        select(Route.id)
        .where(
            Route.content_id == content_id,
            Route.is_deleted.is_(False),
            or_(
                Route.desired_state.in_(["scheduled", "active"]),
                Route.observed_state.in_(["provisioning", "live", "degraded", "stopping"]),
            ),
        )
        .limit(1)
    )
    if active_route is not None:
        raise HTTPException(status_code=409, detail="Content has active routes")
    content.is_deleted = True
    session.commit()
