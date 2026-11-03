import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from botocore.exceptions import ClientError
from sqlalchemy import select

from app.domains.deliveries import (
    DeliveryOutboxEvent,
    DeliveryWorkflowRun,
    OutboxStatus,
    WorkflowRunStatus,
)
from app.outbox import EventBridgePublisher, dispatch_due_events


class RecordingPublisher:
    def __init__(self):
        self.events = []

    def publish(self, event_type, detail):
        self.events.append((event_type, detail))


class FailingPublisher:
    def publish(self, _event_type, _detail):
        raise RuntimeError("temporary publisher outage")


def create_delivery(client, start, end):
    source = client.post(
        "/v1/sources",
        json={
            "name": "outbox-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "outbox-output",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()
    response = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": end.isoformat(),
        },
    )
    assert response.status_code == 201
    return response.json()


def test_dispatch_publishes_due_events_and_leaves_future_events_pending(
    client, test_session_factory
):
    now = datetime.now(timezone.utc)
    delivery = create_delivery(
        client,
        start=now - timedelta(minutes=1),
        end=now + timedelta(hours=1),
    )
    publisher = RecordingPublisher()

    count = dispatch_due_events(publisher, test_session_factory, now=now)

    assert count == 1
    event_type, detail = publisher.events[0]
    assert event_type == "DeliveryStart"
    assert detail["deliveryId"] == delivery["id"]
    assert UUID(detail["eventId"])
    with test_session_factory() as session:
        events = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(delivery["id"])
            )
        ).all()
    states = {event.event_type: event.status for event in events}
    assert states == {
        "DeliveryStart": OutboxStatus.PUBLISHED.value,
        "DeliveryStop": OutboxStatus.PENDING.value,
    }


def test_failed_publish_stays_pending_and_records_retry_details(
    client, test_session_factory
):
    now = datetime.now(timezone.utc)
    delivery = create_delivery(
        client,
        start=now - timedelta(minutes=1),
        end=now + timedelta(hours=1),
    )

    count = dispatch_due_events(FailingPublisher(), test_session_factory, now=now)

    assert count == 0
    with test_session_factory() as session:
        event = session.scalar(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(delivery["id"]),
                DeliveryOutboxEvent.event_type == "DeliveryStart",
            )
        )
    assert event.status == OutboxStatus.PENDING.value
    assert event.attempts == 1
    assert event.last_error == "temporary publisher outage"


def test_eventbridge_publisher_sends_versioned_event(monkeypatch):
    class FakeEventBridgeClient:
        def __init__(self):
            self.entries = None

        def put_events(self, Entries):
            self.entries = Entries
            return {"FailedEntryCount": 0}

    fake_client = FakeEventBridgeClient()
    monkeypatch.setattr("app.outbox.boto3.client", lambda *_args, **_kwargs: fake_client)
    publisher = EventBridgePublisher()

    publisher.publish("DeliveryStart", {"deliveryId": "example-id"})

    assert fake_client.entries[0]["Source"] == "tv.feederline"
    assert fake_client.entries[0]["DetailType"] == "DeliveryStart"
    assert fake_client.entries[0]["EventBusName"] == "feederline-dev-events"
    assert '"schemaVersion": 1' in fake_client.entries[0]["Detail"]


def test_step_functions_publisher_starts_workflow_for_pipeline_events():
    from app.outbox import StepFunctionsPublisher

    class FakeStepFunctionsClient:
        def __init__(self):
            self.executions = []

        def start_execution(self, **request):
            self.executions.append(request)

    event_bridge = RecordingPublisher()
    step_functions = FakeStepFunctionsClient()
    publisher = StepFunctionsPublisher(
        event_bridge,
        step_functions,
        "arn:aws:states:region:account:stateMachine:delivery",
    )
    detail = {"eventId": "f51ec826-a170-43d1-b18b-bfd593b6a451", "deliveryId": "delivery-1"}

    publisher.publish("PipelineLink", detail)

    assert event_bridge.events == [("PipelineLink", detail)]
    request = step_functions.executions[0]
    assert request["name"] == "link-f51ec826-a170-43d1-b18b-bfd593b6a451"
    assert json.loads(request["input"]) == {
        "schemaVersion": 1,
        "eventId": detail["eventId"],
        "eventType": "PipelineLink",
        "detail": detail,
    }


def test_step_functions_publisher_does_not_start_workflow_for_scheduled_events():
    from app.outbox import StepFunctionsPublisher

    class FakeStepFunctionsClient:
        def __init__(self):
            self.executions = []

        def start_execution(self, **request):
            self.executions.append(request)

    client = FakeStepFunctionsClient()
    publisher = StepFunctionsPublisher(RecordingPublisher(), client, "state-machine-arn")

    publisher.publish("DeliveryStart", {"eventId": "event-1", "deliveryId": "delivery-1"})

    assert client.executions == []


def test_step_functions_publisher_treats_duplicate_execution_as_success():
    from app.outbox import StepFunctionsPublisher

    class DuplicateStepFunctionsClient:
        def start_execution(self, **_request):
            raise ClientError(
                {"Error": {"Code": "ExecutionAlreadyExists", "Message": "already running"}},
                "StartExecution",
            )

        def describe_execution(self, **request):
            return {"executionArn": request["executionArn"], "status": "RUNNING"}

    publisher = StepFunctionsPublisher(
        RecordingPublisher(), DuplicateStepFunctionsClient(), "state-machine-arn"
    )

    execution = publisher.publish(
        "PipelineUnlink",
        {"eventId": "f51ec826-a170-43d1-b18b-bfd593b6a451", "deliveryId": "delivery-1"},
    )
    assert execution["status"] == "RUNNING"
    assert execution["executionArn"].endswith(":unlink-f51ec826-a170-43d1-b18b-bfd593b6a451")


def test_dispatch_persists_started_workflow_execution(
    client, test_session_factory, operator_headers
):
    now = datetime.now(timezone.utc)
    delivery = create_delivery(
        client,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
    )
    client.post(
        f"/internal/deliveries/{delivery['id']}/start", headers=operator_headers
    )

    class WorkflowPublisher:
        def publish(self, event_type, _detail):
            if event_type == "PipelineLink":
                return {
                    "executionArn": "arn:aws:states:eu-west-1:123456789012:execution:delivery:link-id",
                    "status": "RUNNING",
                }
            return None

    dispatch_now = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert dispatch_due_events(WorkflowPublisher(), test_session_factory, now=dispatch_now) == 1
    with test_session_factory() as session:
        run = session.scalar(
            select(DeliveryWorkflowRun).where(
                DeliveryWorkflowRun.delivery_id == UUID(delivery["id"])
            )
        )

    assert run.execution_arn.endswith(":execution:delivery:link-id")
    assert run.status == WorkflowRunStatus.RUNNING.value


def test_eventbridge_only_dispatch_does_not_claim_workflow_started(
    client, test_session_factory, operator_headers
):
    now = datetime.now(timezone.utc)
    delivery = create_delivery(
        client,
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
    )
    client.post(
        f"/internal/deliveries/{delivery['id']}/start", headers=operator_headers
    )
    dispatch_now = datetime.now(timezone.utc) + timedelta(seconds=1)

    assert dispatch_due_events(RecordingPublisher(), test_session_factory, now=dispatch_now) == 1
    with test_session_factory() as session:
        run = session.scalar(
            select(DeliveryWorkflowRun).where(
                DeliveryWorkflowRun.delivery_id == UUID(delivery["id"])
            )
        )

    assert run.execution_arn is None
    assert run.status == WorkflowRunStatus.QUEUED.value
