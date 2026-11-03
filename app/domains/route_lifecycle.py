"""Map delivery state to route state.

The sync is derived, not counter-based, so re-running it is idempotent. It emits a
customer route event only when the derived observed state actually changes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domains.deliveries import Delivery, DeliveryState
from app.domains.route_events import RouteEventLevel, RouteEventType, emit_route_event
from app.domains.routes import Route


def derive_route_state(deliveries: list[Delivery]) -> tuple[str, str]:
    states = {delivery.state for delivery in deliveries}
    if not states:
        return "pending", "unknown"
    if DeliveryState.ERROR.value in states:
        return "failed", "degraded"
    if DeliveryState.IN_PROGRESS.value in states:
        return "live", "healthy"
    if DeliveryState.STOPPING.value in states:
        return "stopping", "unknown"
    if states == {DeliveryState.COMPLETE.value}:
        return "completed", "unknown"
    if states == {DeliveryState.CANCELLED.value}:
        return "completed", "unknown"
    if DeliveryState.STARTING.value in states:
        return "provisioning", "unknown"
    return "pending", "unknown"


def _all_cancelled(deliveries: list[Delivery]) -> bool:
    return bool(deliveries) and all(
        delivery.state == DeliveryState.CANCELLED.value for delivery in deliveries
    )


def _cancelled_event_for_transition(
    previous: str, observed: str
) -> tuple[RouteEventType, RouteEventLevel, str] | None:
    if observed == "completed" and previous != "completed":
        return (
            RouteEventType.CANCELLED,
            RouteEventLevel.NEUTRAL,
            "Route cancelled because every output leg was cancelled",
        )
    return None


def _event_for_transition(
    route: Route, previous: str, observed: str
) -> tuple[RouteEventType, RouteEventLevel, str] | None:
    if observed == "live" and previous != "live":
        return (
            RouteEventType.STARTED,
            RouteEventLevel.SUCCESS,
            "Route is live",
        )
    if observed == "failed" and previous != "failed":
        return (
            RouteEventType.FAILED,
            RouteEventLevel.ERROR,
            "Route failed because an output leg failed",
        )
    if observed == "completed" and previous != "completed":
        if route.stop_reason in {"STOPPED", "CANCELLED"}:
            return None
        return (
            RouteEventType.COMPLETED,
            RouteEventLevel.SUCCESS,
            "Route completed",
        )
    return None


def sync_route_lifecycle(session: Session, route: Route) -> Route:
    """Re-derive route state from its deliveries and emit at most one event."""

    deliveries = list(
        session.scalars(select(Delivery).where(Delivery.route_id == route.id)).all()
    )
    if not deliveries:
        return route

    observed, health = derive_route_state(deliveries)
    all_cancelled = _all_cancelled(deliveries)
    previous = route.observed_state
    route.observed_state = observed
    route.health = health

    if observed in {"provisioning", "live", "degraded", "stopping"}:
        if route.desired_state not in {"stopped", "cancelled"}:
            route.desired_state = "active"
    elif observed == "completed":
        if route.desired_state not in {"stopped", "cancelled"}:
            route.desired_state = "cancelled" if all_cancelled else "stopped"
        if all_cancelled:
            route.stop_reason = route.stop_reason or "CANCELLED"

    if all_cancelled:
        transition = _cancelled_event_for_transition(previous, observed)
    else:
        transition = _event_for_transition(route, previous, observed)
    if transition is not None:
        event_type, level, message = transition
        emit_route_event(
            session,
            route.id,
            event_type,
            level=level,
            message=message,
            metadata={
                "previous_state": previous,
                "observed_state": observed,
                "delivery_states": sorted(
                    {delivery.state for delivery in deliveries}
                ),
            },
        )
    return route


def sync_route_for_delivery(session: Session, delivery: Delivery) -> Route | None:
    if delivery.route_id is None:
        return None
    route = session.get(Route, delivery.route_id)
    if route is None:
        return None
    return sync_route_lifecycle(session, route)
