from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from app.domains.deliveries import DeliveryOutboxEvent, OutboxStatus


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_delivery_contract_uses_resource_ids_without_tenant_models(
    client, test_session_factory
):
    profile_response = client.post(
        "/v1/stream-profiles",
        json={"name": "standard-hd", "profile_type": "abr", "config": {"renditions": []}},
    )
    source_response = client.post(
        "/v1/sources",
        json={
            "name": "studio-ingest",
            "source_type": "srtlistenersource",
            "region": "eu-west-1",
            "stream_profile_id": profile_response.json()["id"],
        },
    )
    destination_response = client.post(
        "/v1/destinations",
        json={
            "name": "primary-egress",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    )
    assert source_response.status_code == 201
    assert destination_response.status_code == 201
    assert source_response.json()["stream_profile_id"] == profile_response.json()["id"]

    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    response = client.post(
        "/v1/deliveries",
        json={
            "source_id": source_response.json()["id"],
            "destination_id": destination_response.json()["id"],
            "planned_start": start.isoformat(),
            "planned_end": end.isoformat(),
        },
    )

    assert response.status_code == 201
    assert response.json()["state"] == "PENDING"
    assert response.json()["source_id"] == source_response.json()["id"]
    assert "contribution_id" not in response.json()
    assert "organisation_id" not in response.json()
    assert client.get(f"/v1/deliveries/{response.json()['id']}").status_code == 200

    with test_session_factory() as session:
        events = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(response.json()["id"])
            )
        ).all()
    assert {event.event_type for event in events} == {"DeliveryStart", "DeliveryStop"}
    assert all(event.status == OutboxStatus.PENDING.value for event in events)
    assert client.delete(f"/v1/deliveries/{response.json()['id']}").status_code == 204
    assert client.get(f"/v1/deliveries/{response.json()['id']}").status_code == 404


