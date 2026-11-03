"""Routes: the single customer scheduling object.

A route fans out to N outputs. P0 creates one Delivery per output leg, unchanged from
the existing kernel (decision D2). The route keeps desired vs observed state separate;
observed state is always derived from its deliveries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    or_,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.capabilities.catalog import (
    Capability,
    QualityPreset,
    get_capability,
    get_preset,
    region_availability,
)
from app.capabilities.planner import PlanLeg, resolve_plan
from app.database import Base, CreatedAtMixin, UTCDateTime, get_session
from app.domains.content import Content
from app.domains.deliveries import (
    Delivery,
    DeliveryState,
    _add_delivery_with_events,
    _lock_delivery_table,
    apply_delivery_stop,
    find_overlapping_delivery,
)
from app.domains.destinations import Destination
from app.domains.route_events import (
    RouteEventLevel,
    RouteEventType,
    emit_route_event,
    route_event_to_dict,
)
from app.domains.sources import Source


class Resilience(str, Enum):
    SINGLE = "single"
    REDUNDANT = "redundant"
    GEO_REDUNDANT = "geo-redundant"


class DesiredState(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


class ObservedState(str, Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    LIVE = "live"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class Route(Base, CreatedAtMixin):
    __tablename__ = "route"
    __table_args__ = (
        CheckConstraint(
            "planned_end IS NULL OR planned_end > planned_start",
            name="ck_route_end_after_start",
        ),
        CheckConstraint(
            "(is_open_ended AND planned_end IS NULL) OR "
            "(NOT is_open_ended AND planned_end IS NOT NULL)",
            name="ck_route_open_ended_schedule",
        ),
        CheckConstraint(
            "resilience = 'single' OR redundant_feed_id IS NOT NULL",
            name="ck_route_redundant_feed_required",
        ),
        CheckConstraint(
            "resilience IN ('single', 'redundant', 'geo-redundant')",
            name="ck_route_resilience",
        ),
        Index("ix_route_desired_state_planned_start", "desired_state", "planned_start"),
        Index("ix_route_content_id", "content_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    content_id: Mapped[UUID] = mapped_column(
        ForeignKey("content.id", ondelete="RESTRICT"), nullable=False
    )
    feed_id: Mapped[UUID] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT"), nullable=False
    )
    redundant_feed_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source.id", ondelete="RESTRICT")
    )
    quality_preset: Mapped[str] = mapped_column(String(32), nullable=False)
    resilience: Mapped[str] = mapped_column(String(16), nullable=False)
    desired_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DesiredState.DRAFT.value
    )
    observed_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ObservedState.PENDING.value
    )
    health: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Health.UNKNOWN.value
    )
    planned_start: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    planned_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_open_ended: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    encrypted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    resolved_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    is_deleted: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )


class RouteOutput(Base):
    __tablename__ = "route_output"
    __table_args__ = (
        CheckConstraint(
            "redundant_output_id IS NULL OR redundant_output_id <> output_id",
            name="ck_route_output_distinct",
        ),
        Index("ix_route_output_output_id", "output_id"),
    )

    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), primary_key=True
    )
    output_id: Mapped[UUID] = mapped_column(
        ForeignKey("destination.id", ondelete="RESTRICT"), primary_key=True
    )
    redundant_output_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("destination.id", ondelete="RESTRICT")
    )


class IdempotencyKey(Base, CreatedAtMixin):
    __tablename__ = "idempotency_key"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("route.id", ondelete="CASCADE"), nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class RouteIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: UUID
    feed_id: UUID
    redundant_feed_id: UUID | None = None
    quality_preset: str = Field(min_length=1, max_length=32)
    resilience: Resilience
    output_ids: list[UUID] = Field(min_length=1)
    redundant_output_ids: list[UUID | None] | None = None
    planned_start: datetime
    planned_end: datetime | None = None
    is_open_ended: bool = False
    encrypted: bool = False

    @model_validator(mode="after")
    def validate_intent(self):
        if len(set(self.output_ids)) != len(self.output_ids):
            raise ValueError("output_ids must be unique")
        for value in (self.planned_start, self.planned_end):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("schedule timestamps must include a timezone")
        if self.is_open_ended == (self.planned_end is not None):
            raise ValueError("open-ended routes must omit planned_end; others require it")
        if self.redundant_output_ids is not None and len(
            self.redundant_output_ids
        ) != len(self.output_ids):
            raise ValueError("redundant_output_ids must align with output_ids")
        return self


class RouteCheck(BaseModel):
    level: str
    code: str
    message: str


class PlanSummary(BaseModel):
    """Customer-safe view of a resolved plan.

    No provider, region, workflow, resource or codec detail: the customer approves
    *what* and *when*, while the full plan stays on the operator surface.
    """

    effective_quality_preset: str | None
    estimated_start: str | None
    output_count: int
    resilience: Resilience
    audio_tracks: int | None


class DryRunResponse(BaseModel):
    feasible: bool
    checks: list[RouteCheck]
    effective_quality_preset: str | None
    plan_summary: PlanSummary | None


class RouteOutputRead(BaseModel):
    output_id: UUID
    redundant_output_id: UUID | None


class RouteRead(BaseModel):
    id: UUID
    content_id: UUID
    feed_id: UUID
    redundant_feed_id: UUID | None
    quality_preset: str
    resilience: Resilience
    desired_state: DesiredState
    observed_state: ObservedState
    health: Health
    planned_start: datetime
    planned_end: datetime | None
    is_open_ended: bool
    encrypted: bool
    stop_reason: str | None
    outputs: list[RouteOutputRead]
    plan_summary: PlanSummary
    created_at: datetime


class RouteEventRead(BaseModel):
    id: UUID
    route_id: UUID
    event_type: str
    level: str
    message: str
    metadata: dict[str, Any]
    created_at: datetime


router = APIRouter(prefix="/v1/routes", tags=["routes"])


@dataclass
class Evaluation:
    checks: list[RouteCheck] = field(default_factory=list)
    content: Content | None = None
    feed: Source | None = None
    redundant_feed: Source | None = None
    legs: list[tuple[Destination | None, Destination | None]] = field(default_factory=list)
    preset: QualityPreset | None = None
    effective_preset: QualityPreset | None = None
    plan: dict[str, Any] | None = None

    @property
    def feasible(self) -> bool:
        return not any(check.level == "error" for check in self.checks)


def _check(checks: list[RouteCheck], level: str, code: str, message: str) -> None:
    checks.append(RouteCheck(level=level, code=code, message=message))


def _active_resource(session: Session, model, resource_id: UUID):
    resource = session.get(model, resource_id)
    if resource is None or resource.is_deleted:
        return None
    return resource


def _feed_capability(
    session: Session, feed: Source | None, checks: list[RouteCheck], label: str
) -> Capability | None:
    if feed is None:
        return None
    capability = get_capability(feed.capability_id)
    if capability is None:
        _check(
            checks,
            "error",
            "feed-capability-missing",
            f"{label} has no catalog capability",
        )
        return None
    if capability.kind != "transport":
        _check(
            checks,
            "error",
            "feed-capability-wrong-kind",
            f"{label} capability {capability.id} is not a transport",
        )
        return None
    availability = region_availability(capability, feed.region)
    if not availability.available:
        _check(
            checks,
            "error",
            "feed-region-unavailable",
            availability.reason
            or f"{label} capability {capability.id} is unavailable in {feed.region}",
        )
    if capability.legacy_source_type is None:
        _check(
            checks,
            "error",
            "capability-not-executable",
            f"{label} capability {capability.id} is catalog-visible but not "
            "available to schedule yet",
        )
    return capability


def _output_capability(
    session: Session, output: Destination, checks: list[RouteCheck]
) -> Capability | None:
    capability = get_capability(output.capability_id)
    if capability is None:
        _check(
            checks,
            "error",
            "output-capability-missing",
            "Output has no catalog capability",
        )
        return None
    if capability.kind != "format":
        _check(
            checks,
            "error",
            "output-capability-wrong-kind",
            f"Output {output.name} capability {capability.id} is not a format",
        )
        return None
    availability = region_availability(capability, output.region)
    if not availability.available:
        _check(
            checks,
            "error",
            "output-region-unavailable",
            availability.reason
            or f"Output {output.name} capability {capability.id} is unavailable in "
            f"{output.region}",
        )
    if capability.legacy_destination_type is None:
        _check(
            checks,
            "error",
            "capability-not-executable",
            f"Output {output.name} capability {capability.id} is catalog-visible but "
            "not available to schedule yet",
        )
    return capability


def evaluate_intent(session: Session, intent: RouteIntent) -> Evaluation:
    """Run §6.1 checks and derive a plan. Persists nothing."""

    checks: list[RouteCheck] = []
    evaluation = Evaluation(checks=checks)

    content = _active_resource(session, Content, intent.content_id)
    if content is None:
        _check(checks, "error", "content-not-found", "Content not found")
    evaluation.content = content

    feed = _active_resource(session, Source, intent.feed_id)
    if feed is None:
        _check(checks, "error", "feed-not-found", "Feed not found")
    evaluation.feed = feed

    redundant_feed = None
    if intent.redundant_feed_id is not None:
        redundant_feed = _active_resource(session, Source, intent.redundant_feed_id)
        if redundant_feed is None:
            _check(
                checks, "error", "redundant-feed-not-found", "Redundant feed not found"
            )
    evaluation.redundant_feed = redundant_feed

    preset = get_preset(intent.quality_preset)
    if preset is None:
        _check(
            checks,
            "error",
            "preset-not-found",
            f"Unknown quality preset '{intent.quality_preset}'",
        )
    evaluation.preset = preset

    now = datetime.now(timezone.utc)
    if intent.planned_start <= now:
        _check(
            checks,
            "error",
            "start-in-past",
            "planned_start must be in the future for a new route",
        )
    if intent.planned_end is not None and intent.planned_end <= intent.planned_start:
        _check(
            checks,
            "error",
            "invalid-schedule",
            "planned_end must be later than planned_start",
        )
    if (
        intent.planned_end is not None
        and intent.planned_end > intent.planned_start
        and intent.planned_end - intent.planned_start < timedelta(minutes=10)
    ):
        _check(
            checks,
            "error",
            "schedule-too-short",
            "A bounded route must run for at least 10 minutes",
        )

    if intent.resilience == Resilience.SINGLE:
        if intent.redundant_feed_id is not None or any(
            value is not None for value in (intent.redundant_output_ids or [])
        ):
            _check(
                checks,
                "error",
                "unexpected-redundancy",
                "single resilience must not set redundant feeds or outputs",
            )
    elif intent.redundant_feed_id == intent.feed_id:
        _check(
            checks,
            "error",
            "duplicate-redundant-feed",
            "Redundant feed must be different from the primary feed",
        )

    feed_capability = _feed_capability(session, feed, checks, "Feed")
    redundant_feed_capability = _feed_capability(
        session, redundant_feed, checks, "Redundant feed"
    )

    outputs: list[Destination] = []
    for output_id in intent.output_ids:
        output = _active_resource(session, Destination, output_id)
        if output is None:
            _check(
                checks,
                "error",
                "output-not-found",
                f"Output {output_id} not found",
            )
            outputs.append(None)  # type: ignore[arg-type]
        else:
            outputs.append(output)

    redundant_output_ids = intent.redundant_output_ids or [None] * len(intent.output_ids)
    all_destination_ids = list(intent.output_ids) + [
        value for value in redundant_output_ids if value is not None
    ]
    if len(all_destination_ids) != len(set(all_destination_ids)):
        _check(
            checks,
            "error",
            "duplicate-output-leg",
            "Each output and redundant output must appear on exactly one leg",
        )
    legs: list[tuple[Destination | None, Destination | None]] = []
    for index, output in enumerate(outputs):
        redundant_output = None
        redundant_id = redundant_output_ids[index]
        if redundant_id is not None:
            redundant_output = _active_resource(session, Destination, redundant_id)
            if redundant_output is None:
                _check(
                    checks,
                    "error",
                    "redundant-output-not-found",
                    f"Redundant output {redundant_id} not found",
                )
        legs.append((output, redundant_output))
    evaluation.legs = legs

    output_capabilities: list[Capability | None] = []
    for output, _redundant in legs:
        output_capabilities.append(
            _output_capability(session, output, checks) if output else None
        )
    for _output_index, (_output, redundant) in enumerate(legs):
        if redundant is not None:
            _output_capability(session, redundant, checks)

    # Per-capability fan-out limit.
    counts: dict[str, int] = {}
    for capability in output_capabilities:
        if capability is not None:
            counts[capability.id] = counts.get(capability.id, 0) + 1
    for capability_id, count in counts.items():
        capability = get_capability(capability_id)
        max_outputs = capability.constraints.get("max_outputs") if capability else None
        if isinstance(max_outputs, int) and count > max_outputs:
            _check(
                checks,
                "error",
                "too-many-outputs",
                f"Capability {capability_id} allows at most {max_outputs} outputs",
            )

    effective_preset = preset
    if preset is not None and not preset.transcode:
        if any(
            capability is not None
            and capability.constraints.get("requires_transcode")
            for capability in output_capabilities
        ):
            upgraded = get_preset("1080p")
            if upgraded is not None:
                effective_preset = upgraded
            _check(
                checks,
                "warn",
                "transcode-required",
                "At least one output requires transcoding; the effective preset is "
                f"upgraded from {preset.id} to {effective_preset.id}",
            )
    evaluation.effective_preset = effective_preset

    if intent.resilience == Resilience.GEO_REDUNDANT:
        # Geo-redundancy is expressed as separate primary output legs in two regions.
        # A redundant output is a same-region/different-AZ twin (the execution kernel
        # rejects cross-region redundant pairs), so it does not add a region.
        regions = {
            output.region for output in outputs if output is not None and output.region
        }
        if len(regions) < 2:
            _check(
                checks,
                "error",
                "geo-redundancy-requires-two-regions",
                "geo-redundant routes require outputs in at least two regions",
            )

    if intent.resilience != Resilience.SINGLE:
        if redundant_feed is None:
            _check(
                checks,
                "error",
                "redundant-feed-required",
                "redundant and geo-redundant routes require a redundant feed",
            )
        for output, redundant_output in legs:
            if redundant_output is None:
                name = output.name if output is not None else "an output"
                _check(
                    checks,
                    "error",
                    "redundant-output-required",
                    f"{name} requires a redundant output for {intent.resilience} "
                    "resilience",
                )
                continue
            if output is None:
                continue
            # The execution kernel builds redundant pairs inside one region, in
            # different availability zones. Cross-region redundancy would be a
            # different delivery shape and must not be accepted here.
            if redundant_output.region != output.region:
                _check(
                    checks,
                    "error",
                    "redundant-output-region-mismatch",
                    f"{output.name}: redundant output must be in the same region "
                    f"({output.region})",
                )
            elif (
                redundant_output.availability_zone
                and output.availability_zone
                and redundant_output.availability_zone == output.availability_zone
            ):
                _check(
                    checks,
                    "error",
                    "redundant-output-same-az",
                    f"{output.name}: redundant output must use a different "
                    "availability zone",
                )

    if redundant_feed is not None and feed is not None:
        if redundant_feed.region != feed.region:
            _check(
                checks,
                "error",
                "redundant-feed-region-mismatch",
                "Redundant feed must be in the same region as the primary feed",
            )
        elif (
            redundant_feed.availability_zone
            and feed.availability_zone
            and redundant_feed.availability_zone == feed.availability_zone
        ):
            _check(
                checks,
                "error",
                "redundant-feed-same-az",
                "Redundant feed must use a different availability zone from the "
                "primary feed",
            )

    destination_ids = set(intent.output_ids)
    destination_ids.update(
        value for value in (intent.redundant_output_ids or []) if value is not None
    )
    if find_overlapping_delivery(
        session, destination_ids, intent.planned_start, intent.planned_end
    ) is not None:
        _check(
            checks,
            "error",
            "output-conflict",
            "Output is already booked for the requested window",
        )

    if preset is not None and preset.id == "uhd" and intent.resilience == Resilience.GEO_REDUNDANT:
        _check(
            checks,
            "info",
            "approval-required",
            "UHD geo-redundant routes require a capacity approval before scheduling",
        )

    if content is not None and content.content_type == "event" and intent.is_open_ended:
        _check(
            checks,
            "error",
            "event-open-ended",
            "Events must have an end time; only channels may be open-ended",
        )

    if evaluation.feasible and preset is not None and effective_preset is not None:
        feed_ok = feed is not None and feed_capability is not None
        legs_ok = all(output is not None for output, _ in legs)
        if feed_ok and legs_ok:
            plan_legs: list[PlanLeg] = []
            for (output, redundant_output), capability in zip(legs, output_capabilities):
                assert output is not None and capability is not None
                redundant_capability = (
                    get_capability(redundant_output.capability_id)
                    if redundant_output is not None
                    else None
                )
                plan_legs.append(
                    PlanLeg(
                        output_id=str(output.id),
                        capability=capability,
                        region=output.region or feed.region,
                        redundant_capability=redundant_capability,
                        redundant_region=redundant_output.region
                        if redundant_output is not None
                        else None,
                    )
                )
            evaluation.plan = resolve_plan(
                feed=feed_capability,
                feed_region=feed.region,
                redundant_feed=redundant_feed_capability,
                redundant_feed_region=redundant_feed.region
                if redundant_feed is not None
                else None,
                legs=plan_legs,
                preset=preset,
                effective_preset=effective_preset,
                resilience=intent.resilience.value,
            )
    return evaluation


def _route_outputs(session: Session, route_id: UUID) -> list[RouteOutput]:
    return list(
        session.scalars(
            select(RouteOutput)
            .where(RouteOutput.route_id == route_id)
            .order_by(RouteOutput.output_id)
        ).all()
    )


def _plan_summary(
    plan: dict[str, Any] | None,
    *,
    output_count: int,
    resilience: str,
    fallback_preset: str,
) -> PlanSummary:
    """Project a resolved plan onto the customer-safe summary."""

    plan = plan or {}
    return PlanSummary(
        effective_quality_preset=plan.get("effective_quality_preset")
        or fallback_preset,
        estimated_start=plan.get("estimated_start"),
        output_count=output_count,
        resilience=Resilience(resilience),
        audio_tracks=plan.get("audio_tracks"),
    )


def serialize_route(session: Session, route: Route) -> RouteRead:
    outputs = _route_outputs(session, route.id)
    return RouteRead(
        id=route.id,
        content_id=route.content_id,
        feed_id=route.feed_id,
        redundant_feed_id=route.redundant_feed_id,
        quality_preset=route.quality_preset,
        resilience=Resilience(route.resilience),
        desired_state=DesiredState(route.desired_state),
        observed_state=ObservedState(route.observed_state),
        health=Health(route.health),
        planned_start=route.planned_start,
        planned_end=route.planned_end,
        is_open_ended=route.is_open_ended,
        encrypted=route.encrypted,
        stop_reason=route.stop_reason,
        outputs=[
            RouteOutputRead(
                output_id=leg.output_id, redundant_output_id=leg.redundant_output_id
            )
            for leg in outputs
        ],
        plan_summary=_plan_summary(
            route.resolved_plan,
            output_count=len(outputs),
            resilience=route.resilience,
            fallback_preset=route.quality_preset,
        ),
        created_at=route.created_at,
    )


def _get_route_or_404(route_id: UUID, session: Session) -> Route:
    route = session.get(Route, route_id)
    if route is None or route.is_deleted:
        raise HTTPException(status_code=404, detail="Route not found")
    return route


def _intent_fingerprint(intent: RouteIntent) -> str:
    """Digest the validated intent so an Idempotency-Key is bound to its request body."""
    payload = json.dumps(
        intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Idempotency-Key was already used for a different request",
    )


@router.post("/dry-run", response_model=DryRunResponse)
def dry_run(intent: RouteIntent, session: Session = Depends(get_session)) -> DryRunResponse:
    evaluation = evaluate_intent(session, intent)
    return DryRunResponse(
        feasible=evaluation.feasible,
        checks=evaluation.checks,
        effective_quality_preset=(
            evaluation.effective_preset.id if evaluation.effective_preset else None
        ),
        plan_summary=(
            _plan_summary(
                evaluation.plan,
                output_count=len(evaluation.legs),
                resilience=intent.resilience.value,
                fallback_preset=intent.quality_preset,
            )
            if evaluation.plan is not None
            else None
        ),
    )


@router.post("", response_model=RouteRead, status_code=status.HTTP_201_CREATED)
def create_route(
    intent: RouteIntent,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: Session = Depends(get_session),
) -> RouteRead:
    fingerprint = _intent_fingerprint(intent)
    if idempotency_key:
        existing = session.get(IdempotencyKey, idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise _idempotency_conflict()
            return serialize_route(session, _get_route_or_404(existing.route_id, session))

    evaluation = evaluate_intent(session, intent)
    if not evaluation.feasible:
        conflict = any(check.code == "output-conflict" for check in evaluation.checks)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT if conflict else 422,
            detail={
                "message": "Route is not feasible",
                "checks": [check.model_dump() for check in evaluation.checks],
            },
        )

    # Re-check contention under the same lock pattern the delivery kernel uses.
    _lock_delivery_table(session)
    destination_ids = set(intent.output_ids)
    destination_ids.update(
        value for value in (intent.redundant_output_ids or []) if value is not None
    )
    if (
        find_overlapping_delivery(
            session, destination_ids, intent.planned_start, intent.planned_end
        )
        is not None
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Route is not feasible",
                "checks": [
                    {
                        "level": "error",
                        "code": "output-conflict",
                        "message": "Output is already booked for the requested window",
                    }
                ],
            },
        )

    assert evaluation.preset is not None and evaluation.effective_preset is not None
    route = Route(
        content_id=intent.content_id,
        feed_id=intent.feed_id,
        redundant_feed_id=intent.redundant_feed_id,
        quality_preset=intent.quality_preset,
        resilience=intent.resilience.value,
        desired_state=DesiredState.SCHEDULED.value,
        observed_state=ObservedState.PENDING.value,
        health=Health.UNKNOWN.value,
        planned_start=intent.planned_start,
        planned_end=intent.planned_end,
        is_open_ended=intent.is_open_ended,
        encrypted=intent.encrypted,
        resolved_plan=evaluation.plan,
        plan_version=(evaluation.plan or {}).get("version", 1),
    )
    session.add(route)
    session.flush()

    redundant_output_ids = intent.redundant_output_ids or [None] * len(intent.output_ids)
    for output_id, redundant_output_id in zip(intent.output_ids, redundant_output_ids):
        session.add(
            RouteOutput(
                route_id=route.id,
                output_id=output_id,
                redundant_output_id=(
                    redundant_output_id
                    if intent.resilience != Resilience.SINGLE
                    else None
                ),
            )
        )

    for output_id, redundant_output_id in zip(intent.output_ids, redundant_output_ids):
        _add_delivery_with_events(
            session,
            source_id=route.feed_id,
            destination_id=output_id,
            redundant_source_id=(
                route.redundant_feed_id
                if intent.resilience != Resilience.SINGLE
                else None
            ),
            redundant_destination_id=(
                redundant_output_id
                if intent.resilience != Resilience.SINGLE
                else None
            ),
            planned_start=route.planned_start,
            planned_end=route.planned_end,
            is_open_ended=route.is_open_ended,
            route_id=route.id,
            media_options={
                "route_id": str(route.id),
                "requested_quality_preset": intent.quality_preset,
                "effective_quality_preset": evaluation.effective_preset.id,
                "resilience": intent.resilience.value,
                "encrypted": intent.encrypted,
            },
        )

    emit_route_event(
        session,
        route.id,
        RouteEventType.SCHEDULED,
        level=RouteEventLevel.SUCCESS,
        message="Route scheduled",
        metadata={
            "planned_start": route.planned_start.isoformat(),
            "planned_end": route.planned_end.isoformat() if route.planned_end else None,
            "output_count": len(intent.output_ids),
            "resilience": route.resilience,
        },
    )

    if idempotency_key:
        session.add(
            IdempotencyKey(
                key=idempotency_key,
                route_id=route.id,
                request_fingerprint=fingerprint,
            )
        )

    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if idempotency_key:
            existing = session.get(IdempotencyKey, idempotency_key)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise _idempotency_conflict() from error
                return serialize_route(
                    session, _get_route_or_404(existing.route_id, session)
                )
        raise HTTPException(
            status_code=409, detail="Route could not be created"
        ) from error

    session.refresh(route)
    return serialize_route(session, route)


@router.get("", response_model=list[RouteRead])
def list_routes(
    state: str | None = Query(default=None),
    content_id: UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[RouteRead]:
    statement = (
        select(Route)
        .where(Route.is_deleted.is_(False))
        .order_by(Route.planned_start)
        .limit(limit)
        .offset(offset)
    )
    if state is not None:
        statement = statement.where(
            or_(Route.desired_state == state, Route.observed_state == state)
        )
    if content_id is not None:
        statement = statement.where(Route.content_id == content_id)
    if from_ is not None:
        statement = statement.where(Route.planned_start >= from_)
    if to is not None:
        statement = statement.where(Route.planned_start <= to)
    return [
        serialize_route(session, route) for route in session.scalars(statement).all()
    ]


@router.get("/{route_id}", response_model=RouteRead)
def get_route(route_id: UUID, session: Session = Depends(get_session)) -> RouteRead:
    return serialize_route(session, _get_route_or_404(route_id, session))


@router.post("/{route_id}/cancel", response_model=RouteRead)
def cancel_route(route_id: UUID, session: Session = Depends(get_session)) -> RouteRead:
    route = _get_route_or_404(route_id, session)
    if route.desired_state == DesiredState.CANCELLED.value:
        return serialize_route(session, route)

    deliveries = list(
        session.scalars(select(Delivery).where(Delivery.route_id == route.id)).all()
    )
    if any(
        delivery.state
        in (
            DeliveryState.STARTING.value,
            DeliveryState.IN_PROGRESS.value,
            DeliveryState.STOPPING.value,
        )
        for delivery in deliveries
    ):
        raise HTTPException(
            status_code=409,
            detail="Route has started; stop it instead of cancelling",
        )

    for delivery in deliveries:
        if delivery.state == DeliveryState.PENDING.value:
            apply_delivery_stop(session, delivery, reason="CANCELLED")

    route.desired_state = DesiredState.CANCELLED.value
    route.observed_state = ObservedState.COMPLETED.value
    route.health = Health.UNKNOWN.value
    route.stop_reason = "CANCELLED"
    emit_route_event(
        session,
        route.id,
        RouteEventType.CANCELLED,
        level=RouteEventLevel.NEUTRAL,
        message="Route cancelled before start",
    )
    session.commit()
    session.refresh(route)
    return serialize_route(session, route)


@router.post("/{route_id}/stop", response_model=RouteRead)
def stop_route(route_id: UUID, session: Session = Depends(get_session)) -> RouteRead:
    route = _get_route_or_404(route_id, session)
    deliveries = list(
        session.scalars(select(Delivery).where(Delivery.route_id == route.id)).all()
    )
    active_states = {
        DeliveryState.PENDING.value,
        DeliveryState.STARTING.value,
        DeliveryState.IN_PROGRESS.value,
        DeliveryState.STOPPING.value,
    }
    if route.desired_state == DesiredState.CANCELLED.value:
        # A cancelled route is already terminal; stopping it must not rewrite the
        # customer's intent back to "stopped".
        return serialize_route(session, route)
    if route.desired_state == DesiredState.STOPPED.value and not any(
        delivery.state in active_states for delivery in deliveries
    ):
        return serialize_route(session, route)

    was_stopped = route.desired_state == DesiredState.STOPPED.value
    for delivery in deliveries:
        if delivery.state in active_states:
            apply_delivery_stop(session, delivery, reason="STOPPED")

    route.desired_state = DesiredState.STOPPED.value
    route.stop_reason = "STOPPED"
    if not was_stopped:
        emit_route_event(
            session,
            route.id,
            RouteEventType.STOPPED,
            level=RouteEventLevel.NEUTRAL,
            message="Route stopped by operator",
        )

    from app.domains.route_lifecycle import sync_route_lifecycle

    # Keep observed state derived after the action; the STOPPED reason suppresses a
    # duplicate route.completed event.
    sync_route_lifecycle(session, route)
    session.commit()
    session.refresh(route)
    return serialize_route(session, route)


@router.get("/{route_id}/events", response_model=list[RouteEventRead])
def list_route_events(
    route_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[RouteEventRead]:
    _get_route_or_404(route_id, session)
    from app.domains.route_events import RouteEvent

    events = session.scalars(
        select(RouteEvent)
        .where(RouteEvent.route_id == route_id)
        .order_by(RouteEvent.created_at, RouteEvent.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [RouteEventRead(**route_event_to_dict(event, public=True)) for event in events]


@router.get("/{route_id}/plan")
def get_route_plan(route_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Customer-safe plan summary. Full plans live at ``/internal/plans/{route_id}``."""

    route = _get_route_or_404(route_id, session)
    outputs = _route_outputs(session, route.id)
    summary = _plan_summary(
        route.resolved_plan,
        output_count=len(outputs),
        resilience=route.resilience,
        fallback_preset=route.quality_preset,
    )
    return {"route_id": route.id, "plan_summary": summary.model_dump()}
