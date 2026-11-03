"""Internal operator surface (P0).

Gated by the static operator token from ``app.auth``. Every mutation writes an
``operator_audit`` row in the same transaction.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import require_operator
from app.capabilities.catalog import (
    CAPABILITIES_BY_ID,
    CATALOG_VERSION,
    PRESETS,
    capability_to_dict,
    catalog_payload,
    preset_to_dict,
)
from app.database import get_session
from app.domains.audit import OperatorAudit, record_audit
from app.domains.deliveries import (
    Delivery,
    DeliveryOutboxEvent,
    DeliveryState,
    DeliveryWorkflowRun,
    OutboxStatus,
    _queue_event,
)
from app.domains.route_events import RouteEvent
from app.domains.routes import Route, RouteIntent, RouteOutput, evaluate_intent

router = APIRouter(
    prefix="/internal",
    tags=["operator"],
    dependencies=[Depends(require_operator)],
)


def _get_delivery_or_404(delivery_id: UUID, session: Session) -> Delivery:
    delivery = session.get(Delivery, delivery_id)
    if delivery is None or delivery.state == DeliveryState.DELETED.value:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return delivery


def _route_for_delivery(session: Session, delivery: Delivery) -> Route | None:
    if delivery.route_id is None:
        return None
    return session.get(Route, delivery.route_id)


def _outbox_summary(session: Session, delivery_id: UUID) -> dict[str, int]:
    rows = session.execute(
        select(DeliveryOutboxEvent.status, func.count())
        .where(DeliveryOutboxEvent.delivery_id == delivery_id)
        .group_by(DeliveryOutboxEvent.status)
    ).all()
    summary = {
        OutboxStatus.PENDING.value: 0,
        OutboxStatus.PUBLISHED.value: 0,
        OutboxStatus.CANCELLED.value: 0,
    }
    for event_status, count in rows:
        summary[event_status] = count
    return summary


def _last_error(session: Session, delivery_id: UUID) -> str | None:
    outbox_error = session.scalar(
        select(DeliveryOutboxEvent.last_error)
        .where(
            DeliveryOutboxEvent.delivery_id == delivery_id,
            DeliveryOutboxEvent.last_error.is_not(None),
        )
        .order_by(DeliveryOutboxEvent.created_at.desc())
        .limit(1)
    )
    run_error = session.scalar(
        select(DeliveryWorkflowRun.last_error)
        .where(
            DeliveryWorkflowRun.delivery_id == delivery_id,
            DeliveryWorkflowRun.last_error.is_not(None),
        )
        .order_by(DeliveryWorkflowRun.created_at.desc())
        .limit(1)
    )
    return run_error or outbox_error


def serialize_operator_delivery(session: Session, delivery: Delivery) -> dict[str, Any]:
    route = _route_for_delivery(session, delivery)
    plan = route.resolved_plan if route is not None else None
    latest_run = session.scalar(
        select(DeliveryWorkflowRun)
        .where(DeliveryWorkflowRun.delivery_id == delivery.id)
        .order_by(DeliveryWorkflowRun.created_at.desc())
        .limit(1)
    )
    return {
        "id": delivery.id,
        "route_id": delivery.route_id,
        "source_id": delivery.source_id,
        "destination_id": delivery.destination_id,
        "redundant_source_id": delivery.redundant_source_id,
        "redundant_destination_id": delivery.redundant_destination_id,
        "state": delivery.state,
        "stop_reason": delivery.stop_reason,
        "planned_start": delivery.planned_start,
        "planned_end": delivery.planned_end,
        "is_open_ended": delivery.is_open_ended,
        "provider": (plan or {}).get("provider"),
        "workflow": (plan or {}).get("workflow"),
        "workflow_event": latest_run.event_type if latest_run is not None else None,
        "workflow_status": latest_run.status if latest_run is not None else None,
        "outbox": _outbox_summary(session, delivery.id),
        "last_error": _last_error(session, delivery.id),
        "route": (
            {
                "id": route.id,
                "desired_state": route.desired_state,
                "observed_state": route.observed_state,
                "health": route.health,
            }
            if route is not None
            else None
        ),
        "created_at": delivery.created_at,
    }


@router.get("/deliveries")
def list_operator_deliveries(
    state: DeliveryState | None = Query(default=None),
    route_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    statement = (
        select(Delivery)
        .where(Delivery.state != DeliveryState.DELETED.value)
        .order_by(Delivery.planned_start)
        .limit(limit)
        .offset(offset)
    )
    if state is not None:
        statement = statement.where(Delivery.state == state.value)
    if route_id is not None:
        statement = statement.where(Delivery.route_id == route_id)
    return [
        serialize_operator_delivery(session, delivery)
        for delivery in session.scalars(statement).all()
    ]


@router.get("/deliveries/{delivery_id}")
def get_operator_delivery(
    delivery_id: UUID, session: Session = Depends(get_session)
) -> dict[str, Any]:
    delivery = _get_delivery_or_404(delivery_id, session)
    model = serialize_operator_delivery(session, delivery)
    runs = session.scalars(
        select(DeliveryWorkflowRun)
        .where(DeliveryWorkflowRun.delivery_id == delivery_id)
        .order_by(DeliveryWorkflowRun.created_at.desc())
    ).all()
    events = session.scalars(
        select(DeliveryOutboxEvent)
        .where(DeliveryOutboxEvent.delivery_id == delivery_id)
        .order_by(DeliveryOutboxEvent.created_at)
    ).all()
    model["workflow_runs"] = [
        {
            "event_id": run.event_id,
            "delivery_id": run.delivery_id,
            "event_type": run.event_type,
            "execution_arn": run.execution_arn,
            "status": run.status,
            "resolved_plan": run.resolved_plan,
            "resource_map": run.resource_map,
            "last_error": run.last_error,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "created_at": run.created_at,
        }
        for run in runs
    ]
    model["outbox_events"] = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "status": event.status,
            "due_at": event.due_at,
            "attempts": event.attempts,
            "last_error": event.last_error,
            "published_at": event.published_at,
        }
        for event in events
    ]
    return model


@router.post("/deliveries/{delivery_id}/retry")
def retry_delivery(
    delivery_id: UUID,
    actor: str = Depends(require_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    delivery = session.scalar(
        select(Delivery).where(Delivery.id == delivery_id).with_for_update()
    )
    if delivery is None or delivery.state == DeliveryState.DELETED.value:
        raise HTTPException(status_code=404, detail="Delivery not found")

    if delivery.state == DeliveryState.ERROR.value:
        delivery.state = DeliveryState.STARTING.value
        _queue_event(session, delivery, "PipelineLink", datetime.now(timezone.utc))
        record_audit(
            session,
            actor=actor,
            action="delivery.retry",
            target_type="delivery",
            target_id=delivery.id,
            request_metadata={"state": DeliveryState.ERROR.value},
        )
        from app.domains.route_lifecycle import sync_route_for_delivery

        sync_route_for_delivery(session, delivery)
        session.commit()
        session.refresh(delivery)
        return serialize_operator_delivery(session, delivery)

    stuck_event = session.scalar(
        select(DeliveryOutboxEvent)
        .where(
            DeliveryOutboxEvent.delivery_id == delivery_id,
            DeliveryOutboxEvent.status == OutboxStatus.PENDING.value,
            or_(
                DeliveryOutboxEvent.attempts > 0,
                DeliveryOutboxEvent.last_error.is_not(None),
            ),
        )
        .order_by(DeliveryOutboxEvent.created_at)
        .limit(1)
    )
    if stuck_event is None:
        raise HTTPException(
            status_code=409, detail="Delivery has no failed event to retry"
        )
    stuck_event.due_at = datetime.now(timezone.utc)
    record_audit(
        session,
        actor=actor,
        action="outbox.retry",
        target_type="delivery_outbox",
        target_id=stuck_event.id,
        request_metadata={"delivery_id": str(delivery_id)},
    )
    session.commit()
    session.refresh(delivery)
    return serialize_operator_delivery(session, delivery)


@router.post("/outbox/{event_id}/retry")
def retry_outbox_event(
    event_id: UUID,
    actor: str = Depends(require_operator),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    event = session.get(DeliveryOutboxEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Outbox event not found")
    if event.status != OutboxStatus.PENDING.value:
        raise HTTPException(
            status_code=409, detail="Only pending outbox events can be retried"
        )
    event.due_at = datetime.now(timezone.utc)
    record_audit(
        session,
        actor=actor,
        action="outbox.retry",
        target_type="delivery_outbox",
        target_id=event.id,
        request_metadata={"delivery_id": str(event.delivery_id)},
    )
    session.commit()
    return _outbox_event_dict(session, event)


@router.get("/capabilities")
def operator_catalog() -> dict[str, Any]:
    """Full catalog, including legacy execution types and plan recipes."""

    return catalog_payload(public=False)


@router.get("/capabilities/{capability_id}")
def operator_capability(capability_id: str) -> dict[str, Any]:
    capability = CAPABILITIES_BY_ID.get(capability_id)
    if capability is None:
        raise HTTPException(status_code=404, detail="Capability not found")
    return capability_to_dict(capability, public=False)


@router.get("/quality-presets")
def operator_quality_presets() -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "quality_presets": [preset_to_dict(preset, public=False) for preset in PRESETS],
    }


@router.post("/routes/dry-run")
def operator_dry_run(
    intent: RouteIntent, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Full feasibility evaluation and resolved plan for engineering/support."""

    evaluation = evaluate_intent(session, intent)
    return {
        "feasible": evaluation.feasible,
        "checks": [check.model_dump() for check in evaluation.checks],
        "effective_quality_preset": (
            evaluation.effective_preset.id if evaluation.effective_preset else None
        ),
        "plan": evaluation.plan,
    }


