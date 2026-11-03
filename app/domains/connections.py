"""Customer-facing Feeds and Outputs.

Decision D1: extend ``source`` / ``destination`` in place rather than introduce a
parallel ``connection`` table. The customer sends a capability id; the execution type
(``source_type`` / ``destination_type``) is derived from the catalog and is never chosen
by the client.
"""

import ipaddress
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.capabilities.catalog import (
    Capability,
    field_is_required,
    get_capability,
    public_config,
    region_availability,
)
from app.database import get_session
from app.domains.destinations import Destination
from app.domains.sources import Source


def _strip_required(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _invalid(capability: Capability, field: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=422, detail=f"Capability {capability.id}, field '{field}': {message}"
    )


def _check_value(capability: Capability, spec, value: Any) -> None:
    if spec.choices is not None and value not in spec.choices:
        allowed = ", ".join(spec.choices)
        raise _invalid(capability, spec.name, f"must be one of: {allowed}")
    if spec.field_type in {"string", "url", "cidr"}:
        if not isinstance(value, str) or not value.strip():
            raise _invalid(capability, spec.name, "must be a non-blank string")
        if spec.field_type == "url":
            parsed = urlparse(value)
            if not parsed.scheme:
                raise _invalid(capability, spec.name, "must be an absolute URL")
        elif spec.field_type == "cidr":
            try:
                for entry in value.split(","):
                    ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError as error:
                raise _invalid(
                    capability, spec.name, "must be a CIDR (e.g. 192.0.2.0/24)"
                ) from error
    elif spec.field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _invalid(capability, spec.name, "must be an integer")
    elif spec.field_type == "boolean":
        if not isinstance(value, bool):
            raise _invalid(capability, spec.name, "must be a boolean")


def _validate_config(capability: Capability, config: dict[str, Any]) -> None:
    checked: set[str] = set()
    for spec in capability.required_fields:
        checked.add(spec.name)
        value = config.get(spec.name)
        if value is None:
            if field_is_required(spec, config):
                raise _invalid(capability, spec.name, "is required")
            continue
        _check_value(capability, spec, value)
    # Validate the type/choices of optional values too, so bad config does not reach
    # the plan. Unknown keys are allowed (they may be provider passthrough fields).
    specs = {spec.name: spec for spec in capability.required_fields}
    for name, value in config.items():
        if name in checked or value is None:
            continue
        spec = specs.get(name)
        if spec is not None:
            _check_value(capability, spec, value)


def _merge_config(capability: Capability, config: dict[str, Any] | None) -> dict[str, Any]:
    """Add platform-filled fields (e.g. the RIST/RTP protocol under ``ipsource``)."""
    return {**(config or {}), **capability.fixed_config}


def _validated_capability(
    capability_id: str, kind: str, region: str | None, config: dict[str, Any]
) -> Capability:
    capability = get_capability(capability_id)
    if capability is None:
        raise HTTPException(status_code=422, detail=f"Unknown capability '{capability_id}'")
    if capability.kind != kind:
        raise HTTPException(
            status_code=422,
            detail=f"Capability '{capability_id}' is a {capability.kind}, not a {kind}",
        )
    availability = region_availability(capability, region)
    if not availability.available:
        raise HTTPException(
            status_code=422,
            detail=availability.reason
            or f"Capability '{capability_id}' is not available in {region}",
        )
    _validate_config(capability, config)
    return capability


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------


class FeedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "region", "capability_id")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        return _strip_required(value)


class FeedUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    capability_id: str | None = Field(default=None, min_length=1, max_length=64)
    region: str | None = Field(default=None, min_length=1, max_length=64)
    config: dict[str, Any] | None = None

    @field_validator("name", "region", "capability_id")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required(value)


class CapabilitySummary(BaseModel):
    id: str
    kind: str
    name: str
    mode: str


class FeedRead(BaseModel):
    id: UUID
    name: str
    capability: CapabilitySummary
    region: str
    details: dict[str, Any]
    created_at: datetime


class OutputCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", "region", "capability_id")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        return _strip_required(value)


class OutputUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    capability_id: str | None = Field(default=None, min_length=1, max_length=64)
    region: str | None = Field(default=None, min_length=1, max_length=64)
    config: dict[str, Any] | None = None

    @field_validator("name", "region", "capability_id")
    @classmethod
    def strip_optional_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _strip_required(value)


