"""Review regressions for the P0 operator boundary and output delete guard."""

import dataclasses
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.domains.deliveries import Delivery, DeliveryState
from app.domains.routes import Route


def future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def create_content(client, name="review-content"):
    response = client.post(
        "/v1/content",
        json={
            "name": name,
            "content_type": "channel",
            "owner": "operations",
            "description": "review regression content",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_feed(client, name="review-feed"):
    response = client.post(
        "/v1/feeds",
        json={
            "name": name,
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {
            "port": 5000,
            "min_latency_ms": 200,
            "whitelist_cidr": "192.0.2.0/24",
        },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_output(client, name="review-output"):
    response = client.post(
        "/v1/outputs",
        json={
            "name": name,
            "capability_id": "linear",
            "region": "eu-west-1",
            "config": {
                "ip_address": "192.0.2.10",
                "port": 6000,
                "ip_destination_type": "rtp",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_route(client, content_id, feed_id, output_id):
    start = future()
    response = client.post(
        "/v1/routes",
        json={
            "content_id": content_id,
            "feed_id": feed_id,
            "quality_preset": "1080p",
            "resilience": "single",
            "output_ids": [output_id],
            "planned_start": start.isoformat(),
            "planned_end": (start + timedelta(hours=1)).isoformat(),
            "is_open_ended": False,
            "encrypted": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def disable_operator_token(monkeypatch):
    """Simulate an unconfigured boundary without mutating the frozen settings."""

    monkeypatch.setattr(
        "app.auth.settings", dataclasses.replace(settings, operator_token="")
    )


def test_configured_operator_boundary_requires_token(client, operator_headers):
    assert client.get("/internal/deliveries").status_code == 401

    response = client.get("/internal/deliveries", headers=operator_headers)
    assert response.status_code == 200


def test_unconfigured_operator_boundary_fails_closed(client, monkeypatch):
    disable_operator_token(monkeypatch)

    response = client.get("/internal/deliveries")
    assert response.status_code == 503
    assert response.json()["detail"] == "Operator boundary is not configured"

    response = client.get(
        "/internal/deliveries", headers={"X-Operator-Token": "anything"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Operator boundary is not configured"

    # Customer endpoints stay open while the operator boundary is unconfigured.
    assert client.get("/v1/content").status_code == 200


def test_output_delete_ignores_soft_deleted_routes(client, test_session_factory):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(client, content["id"], feed["id"], output["id"])

    # Route creation schedules a PENDING delivery; finish it so the delete guard
    # under test is the route reference, not an active delivery.
    with test_session_factory() as session:
        deliveries = session.scalars(
            select(Delivery).where(Delivery.route_id == UUID(route["id"]))
        ).all()
        assert deliveries
        for delivery in deliveries:
            delivery.state = DeliveryState.COMPLETE.value
        session.commit()

    response = client.delete(f"/v1/outputs/{output['id']}")
    assert response.status_code == 409
    assert response.json()["detail"] == "Output is used by a route"

    with test_session_factory() as session:
        stored_route = session.get(Route, UUID(route["id"]))
        stored_route.is_deleted = True
        session.commit()

    assert client.delete(f"/v1/outputs/{output['id']}").status_code == 204