@router.get("/plans/{route_id}")
def get_operator_plan(
    route_id: UUID, session: Session = Depends(get_session)
) -> dict[str, Any]:
    route = session.get(Route, route_id)
    if route is None or route.is_deleted:
        raise HTTPException(status_code=404, detail="Route not found")
    outputs = session.scalars(
        select(RouteOutput).where(RouteOutput.route_id == route_id)
    ).all()
    deliveries = session.scalars(
        select(Delivery).where(Delivery.route_id == route_id)
    ).all()
    delivery_models = []
    for delivery in deliveries:
        latest_run = session.scalar(
            select(DeliveryWorkflowRun)
            .where(DeliveryWorkflowRun.delivery_id == delivery.id)
            .order_by(DeliveryWorkflowRun.created_at.desc())
            .limit(1)
        )
        delivery_models.append(
            {
                "delivery_id": delivery.id,
                "output_id": delivery.destination_id,
                "state": delivery.state,
                "workflow": latest_run.event_type if latest_run is not None else None,
                "workflow_status": latest_run.status if latest_run is not None else None,
                "resolved_plan": latest_run.resolved_plan if latest_run is not None else None,
                "resource_map": latest_run.resource_map if latest_run is not None else None,
                "last_error": latest_run.last_error if latest_run is not None else None,
            }
        )
    return {
        "route_id": route.id,
        "plan_version": route.plan_version,
        "plan": route.resolved_plan,
        "outputs": [
            {
                "output_id": leg.output_id,
                "redundant_output_id": leg.redundant_output_id,
            }
            for leg in outputs
        ],
        "deliveries": delivery_models,
    }