class OutputRead(BaseModel):
    id: UUID
    name: str
    capability: CapabilitySummary
    region: str | None
    details: dict[str, Any]
    created_at: datetime


router = APIRouter(tags=["connections"])


def _get_feed_or_404(feed_id: UUID, session: Session) -> Source:
    feed = session.scalar(
        select(Source).where(
            Source.id == feed_id, Source.capability_id.is_not(None)
        )
    )
    if feed is None or feed.is_deleted:
        raise HTTPException(status_code=404, detail="Feed not found")
    return feed


def _get_output_or_404(output_id: UUID, session: Session) -> Destination:
    output = session.scalar(
        select(Destination).where(
            Destination.id == output_id, Destination.capability_id.is_not(None)
        )
    )
    if output is None or output.is_deleted:
        raise HTTPException(status_code=404, detail="Output not found")
    return output


def _capability_summary(
    capability: Capability | None, fallback_id: str | None
) -> CapabilitySummary:
    if capability is None:
        return CapabilitySummary(
            id=fallback_id or "unknown",
            kind="unknown",
            name="Unknown capability",
            mode="unknown",
        )
    return CapabilitySummary(
        id=capability.id,
        kind=capability.kind,
        name=capability.name,
        mode=capability.mode,
    )


def _feed_read(feed: Source) -> FeedRead:
    capability = get_capability(feed.capability_id)
    return FeedRead(
        id=feed.id,
        name=feed.name,
        capability=_capability_summary(capability, feed.capability_id),
        region=feed.region,
        details=public_config(capability, feed.config),
        created_at=feed.created_at,
    )


def _output_read(output: Destination) -> OutputRead:
    capability = get_capability(output.capability_id)
    return OutputRead(
        id=output.id,
        name=output.name,
        capability=_capability_summary(capability, output.capability_id),
        region=output.region,
        details=public_config(capability, output.config),
        created_at=output.created_at,
    )


def _ensure_feed_not_in_route(feed_id: UUID, session: Session) -> None:
    from app.domains.routes import Route

    referenced = session.scalar(
        select(Route.id)
        .where(
            Route.is_deleted.is_(False),
            or_(Route.feed_id == feed_id, Route.redundant_feed_id == feed_id),
        )
        .limit(1)
    )
    if referenced is not None:
        raise HTTPException(status_code=409, detail="Feed is used by a route")


def _ensure_output_not_in_route(output_id: UUID, session: Session) -> None:
    from app.domains.routes import Route, RouteOutput

    referenced = session.scalar(
        select(RouteOutput.route_id)
        .join(Route, Route.id == RouteOutput.route_id)
        .where(
            Route.is_deleted.is_(False),
            or_(
                RouteOutput.output_id == output_id,
                RouteOutput.redundant_output_id == output_id,
            ),
        )
        .limit(1)
    )
    if referenced is not None:
        raise HTTPException(status_code=409, detail="Output is used by a route")


def _ensure_no_active_delivery(field, resource_id: UUID, session: Session, label: str) -> None:
    from app.domains.deliveries import Delivery, DeliveryState

    active_states = [
        DeliveryState.PENDING.value,
        DeliveryState.STARTING.value,
        DeliveryState.IN_PROGRESS.value,
        DeliveryState.STOPPING.value,
    ]
    active_delivery = session.scalar(
        select(Delivery.id).where(Delivery.state.in_(active_states), field == resource_id).limit(1)
    )
    if active_delivery is not None:
        raise HTTPException(status_code=409, detail=f"{label} is currently in use")


@router.post("/v1/feeds", response_model=FeedRead, status_code=status.HTTP_201_CREATED)
def create_feed(payload: FeedCreate, session: Session = Depends(get_session)) -> FeedRead:
    capability = _validated_capability(
        payload.capability_id, "transport", payload.region, payload.config
    )
    feed = Source(
        name=payload.name,
        capability_id=capability.id,
        source_type=capability.legacy_source_type or capability.id,
        region=payload.region,
        config=_merge_config(capability, payload.config),
    )
    session.add(feed)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Feed name already exists") from error
    session.refresh(feed)
    return _feed_read(feed)


