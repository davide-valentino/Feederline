from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from app.domains.deliveries import Delivery, DeliveryWorkflowRun, WorkflowRunStatus
from app.workflows.direct_same_region import (
    ensure_flow_output,
    remove_flow_output,
    resolve_direct_link_plan,
)


def create_linkable_delivery(client):
    source = client.post(
        "/v1/sources",
        json={
            "name": "direct-flow-source",
            "source_type": "mediaconnectsource",
            "region": "eu-west-1",
            "config": {
                "flow_arn": "arn:aws:mediaconnect:eu-west-1:123456789012:flow:flow-id"
            },
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "direct-flow-destination",
            "destination_type": "mediaconnectdestination",
            "region": "eu-west-1",
            "config": {
                "mediaconnect_output": {
                    "Protocol": "rtp",
                    "Destination": "192.0.2.20",
                    "Port": 5000,
                }
            },
        },
    ).json()
    start = datetime.now(timezone.utc) + timedelta(minutes=5)
    delivery = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    ).json()
    client.post(f"/internal/deliveries/{delivery['id']}/start")
    return delivery


def test_resolve_direct_link_plan_freezes_media_connect_request(client, test_session_factory):
    delivery = create_linkable_delivery(client)
    with test_session_factory() as session:
        plan = resolve_direct_link_plan(session, UUID(delivery["id"]))
        run = session.scalar(
            select(DeliveryWorkflowRun).where(
                DeliveryWorkflowRun.delivery_id == UUID(delivery["id"])
            )
        )

    assert plan["flowArn"].endswith(":flow:flow-id")
    assert plan["outputName"] == f"feederline-{delivery['id']}"
    assert plan["output"]["Protocol"] == "rtp"
    assert run.event_type == "PipelineLink"
    assert run.status == "QUEUED"


def test_ensure_flow_output_is_idempotent_by_stable_output_name():
    class FakeMediaConnectClient:
        def __init__(self):
            self.outputs = []
            self.add_calls = 0

        def describe_flow(self, FlowArn):
            return {
                "Flow": {
                    "FlowArn": FlowArn,
                    "Status": "ACTIVE",
                    "Outputs": list(self.outputs),
                }
            }

        def add_flow_outputs(self, FlowArn, Outputs):
            self.add_calls += 1
            output = {"Name": Outputs[0]["Name"], "OutputArn": "arn:output:one"}
            self.outputs.append(output)
            return {"FlowArn": FlowArn, "Outputs": [output]}

    client = FakeMediaConnectClient()
    plan = {
        "deliveryId": "delivery-1",
        "flowArn": "arn:flow:one",
        "region": "eu-west-1",
        "outputName": "feederline-delivery-1",
        "output": {"Name": "feederline-delivery-1", "Protocol": "rtp"},
    }

    first = ensure_flow_output(plan, client)
    second = ensure_flow_output(plan, client)

    assert first == second
    assert first["outputArn"] == "arn:output:one"
    assert client.add_calls == 1


def test_remove_flow_output_is_idempotent():
    class FakeMediaConnectClient:
        def __init__(self):
            self.outputs = [{"Name": "out", "OutputArn": "arn:output:one"}]
            self.removed = []

        def describe_flow(self, FlowArn):
            return {"Flow": {"FlowArn": FlowArn, "Status": "ACTIVE", "Outputs": self.outputs}}

        def remove_flow_output(self, FlowArn, OutputArn):
            self.removed.append(OutputArn)
            self.outputs = [output for output in self.outputs if output["OutputArn"] != OutputArn]

    client = FakeMediaConnectClient()
    resources = {"flowArn": "arn:flow:one", "outputArn": "arn:output:one"}

    remove_flow_output(resources, client)
    remove_flow_output(resources, client)

    assert client.removed == ["arn:output:one"]