def _outbox_event_dict(session: Session, event: DeliveryOutboxEvent) -> dict[str, Any]:
    delivery = session.get(Delivery, event.delivery_id)
    return {
        "id": event.id,
        "delivery_id": event.delivery_id,
        "route_id": delivery.route_id if delivery is not None else None,
        "event_type": event.event_type,
        "status": event.status,
        "due_at": event.due_at,
        "payload": event.payload,
        "attempts": event.attempts,
        "last_error": event.last_error,
        "published_at": event.published_at,
        "created_at": event.created_at,
    }


@router.get("/outbox")
def list_outbox(
    status_filter: OutboxStatus | None = Query(default=None, alias="status"),
    route_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    statement = (
        select(DeliveryOutboxEvent)
        .order_by(DeliveryOutboxEvent.due_at, DeliveryOutboxEvent.created_at)
        .limit(limit)
        .offset(offset)
    )
    if status_filter is not None:
        statement = statement.where(DeliveryOutboxEvent.status == status_filter.value)
    if route_id is not None:
        statement = statement.join(
            Delivery, Delivery.id == DeliveryOutboxEvent.delivery_id
        ).where(Delivery.route_id == route_id)
    return [
        _outbox_event_dict(session, event)
        for event in session.scalars(statement).all()
    ]


@router.get("/audit")
def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    audits = session.scalars(
        select(OperatorAudit)
        .order_by(OperatorAudit.created_at.desc(), OperatorAudit.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [
        {
            "id": audit.id,
            "actor": audit.actor,
            "action": audit.action,
            "target_type": audit.target_type,
            "target_id": audit.target_id,
            "request_metadata": audit.request_metadata,
            "created_at": audit.created_at,
        }
        for audit in audits
    ]


@router.get("/route-events")
def list_internal_route_events(
    route_id: UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    statement = (
        select(RouteEvent)
        .order_by(RouteEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if route_id is not None:
        statement = statement.where(RouteEvent.route_id == route_id)
    return [
        {
            "id": event.id,
            "route_id": event.route_id,
            "event_type": event.event_type,
            "level": event.level,
            "message": event.message,
            "metadata": event.event_metadata,
            "created_at": event.created_at,
        }
        for event in session.scalars(statement).all()
    ]