@router.get("/v1/feeds", response_model=list[FeedRead])
def list_feeds(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[FeedRead]:
    statement = (
        select(Source)
        .where(Source.is_deleted.is_(False), Source.capability_id.is_not(None))
        .order_by(Source.name)
        .limit(limit)
        .offset(offset)
    )
    return [_feed_read(feed) for feed in session.scalars(statement).all()]


@router.get("/v1/feeds/{feed_id}", response_model=FeedRead)
def get_feed(feed_id: UUID, session: Session = Depends(get_session)) -> FeedRead:
    return _feed_read(_get_feed_or_404(feed_id, session))


@router.patch("/v1/feeds/{feed_id}", response_model=FeedRead)
def update_feed(
    feed_id: UUID, payload: FeedUpdate, session: Session = Depends(get_session)
) -> FeedRead:
    feed = _get_feed_or_404(feed_id, session)
    updates = payload.model_dump(exclude_unset=True)

    capability_id = updates.get("capability_id", feed.capability_id)
    if capability_id is None:
        raise HTTPException(status_code=422, detail="Feed requires a capability_id")
    region = updates.get("region", feed.region)
    config = updates["config"] if "config" in updates else feed.config
    capability = _validated_capability(capability_id, "transport", region, config)

    for field, value in updates.items():
        setattr(feed, field, value)
    feed.config = _merge_config(capability, config)
    feed.source_type = capability.legacy_source_type or capability.id
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Feed name already exists") from error
    session.refresh(feed)
    return _feed_read(feed)


@router.delete("/v1/feeds/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(feed_id: UUID, session: Session = Depends(get_session)) -> None:
    feed = _get_feed_or_404(feed_id, session)
    from app.domains.deliveries import Delivery

    _ensure_no_active_delivery(Delivery.source_id, feed_id, session, "Feed")
    _ensure_no_active_delivery(Delivery.redundant_source_id, feed_id, session, "Feed")
    _ensure_feed_not_in_route(feed_id, session)
    feed.is_deleted = True
    session.commit()


@router.post("/v1/outputs", response_model=OutputRead, status_code=status.HTTP_201_CREATED)
def create_output(payload: OutputCreate, session: Session = Depends(get_session)) -> OutputRead:
    capability = _validated_capability(
        payload.capability_id, "format", payload.region, payload.config
    )
    output = Destination(
        name=payload.name,
        capability_id=capability.id,
        destination_type=capability.legacy_destination_type or capability.id,
        region=payload.region,
        config=_merge_config(capability, payload.config),
    )
    session.add(output)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Output name already exists") from error
    session.refresh(output)
    return _output_read(output)


@router.get("/v1/outputs", response_model=list[OutputRead])
def list_outputs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[OutputRead]:
    statement = (
        select(Destination)
        .where(
            Destination.is_deleted.is_(False),
            Destination.capability_id.is_not(None),
        )
        .order_by(Destination.name)
        .limit(limit)
        .offset(offset)
    )
    return [_output_read(output) for output in session.scalars(statement).all()]


@router.get("/v1/outputs/{output_id}", response_model=OutputRead)
def get_output(output_id: UUID, session: Session = Depends(get_session)) -> OutputRead:
    return _output_read(_get_output_or_404(output_id, session))


@router.patch("/v1/outputs/{output_id}", response_model=OutputRead)
def update_output(
    output_id: UUID, payload: OutputUpdate, session: Session = Depends(get_session)
) -> OutputRead:
    output = _get_output_or_404(output_id, session)
    updates = payload.model_dump(exclude_unset=True)

    capability_id = updates.get("capability_id", output.capability_id)
    if capability_id is None:
        raise HTTPException(status_code=422, detail="Output requires a capability_id")
    region = updates.get("region", output.region)
    config = updates["config"] if "config" in updates else output.config
    capability = _validated_capability(capability_id, "format", region, config)

    for field, value in updates.items():
        setattr(output, field, value)
    output.config = _merge_config(capability, config)
    output.destination_type = capability.legacy_destination_type or capability.id
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Output name already exists") from error
    session.refresh(output)
    return _output_read(output)


@router.delete("/v1/outputs/{output_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_output(output_id: UUID, session: Session = Depends(get_session)) -> None:
    output = _get_output_or_404(output_id, session)
    from app.domains.deliveries import Delivery

    _ensure_no_active_delivery(Delivery.destination_id, output_id, session, "Output")
    _ensure_no_active_delivery(Delivery.redundant_destination_id, output_id, session, "Output")
    _ensure_output_not_in_route(output_id, session)
    output.is_deleted = True
    session.commit()