def test_delivery_requires_a_complete_redundant_pair(client):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    response = client.post(
        "/v1/deliveries",
        json={
            "source_id": str(uuid4()),
            "destination_id": str(uuid4()),
            "redundant_source_id": str(uuid4()),
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 422


def test_delivery_rejects_unknown_source_or_destination(client):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    response = client.post(
        "/v1/deliveries",
        json={
            "source_id": str(uuid4()),
            "destination_id": str(uuid4()),
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 404


def test_overlapping_active_delivery_on_destination_is_rejected(client):
    source = client.post(
        "/v1/sources",
        json={
            "name": "overlap-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "overlap-output",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    first = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": end.isoformat(),
        },
    )
    second = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": (start + timedelta(minutes=30)).isoformat(),
            "planned_end": (end + timedelta(minutes=30)).isoformat(),
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409


def test_cancel_pending_delivery_cancels_its_scheduled_events(client, test_session_factory):
    source = client.post(
        "/v1/sources",
        json={
            "name": "cancel-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "cancel-output",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    response = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    )

    cancelled = client.patch(f"/v1/deliveries/{response.json()['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"
    with test_session_factory() as session:
        events = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(response.json()["id"])
            )
        ).all()
    assert all(event.status == OutboxStatus.CANCELLED.value for event in events)


def test_delivery_start_and_stop_callbacks_complete_lifecycle(client):
    source = client.post(
        "/v1/sources",
        json={
            "name": "lifecycle-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "lifecycle-output",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    created = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    )
    delivery_id = created.json()["id"]

    started = client.post(f"/internal/deliveries/{delivery_id}/start")
    repeated_start = client.post(f"/internal/deliveries/{delivery_id}/start")
    pipeline_started = client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")
    stopping = client.post(f"/internal/deliveries/{delivery_id}/stop")
    completed = client.post(f"/v1/deliveries/{delivery_id}/pipeline-stopped")

    assert started.json()["state"] == "STARTING"
    assert repeated_start.json()["state"] == "STARTING"
    assert pipeline_started.json()["state"] == "IN_PROGRESS"
    assert stopping.json()["state"] == "STOPPING"
    assert completed.json()["state"] == "COMPLETE"


def test_stop_during_start_waits_for_link_completion_before_queueing_unlink(
    client, test_session_factory
):
    source = client.post(
        "/v1/sources",
        json={
            "name": "inflight-stop-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "inflight-stop-destination",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    delivery = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    ).json()
    delivery_id = delivery["id"]

    assert client.post(f"/internal/deliveries/{delivery_id}/start").json()["state"] == "STARTING"
    assert client.post(f"/internal/deliveries/{delivery_id}/stop").json()["state"] == "STOPPING"
    with test_session_factory() as session:
        before_link_finishes = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(delivery_id),
                DeliveryOutboxEvent.event_type == "PipelineUnlink",
            )
        ).all()
    assert before_link_finishes == []

    assert client.post(f"/v1/deliveries/{delivery_id}/pipeline-started").json()["state"] == "STOPPING"
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")
    with test_session_factory() as session:
        after_link_finishes = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(delivery_id),
                DeliveryOutboxEvent.event_type == "PipelineUnlink",
            )
        ).all()
    assert len(after_link_finishes) == 1


def test_source_and_destination_updates_and_archive_safety(client):
    source = client.post(
        "/v1/sources",
        json={
            "name": "resource-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
        },
    ).json()
    destination = client.post(
        "/v1/destinations",
        json={
            "name": "resource-destination",
            "destination_type": "hlsdestination",
            "region": "eu-west-1",
        },
    ).json()

    updated_source = client.patch(
        f"/v1/sources/{source['id']}", json={"name": "resource-source-renamed"}
    )
    updated_destination = client.patch(
        f"/v1/destinations/{destination['id']}", json={"region": "eu-west-2"}
    )
    assert updated_source.json()["name"] == "resource-source-renamed"
    assert updated_destination.json()["region"] == "eu-west-2"

    start = datetime.now(timezone.utc) + timedelta(hours=1)
    delivery = client.post(
        "/v1/deliveries",
        json={
            "source_id": source["id"],
            "destination_id": destination["id"],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert delivery.status_code == 201
    assert client.delete(f"/v1/sources/{source['id']}").status_code == 409
    assert client.delete(f"/v1/destinations/{destination['id']}").status_code == 409

    cancelled = client.patch(f"/v1/deliveries/{delivery.json()['id']}/cancel")
    assert cancelled.status_code == 200
    assert client.delete(f"/v1/sources/{source['id']}").status_code == 204
    assert client.delete(f"/v1/destinations/{destination['id']}").status_code == 204
    assert client.get(f"/v1/sources/{source['id']}").status_code == 404
    assert client.get(f"/v1/destinations/{destination['id']}").status_code == 404


def test_stream_profile_update_and_archive_reference_safety(client):
    referenced_profile = client.post(
        "/v1/stream-profiles",
        json={"name": "referenced-profile", "profile_type": "abr", "config": {}},
    ).json()
    source = client.post(
        "/v1/sources",
        json={
            "name": "profile-source",
            "source_type": "srtcallersource",
            "region": "eu-west-1",
            "stream_profile_id": referenced_profile["id"],
        },
    )
    assert source.status_code == 201
    assert client.delete(
        f"/v1/stream-profiles/{referenced_profile['id']}"
    ).status_code == 409

    unused_profile = client.post(
        "/v1/stream-profiles",
        json={"name": "unused-profile", "profile_type": "abr", "config": {}},
    ).json()
    updated = client.patch(
        f"/v1/stream-profiles/{unused_profile['id']}",
        json={"config": {"renditions": [{"height": 720}]}},
    )
    assert updated.json()["config"]["renditions"][0]["height"] == 720
    assert client.delete(f"/v1/stream-profiles/{unused_profile['id']}").status_code == 204
    assert client.get(f"/v1/stream-profiles/{unused_profile['id']}").status_code == 404