def test_direct_link_and_unlink_activities_persist_and_reuse_resource_map(
    client, test_session_factory, monkeypatch
):
    delivery = create_linkable_delivery(client)
    with test_session_factory() as session:
        link_run = session.scalar(
            select(DeliveryWorkflowRun).where(
                DeliveryWorkflowRun.delivery_id == UUID(delivery["id"]),
                DeliveryWorkflowRun.event_type == "PipelineLink",
            )
        )

    class FakeMediaConnectClient:
        def __init__(self):
            self.outputs = []
            self.add_calls = 0
            self.remove_calls = 0

        def describe_flow(self, FlowArn):
            return {
                "Flow": {
                    "FlowArn": FlowArn,
                    "Status": "ACTIVE",
                    "Outputs": list(self.outputs),
                }
            }

        def add_flow_outputs(self, FlowArn, Outputs):
            self.add_calls += 1
            output = {"Name": Outputs[0]["Name"], "OutputArn": "arn:output:one"}
            self.outputs.append(output)
            return {"FlowArn": FlowArn, "Outputs": [output]}

        def remove_flow_output(self, FlowArn, OutputArn):
            self.remove_calls += 1
            self.outputs = [output for output in self.outputs if output["OutputArn"] != OutputArn]

    media_connect = FakeMediaConnectClient()
    monkeypatch.setattr("app.workflows.direct_same_region.SessionLocal", test_session_factory)
    monkeypatch.setattr(
        "app.workflows.direct_same_region.boto3.client",
        lambda *_args, **_kwargs: media_connect,
    )
    from app.workflows.direct_same_region import lambda_handler

    execution_arn = "arn:aws:states:eu-west-1:123456789012:execution:direct:link"
    base_event = {
        "eventId": str(link_run.event_id),
        "deliveryId": delivery["id"],
        "executionArn": execution_arn,
    }
    plan = lambda_handler({**base_event, "action": "resolve_link"}, None)["plan"]
    resource_map = lambda_handler(
        {**base_event, "action": "apply_link", "plan": plan}, None
    )["resourceMap"]
    lambda_handler({**base_event, "action": "complete_link"}, None)

    with test_session_factory() as session:
        link_run = session.get(DeliveryWorkflowRun, link_run.event_id)
        linked_delivery = session.get(Delivery, UUID(delivery["id"]))
        assert link_run.execution_arn == execution_arn
        assert link_run.resolved_plan == plan
        assert link_run.resource_map == resource_map
        assert link_run.status == WorkflowRunStatus.SUCCEEDED.value
        assert linked_delivery.state == "IN_PROGRESS"

    client.post(f"/internal/deliveries/{delivery['id']}/stop")
    with test_session_factory() as session:
        unlink_run = session.scalar(
            select(DeliveryWorkflowRun).where(
                DeliveryWorkflowRun.delivery_id == UUID(delivery["id"]),
                DeliveryWorkflowRun.event_type == "PipelineUnlink",
            )
        )
    unlink_event = {
        "eventId": str(unlink_run.event_id),
        "deliveryId": delivery["id"],
        "executionArn": "arn:aws:states:eu-west-1:123456789012:execution:direct:unlink",
    }
    resolved_unlink = lambda_handler(
        {**unlink_event, "action": "resolve_unlink"}, None
    )
    lambda_handler(
        {
            **unlink_event,
            "action": "apply_unlink",
            "resourceMap": resolved_unlink["resourceMap"],
        },
        None,
    )
    lambda_handler({**unlink_event, "action": "complete_unlink"}, None)

    with test_session_factory() as session:
        unlink_run = session.get(DeliveryWorkflowRun, unlink_run.event_id)
        completed_delivery = session.get(Delivery, UUID(delivery["id"]))
        assert unlink_run.resource_map == resource_map
        assert unlink_run.status == WorkflowRunStatus.SUCCEEDED.value
        assert completed_delivery.state == "COMPLETE"
    assert media_connect.add_calls == 1
    assert media_connect.remove_calls == 1