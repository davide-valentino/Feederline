"""AWS activities for direct, same-region MediaConnect output workflows."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.domains import (  # noqa: F401  register every mapped table (mappers resolve relations across domains)
    audit,
    connections,
    content,
    operator,
    route_events,
    routes,
    stream_profiles,
)
from app.domains import deliveries
from app.domains.deliveries import (
    Delivery,
    DeliveryOutboxEvent,
    DeliveryState,
    DeliveryWorkflowRun,
    WorkflowRunStatus,
    complete_pipeline_link,
)
from app.domains.destinations import Destination
from app.domains.sources import Source


def resolve_direct_link_plan(session: Session, delivery_id: UUID) -> dict[str, Any]:
    delivery = session.get(Delivery, delivery_id)
    if delivery is None or delivery.state not in (
        DeliveryState.STARTING.value,
        DeliveryState.STOPPING.value,
    ):
        raise ValueError("Delivery is not in a linkable state")
    if delivery.redundant_source_id is not None:
        raise ValueError("Direct same-region pilot does not support redundant deliveries")

    source = session.get(Source, delivery.source_id)
    destination = session.get(Destination, delivery.destination_id)
    if source is None or source.is_deleted or destination is None or destination.is_deleted:
        raise ValueError("Delivery source or destination is unavailable")
    if source.source_type != "mediaconnectsource":
        raise ValueError("Direct same-region pilot requires a MediaConnect source")
    if not source.region or source.region != destination.region:
        raise ValueError("Direct same-region pilot requires matching source and destination regions")

    flow_arn = source.config.get("flow_arn")
    output = destination.config.get("mediaconnect_output")
    arn_parts = flow_arn.split(":") if isinstance(flow_arn, str) else []
    if (
        len(arn_parts) < 6
        or arn_parts[0] != "arn"
        or arn_parts[2] != "mediaconnect"
        or arn_parts[3] != source.region
    ):
        raise ValueError("Source config must contain a MediaConnect flow_arn")
    if not isinstance(output, dict):
        raise ValueError("Destination config must contain a mediaconnect_output object")
    if not all(key in output for key in ("Protocol", "Destination", "Port")):
        raise ValueError("mediaconnect_output requires Protocol, Destination, and Port")
    if (
        not isinstance(output["Protocol"], str)
        or not output["Protocol"]
        or not isinstance(output["Destination"], str)
        or not output["Destination"]
        or not isinstance(output["Port"], int)
        or isinstance(output["Port"], bool)
        or not 1 <= output["Port"] <= 65535
    ):
        raise ValueError("mediaconnect_output has invalid protocol, destination, or port")

    output_name = f"feederline-{delivery.id}"
    output_config = deepcopy(output)
    output_config["Name"] = output_name
    return {
        "deliveryId": str(delivery.id),
        "flowArn": flow_arn,
        "region": source.region,
        "outputName": output_name,
        "output": output_config,
    }


def check_flow_status(plan: dict[str, Any], client: Any) -> dict[str, Any]:
    response = client.describe_flow(FlowArn=plan["flowArn"])
    flow = response["Flow"]
    return {"status": flow["Status"], "flowArn": flow["FlowArn"]}


def ensure_flow_output(plan: dict[str, Any], client: Any) -> dict[str, Any]:
    flow = client.describe_flow(FlowArn=plan["flowArn"])["Flow"]
    if flow["Status"] not in ("ACTIVE", "STANDBY"):
        raise RuntimeError(f"MediaConnect flow is not ready: {flow['Status']}")

    for output in flow.get("Outputs", []):
        if output.get("Name") == plan["outputName"]:
            return _resource_map(plan, output["OutputArn"])

    response = client.add_flow_outputs(FlowArn=plan["flowArn"], Outputs=[plan["output"]])
    outputs = response.get("Outputs", [])
    if len(outputs) != 1 or not outputs[0].get("OutputArn"):
        raise RuntimeError("MediaConnect did not return the created output ARN")
    return _resource_map(plan, outputs[0]["OutputArn"])


def remove_flow_output(resource_map: dict[str, Any], client: Any) -> None:
    try:
        flow = client.describe_flow(FlowArn=resource_map["flowArn"])["Flow"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NotFoundException":
            return
        raise
    output_arn = resource_map["outputArn"]
    if any(output.get("OutputArn") == output_arn for output in flow.get("Outputs", [])):
        client.remove_flow_output(FlowArn=resource_map["flowArn"], OutputArn=output_arn)


def _resource_map(plan: dict[str, Any], output_arn: str) -> dict[str, Any]:
    return {
        "kind": "mediaconnect_flow_output",
        "deliveryId": plan["deliveryId"],
        "flowArn": plan["flowArn"],
        "outputArn": output_arn,
        "outputName": plan["outputName"],
        "region": plan["region"],
    }


def _get_workflow_run(session: Session, event_id: UUID) -> DeliveryWorkflowRun:
    run = session.get(DeliveryWorkflowRun, event_id)
    if run is None:
        raise ValueError(f"Workflow run {event_id} was not created with its outbox event")
    return run


def _latest_link_run(session: Session, delivery_id: UUID) -> DeliveryWorkflowRun:
    run = session.scalar(
        select(DeliveryWorkflowRun)
        .where(
            DeliveryWorkflowRun.delivery_id == delivery_id,
            DeliveryWorkflowRun.event_type == "PipelineLink",
            DeliveryWorkflowRun.resource_map.is_not(None),
        )
        .order_by(DeliveryWorkflowRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        raise ValueError("No persisted MediaConnect resource map exists for this delivery")
    return run


def _resolve_link(session: Session, event_id: UUID, delivery_id: UUID, execution_arn: str):
    run = _get_workflow_run(session, event_id)
    if run.event_type != "PipelineLink" or run.delivery_id != delivery_id:
        raise ValueError("Workflow event is not PipelineLink")
    plan = run.resolved_plan or resolve_direct_link_plan(session, delivery_id)
    run.resolved_plan = plan
    run.execution_arn = execution_arn or run.execution_arn
    run.status = WorkflowRunStatus.RUNNING.value
    run.started_at = run.started_at or datetime.now(timezone.utc)
    run.last_error = None
    session.commit()
    return plan


def _resolve_unlink(session: Session, event_id: UUID, delivery_id: UUID, execution_arn: str):
    run = _get_workflow_run(session, event_id)
    if run.event_type != "PipelineUnlink" or run.delivery_id != delivery_id:
        raise ValueError("Workflow event is not PipelineUnlink")
    link_run = _latest_link_run(session, delivery_id)
    if link_run.resolved_plan is None:
        raise ValueError("Link workflow did not persist its resolved plan")
    if link_run.resource_map is None:
        raise ValueError("Link workflow did not persist its MediaConnect resource map")
    run.resolved_plan = link_run.resolved_plan
    run.resource_map = link_run.resource_map
    run.execution_arn = execution_arn or run.execution_arn
    run.status = WorkflowRunStatus.RUNNING.value
    run.started_at = run.started_at or datetime.now(timezone.utc)
    session.commit()
    return run.resolved_plan, run.resource_map


def _persist_resource_map(
    session: Session, event_id: UUID, execution_arn: str, resource_map: dict[str, Any]
) -> None:
    run = _get_workflow_run(session, event_id)
    if run.delivery_id != UUID(resource_map["deliveryId"]):
        raise ValueError("Resource map delivery does not match its workflow event")
    run.execution_arn = execution_arn or run.execution_arn
    run.resource_map = resource_map
    run.status = WorkflowRunStatus.RUNNING.value
    run.last_error = None
    session.commit()


def _record_failure(session: Session, event_id: UUID, delivery_id: UUID, error: str) -> None:
    run = _get_workflow_run(session, event_id)
    if run.delivery_id != delivery_id:
        raise ValueError("Workflow event does not match delivery")
    run.status = WorkflowRunStatus.FAILED.value
    run.last_error = error[:4000]
    run.finished_at = datetime.now(timezone.utc)
    delivery = session.get(Delivery, delivery_id)
    if delivery is not None and delivery.state not in (
        DeliveryState.COMPLETE.value,
        DeliveryState.CANCELLED.value,
        DeliveryState.DELETED.value,
    ):
        delivery.state = DeliveryState.ERROR.value
        pending_events = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == delivery.id,
                DeliveryOutboxEvent.status == "PENDING",
            )
        )
        for pending_event in pending_events:
            pending_event.status = "CANCELLED"
    session.commit()


def _complete_link(session: Session, event_id: UUID, delivery_id: UUID) -> None:
    run = _get_workflow_run(session, event_id)
    if run.delivery_id != delivery_id or run.resource_map is None:
        raise ValueError("Link workflow has no persisted resource map for this delivery")
    delivery = session.get(Delivery, delivery_id)
    if delivery is None:
        raise ValueError(f"Delivery {delivery_id} no longer exists")
    complete_pipeline_link(session, delivery)
    run.status = WorkflowRunStatus.SUCCEEDED.value
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def _complete_unlink(session: Session, event_id: UUID, delivery_id: UUID) -> None:
    run = _get_workflow_run(session, event_id)
    if run.delivery_id != delivery_id:
        raise ValueError("Workflow event does not match delivery")
    delivery = session.get(Delivery, delivery_id)
    if delivery is None:
        raise ValueError(f"Delivery {delivery_id} no longer exists")
    if delivery.state == DeliveryState.STOPPING.value:
        delivery.state = (
            DeliveryState.CANCELLED.value
            if delivery.stop_reason == "CANCELLED"
            else DeliveryState.COMPLETE.value
        )
    elif delivery.state not in (DeliveryState.COMPLETE.value, DeliveryState.CANCELLED.value):
        raise ValueError("Delivery is not stopping")
    run.status = WorkflowRunStatus.SUCCEEDED.value
    run.finished_at = datetime.now(timezone.utc)
    session.commit()


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    action = event["action"]
    event_id = UUID(event["eventId"])
    delivery_id = UUID(event["deliveryId"])
    execution_arn = event.get("executionArn", "")
    with SessionLocal() as session:
        if action == "resolve_link":
            return {"plan": _resolve_link(session, event_id, delivery_id, execution_arn)}
        if action == "check_flow":
            client = boto3.client("mediaconnect", region_name=event["plan"]["region"])
            return check_flow_status(event["plan"], client)
        if action == "apply_link":
            plan = event["plan"]
            client = boto3.client("mediaconnect", region_name=plan["region"])
            resource_map = ensure_flow_output(plan, client)
            _persist_resource_map(session, event_id, execution_arn, resource_map)
            return {"resourceMap": resource_map}
        if action == "compensate_link":
            run = _get_workflow_run(session, event_id)
            resource_map = run.resource_map
            if resource_map is None and run.resolved_plan is not None:
                plan = run.resolved_plan
                client = boto3.client("mediaconnect", region_name=plan["region"])
                flow = client.describe_flow(FlowArn=plan["flowArn"])["Flow"]
                output_arn = next(
                    (
                        output.get("OutputArn")
                        for output in flow.get("Outputs", [])
                        if output.get("Name") == plan["outputName"]
                    ),
                    None,
                )
                if output_arn is not None:
                    resource_map = _resource_map(plan, output_arn)
                    run.resource_map = resource_map
                    session.commit()
            if resource_map is not None:
                client = boto3.client("mediaconnect", region_name=resource_map["region"])
                remove_flow_output(resource_map, client)
            return {"compensated": True}
        if action == "resolve_unlink":
            plan, resource_map = _resolve_unlink(session, event_id, delivery_id, execution_arn)
            return {"plan": plan, "resourceMap": resource_map}
        if action == "apply_unlink":
            resource_map = event["resourceMap"]
            client = boto3.client("mediaconnect", region_name=resource_map["region"])
            remove_flow_output(resource_map, client)
            return {"unlinked": True}
        if action == "complete_link":
            _complete_link(session, event_id, delivery_id)
            return {"state": "SUCCEEDED"}
        if action == "complete_unlink":
            _complete_unlink(session, event_id, delivery_id)
            return {"state": "SUCCEEDED"}
        if action == "record_failure":
            _record_failure(session, event_id, delivery_id, event.get("error", "workflow failed"))
            return {"state": "FAILED"}
    raise ValueError(f"Unsupported workflow activity action: {action}")