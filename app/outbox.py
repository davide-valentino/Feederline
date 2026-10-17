import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.database import SessionLocal
from app.domains.deliveries import (
    DeliveryOutboxEvent,
    DeliveryWorkflowRun,
    OutboxStatus,
    WorkflowRunStatus,
)


logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    def publish(self, event_type: str, detail: dict[str, Any]) -> dict[str, Any] | None: ...


class LoggingPublisher:
    def publish(self, event_type: str, detail: dict[str, Any]) -> None:
        logger.info("Event %s: %s", event_type, json.dumps(detail, sort_keys=True))


class EventBridgePublisher:
    def __init__(self) -> None:
        self.client = boto3.client("events", region_name=settings.aws_region)

    def publish(self, event_type: str, detail: dict[str, Any]) -> None:
        response = self.client.put_events(
            Entries=[
                {
                    "EventBusName": settings.event_bus_name,
                    "Source": "tv.feederline",
                    "DetailType": event_type,
                    "Detail": json.dumps({"schemaVersion": 1, **detail}),
                }
            ]
        )
        if response.get("FailedEntryCount", 0):
            errors = response.get("Entries", [])
            raise RuntimeError(f"EventBridge rejected event: {errors}")


class StepFunctionsPublisher:
    def __init__(
        self,
        event_bridge: EventPublisher,
        client: Any,
        state_machine_arn: str,
    ) -> None:
        if not state_machine_arn:
            raise ValueError(
                "WORKFLOW_STATE_MACHINE_ARN is required for Step Functions publishing"
            )
        self.event_bridge = event_bridge
        self.client = client
        self.state_machine_arn = state_machine_arn

    def publish(self, event_type: str, detail: dict[str, Any]) -> dict[str, Any] | None:
        self.event_bridge.publish(event_type, detail)
        if event_type not in {"PipelineLink", "PipelineUnlink"}:
            return None

        event_id = detail.get("eventId")
        if not event_id:
            raise ValueError(f"{event_type} event is missing eventId")
        action = "link" if event_type == "PipelineLink" else "unlink"
        try:
            response = self.client.start_execution(
                stateMachineArn=self.state_machine_arn,
                name=f"{action}-{event_id}",
                input=json.dumps(
                    {
                        "schemaVersion": 1,
                        "eventId": event_id,
                        "eventType": event_type,
                        "detail": detail,
                    },
                    sort_keys=True,
                ),
            )
            return response
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                raise
            logger.info("Step Functions execution already exists for outbox event %s", event_id)
            execution_arn = self.state_machine_arn.replace(":stateMachine:", ":execution:")
            execution_arn = f"{execution_arn}:{action}-{event_id}"
            return self.client.describe_execution(executionArn=execution_arn)


def build_publisher() -> EventPublisher:
    if settings.event_publisher == "log":
        return LoggingPublisher()
    if settings.event_publisher == "eventbridge":
        return EventBridgePublisher()
    if settings.event_publisher == "stepfunctions":
        return StepFunctionsPublisher(
            event_bridge=EventBridgePublisher(),
            client=boto3.client("stepfunctions", region_name=settings.aws_region),
            state_machine_arn=settings.workflow_state_machine_arn,
        )
    raise ValueError(f"Unsupported event publisher: {settings.event_publisher}")


def dispatch_due_events(
    publisher: EventPublisher,
    session_factory: sessionmaker[Session] = SessionLocal,
    now: datetime | None = None,
    batch_size: int = 50,
) -> int:
    current_time = now or datetime.now(timezone.utc)
    dispatched = 0
    with session_factory() as session:
        statement = (
            select(DeliveryOutboxEvent)
            .where(
                DeliveryOutboxEvent.status == OutboxStatus.PENDING.value,
                DeliveryOutboxEvent.due_at <= current_time,
            )
            .order_by(DeliveryOutboxEvent.due_at, DeliveryOutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        due_events = session.scalars(statement).all()
        for event in due_events:
            event.attempts += 1
            try:
                publish_result = publisher.publish(
                    event.event_type,
                    {"eventId": str(event.id), **event.payload},
                )
                if (
                    event.event_type in {"PipelineLink", "PipelineUnlink"}
                    and publish_result is not None
                    and publish_result.get("executionArn")
                ):
                    workflow_run = session.get(DeliveryWorkflowRun, event.id)
                    if workflow_run is not None:
                        workflow_run.execution_arn = publish_result["executionArn"]
                        if workflow_run.status == WorkflowRunStatus.QUEUED.value:
                            execution_status = publish_result.get("status", "RUNNING") if publish_result else "RUNNING"
                            workflow_run.status = {
                                "RUNNING": WorkflowRunStatus.RUNNING.value,
                                "SUCCEEDED": WorkflowRunStatus.SUCCEEDED.value,
                                "FAILED": WorkflowRunStatus.FAILED.value,
                                "TIMED_OUT": WorkflowRunStatus.FAILED.value,
                                "ABORTED": WorkflowRunStatus.FAILED.value,
                            }.get(execution_status, WorkflowRunStatus.RUNNING.value)
                            if workflow_run.status == WorkflowRunStatus.RUNNING.value:
                                workflow_run.started_at = workflow_run.started_at or current_time
                            else:
                                workflow_run.finished_at = current_time
                        if workflow_run.status != WorkflowRunStatus.FAILED.value:
                            workflow_run.last_error = None
            except Exception as error:  # keep failed events pending for retry
                event.last_error = str(error)[:2000]
                workflow_run = session.get(DeliveryWorkflowRun, event.id)
                if workflow_run is not None:
                    workflow_run.last_error = event.last_error
                logger.exception("Failed to publish outbox event %s", event.id)
            else:
                event.status = OutboxStatus.PUBLISHED.value
                event.published_at = current_time
                event.last_error = None
                dispatched += 1
        session.commit()
    return dispatched


def run_worker() -> None:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Publish due Feederline outbox events")
    parser.add_argument(
        "--once",
        action="store_true",
        help="publish one batch of due events and exit",
    )
    arguments = parser.parse_args()
    publisher = build_publisher()
    logger.info("Starting Feederline outbox worker with %s publisher", settings.event_publisher)
    if arguments.once:
        dispatched = dispatch_due_events(publisher)
        logger.info("Published %s outbox events", dispatched)
        return
    while True:
        dispatch_due_events(publisher)
        time.sleep(settings.outbox_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker()
