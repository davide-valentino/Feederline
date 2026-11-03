"""Control-room read models.

P0 implements these as thin queries over route / route_event / delivery. No new
tables and no stored aggregates.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.domains.deliveries import Delivery, DeliveryState
from app.domains.route_events import RouteEvent, route_event_to_dict
from app.domains.routes import Route, serialize_route

router = APIRouter(prefix="/v1/control-room", tags=["control room"])

LIVE_ROUTE_STATES = {"provisioning", "live", "degraded"}
ACTIVE_DELIVERY_STATES = {
    DeliveryState.PENDING.value,
    DeliveryState.STARTING.value,
    DeliveryState.IN_PROGRESS.value,
    DeliveryState.STOPPING.value,
}


@router.get("/summary")
def summary(session: Session = Depends(get_session)) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    live = session.scalar(
        select(func.count())
        .select_from(Route)
        .where(
            Route.is_deleted.is_(False),
            Route.observed_state.in_(LIVE_ROUTE_STATES),
        )
    ) or 0
    scheduled = session.scalar(
        select(func.count())
        .select_from(Route)
        .where(
            Route.is_deleted.is_(False),
            Route.desired_state == "scheduled",
            Route.planned_start > now,
        )
    ) or 0
    error = session.scalar(
        select(func.count())
        .select_from(Route)
        .where(
            Route.is_deleted.is_(False),
            or_(Route.observed_state == "failed", Route.health == "degraded"),
        )
    ) or 0

    live_routes = session.scalars(
        select(Route).where(
            Route.is_deleted.is_(False),
            Route.observed_state.in_(LIVE_ROUTE_STATES),
        )
    ).all()
    feed_ids = {
        feed_id
        for route in live_routes
        for feed_id in (route.feed_id, route.redundant_feed_id)
        if feed_id is not None
    }
    outputs_in_use = session.scalars(
        select(Delivery.destination_id).where(
            Delivery.state.in_(ACTIVE_DELIVERY_STATES),
            Delivery.route_id.is_not(None),
        )
    ).all()
    redundant_outputs_in_use = session.scalars(
        select(Delivery.redundant_destination_id).where(
            Delivery.state.in_(ACTIVE_DELIVERY_STATES),
            Delivery.redundant_destination_id.is_not(None),
            Delivery.route_id.is_not(None),
        )
    ).all()
    outputs_in_use_count = len(set(outputs_in_use) | set(redundant_outputs_in_use))

    return {
        "live_routes": live,
        "scheduled_routes": scheduled,
        "error_routes": error,
        "feeds_online": len(feed_ids),
        "outputs_in_use": outputs_in_use_count,
    }


@router.get("/activity")
def activity(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    events = session.scalars(
        select(RouteEvent)
        .order_by(RouteEvent.created_at.desc(), RouteEvent.id)
        .limit(limit)
    ).all()
    return [route_event_to_dict(event, public=True) for event in events]


@router.get("/upcoming")
def upcoming(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    routes = session.scalars(
        select(Route)
        .where(
            Route.is_deleted.is_(False),
            Route.desired_state == "scheduled",
            Route.planned_start > now,
        )
        .order_by(Route.planned_start)
        .limit(limit)
    ).all()
    return [serialize_route(session, route).model_dump() for route in routes]

