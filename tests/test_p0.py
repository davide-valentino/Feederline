from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.domains.deliveries import Delivery, DeliveryOutboxEvent
from app.domains.routes import Route

OPERATOR_HEADERS = {"X-Operator-Token": settings.operator_token}


def future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def create_content(client, name="channel-one", content_type="channel"):
    response = client.post(
        "/v1/content",
        json={
            "name": name,
            "content_type": content_type,
            "owner": "operations",
            "description": "test content",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_feed(
    client,
    name="feed-one",
    capability_id="srt-listener",
    region="eu-west-1",
    config=None,
):
    response = client.post(
        "/v1/feeds",
        json={
            "name": name,
            "capability_id": capability_id,
            "region": region,
            "config": config
            if config is not None
            else {
                "port": 5000,
                "min_latency_ms": 200,
                "whitelist_cidr": "192.0.2.0/24",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_output(
    client,
    name="output-one",
    capability_id="linear",
    region="eu-west-1",
    config=None,
):
    response = client.post(
        "/v1/outputs",
        json={
            "name": name,
            "capability_id": capability_id,
            "region": region,
            "config": config
            if config is not None
            else {
                "ip_address": "192.0.2.10",
                "port": 6000,
                "ip_destination_type": "rtp",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def route_body(
    content_id,
    feed_id,
    output_ids,
    *,
    redundant_feed_id=None,
    redundant_output_ids=None,
    quality_preset="1080p",
    resilience="single",
    start=None,
    end=None,
    is_open_ended=False,
    encrypted=False,
):
    start = start or future()
    if not is_open_ended and end is None:
        end = start + timedelta(hours=1)
    body = {
        "content_id": content_id,
        "feed_id": feed_id,
        "redundant_feed_id": redundant_feed_id,
        "quality_preset": quality_preset,
        "resilience": resilience,
        "output_ids": output_ids,
        "planned_start": start.isoformat(),
        "planned_end": end.isoformat() if end is not None else None,
        "is_open_ended": is_open_ended,
        "encrypted": encrypted,
    }
    if redundant_output_ids is not None:
        body["redundant_output_ids"] = redundant_output_ids
    return body


def create_route(client, body, idempotency_key=None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post("/v1/routes", json=body, headers=headers)


def delivery_ids_for_route(client, route_id):
    """Resolve kernel delivery ids through the operator surface.

    The customer route representation intentionally no longer exposes them.
    """

    response = client.get(
        f"/internal/deliveries?route_id={route_id}", headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


def test_catalog_is_served_as_data(client):
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_version"] == "2026-11-03"
    assert len(payload["transports"]) == 6
    assert len(payload["formats"]) == 11
    assert len(payload["quality_presets"]) == 4

    capability = client.get("/v1/capabilities/srt-listener").json()
    assert capability["kind"] == "transport"
    assert "legacy_source_type" not in capability
    assert capability["required_fields"][0]["name"] == "port"

    operator_capability = client.get(
        "/internal/capabilities/srt-listener", headers=OPERATOR_HEADERS
    ).json()
    assert operator_capability["legacy_source_type"] == "srtlistenersource"

    presets = client.get("/v1/quality-presets").json()["quality_presets"]
    assert {preset["id"] for preset in presets} == {
        "passthrough",
        "1080p",
        "1080p-premium",
        "uhd",
    }
    assert all("profile" not in preset for preset in presets)

    operator_presets = client.get(
        "/internal/quality-presets", headers=OPERATOR_HEADERS
    ).json()["quality_presets"]
    assert next(
        preset for preset in operator_presets if preset["id"] == "1080p"
    )["profile"]["id"] == "profile-1080p"


def test_content_crud_and_derived_status(client, test_session_factory):
    content = create_content(client)
    assert content["status"] == "off-air"

    feed = create_feed(client)
    output = create_output(client)
    body = route_body(
        content["id"], feed["id"], [output["id"]], start=future(2)
    )
    route = create_route(client, body)
    assert route.status_code == 201, route.text
    route_id = route.json()["id"]

    assert client.get(f"/v1/content/{content['id']}").json()["status"] == "scheduled"

    delivery_id = delivery_ids_for_route(client, route_id)[0]
    assert client.post(
        f"/internal/deliveries/{delivery_id}/start", headers=OPERATOR_HEADERS
    ).status_code == 200
    assert client.get(f"/v1/content/{content['id']}").json()["status"] == "live"

    assert client.post(
        f"/v1/deliveries/{delivery_id}/pipeline-started"
    ).status_code == 200
    assert client.get(f"/v1/content/{content['id']}").json()["status"] == "live"

    # Natural completion (scheduled end) moves the derived status to complete.
    client.post(f"/internal/deliveries/{delivery_id}/stop", headers=OPERATOR_HEADERS)
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-stopped")
    assert client.get(f"/v1/content/{content['id']}").json()["status"] == "complete"

    # Rule 3: desired active with no live delivery still counts as live.
    with test_session_factory() as session:
        route_model = session.get(Route, UUID(route_id))
        route_model.desired_state = "active"
        route_model.observed_state = "pending"
        session.commit()
    assert client.get(f"/v1/content/{content['id']}").json()["status"] == "live"

    assert client.patch(
        f"/v1/content/{content['id']}", json={"owner": "traffic"}
    ).json()["owner"] == "traffic"
    assert client.delete(f"/v1/content/{content['id']}").status_code == 409


def test_feed_and_output_validate_capability_fields_and_regions(client):
    feed = create_feed(client)
    assert feed["capability"]["id"] == "srt-listener"
    assert "source_type" not in feed
    assert "config" not in feed
    # Only standard fields are echoed; port/CIDR stay below the line.
    assert feed["details"] == {"min_latency_ms": 200}

    missing_field = client.post(
        "/v1/feeds",
        json={
            "name": "bad-feed",
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {},
        },
    )
    assert missing_field.status_code == 422

    wrong_region = client.post(
        "/v1/feeds",
        json={
            "name": "bad-region-feed",
            "capability_id": "srt-caller",
            "region": "ap-southeast-2",
            "config": {"endpoint": "192.0.2.1", "port": 5000},
        },
    )
    assert wrong_region.status_code == 422

    output = create_output(
        client,
        capability_id="hls",
        config={
            "url": "https://example.test/hls",
            "output_destination_type": "s3",
            "name_modifier": "live",
            "cdn_settings_type": "none",
        },
    )
    assert output["capability"]["id"] == "hls"
    assert output["details"] == {
        "url": "https://example.test/hls",
        "name_modifier": "live",
    }

    wrong_kind = client.post(
        "/v1/outputs",
        json={
            "name": "bad-output",
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {"port": 5000},
        },
    )
    assert wrong_kind.status_code == 422


def test_dry_run_warns_and_upgrades_passthrough_for_ott(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(
        client,
        capability_id="hls",
        config={
            "url": "https://example.test/hls",
            "output_destination_type": "s3",
            "name_modifier": "live",
            "cdn_settings_type": "none",
        },
    )
    body = route_body(
        content["id"],
        feed["id"],
        [output["id"]],
        quality_preset="passthrough",
    )

    response = client.post("/v1/routes/dry-run", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["feasible"] is True
    assert payload["effective_quality_preset"] == "1080p"
    assert any(
        check["code"] == "transcode-required" and check["level"] == "warn"
        for check in payload["checks"]
    )
    assert "plan" not in payload
    assert payload["plan_summary"]["effective_quality_preset"] == "1080p"

    operator = client.post(
        "/internal/routes/dry-run", json=body, headers=OPERATOR_HEADERS
    ).json()
    assert operator["plan"]["provider"] == "aws"
    assert any(
        resource.startswith("transcode:") for resource in operator["plan"]["resources"]
    )


def test_dry_run_rejects_infeasible_intents(client, test_session_factory):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)

    past = client.post(
        "/v1/routes/dry-run",
        json=route_body(
            content["id"],
            feed["id"],
            [output["id"]],
            start=datetime.now(timezone.utc) - timedelta(hours=1),
        ),
    ).json()
    assert past["feasible"] is False
    assert any(check["code"] == "start-in-past" for check in past["checks"])

    start = future()
    bad_end = client.post(
        "/v1/routes/dry-run",
        json=route_body(
            content["id"],
            feed["id"],
            [output["id"]],
            start=start,
            end=start - timedelta(minutes=1),
        ),
    ).json()
    assert bad_end["feasible"] is False
    assert any(check["code"] == "invalid-schedule" for check in bad_end["checks"])

    missing_feed = client.post(
        "/v1/routes/dry-run",
        json=route_body(
            content["id"], feed["id"], [output["id"]], resilience="redundant"
        ),
    ).json()
    assert missing_feed["feasible"] is False
    assert any(
        check["code"] == "redundant-feed-required" for check in missing_feed["checks"]
    )

    redundant_feed = create_feed(client, name="feed-two", region="eu-west-1")
    missing_output = client.post(
        "/v1/routes/dry-run",
        json=route_body(
            content["id"],
            feed["id"],
            [output["id"]],
            redundant_feed_id=redundant_feed["id"],
            resilience="redundant",
        ),
    ).json()
    assert missing_output["feasible"] is False
    assert any(
        check["code"] == "redundant-output-required"
        for check in missing_output["checks"]
    )

    # Build an unavailable transport/region pair directly; the write API blocks it.
    from app.domains.sources import Source

    with test_session_factory() as session:
        unavailable = Source(
            name="ap-srt-caller",
            source_type="srtcallersource",
            capability_id="srt-caller",
            region="ap-southeast-2",
            config={"endpoint": "192.0.2.1", "port": 5000},
        )
        session.add(unavailable)
        session.commit()
        unavailable_id = str(unavailable.id)

    region = client.post(
        "/v1/routes/dry-run",
        json=route_body(content["id"], unavailable_id, [output["id"]]),
    ).json()
    assert region["feasible"] is False
    assert any(
        check["code"] == "feed-region-unavailable" for check in region["checks"]
    )

    # POST /v1/routes re-runs the same checks and cannot be bypassed by skipping dry-run.
    past_start = datetime.now(timezone.utc) - timedelta(hours=1)
    assert create_route(
        client, route_body(content["id"], feed["id"], [output["id"]], start=past_start)
    ).status_code == 422
    assert create_route(
        client,
        route_body(content["id"], feed["id"], [output["id"]], resilience="redundant"),
    ).status_code == 422
    assert create_route(
        client,
        route_body(
            content["id"],
            feed["id"],
            [output["id"]],
            redundant_feed_id=redundant_feed["id"],
            resilience="redundant",
        ),
    ).status_code == 422


def test_route_fan_out_creates_one_delivery_per_output_and_is_idempotent(
    client, test_session_factory
):
    content = create_content(client)
    feed = create_feed(client)
    first_output = create_output(client, name="out-one")
    second_output = create_output(client, name="out-two", region="eu-west-2")
    body = route_body(
        content["id"], feed["id"], [first_output["id"], second_output["id"]]
    )

    response = create_route(client, body, idempotency_key="route-create-1")
    assert response.status_code == 201, response.text
    route = response.json()
    assert len(route["outputs"]) == 2
    assert len(delivery_ids_for_route(client, route["id"])) == 2
    assert route["desired_state"] == "scheduled"
    assert route["observed_state"] == "pending"
    assert "resolved_plan" not in route
    assert "delivery_ids" not in route
    assert route["plan_summary"]["output_count"] == 2

    repeated = create_route(client, body, idempotency_key="route-create-1")
    assert repeated.status_code == 201
    assert repeated.json()["id"] == route["id"]

    with test_session_factory() as session:
        deliveries = session.scalars(
            select(Delivery).where(Delivery.route_id == UUID(route["id"]))
        ).all()
        events = session.scalars(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id.in_(
                    [delivery.id for delivery in deliveries]
                )
            )
        ).all()
    assert len(deliveries) == 2
    assert all(delivery.state == "PENDING" for delivery in deliveries)
    assert {event.event_type for event in events} == {"DeliveryStart", "DeliveryStop"}
    assert len(events) == 4

    events = client.get(f"/v1/routes/{route['id']}/events").json()
    assert [event["event_type"] for event in events] == ["route.scheduled"]


def test_overlapping_output_is_a_conflict(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)

    first = create_route(client, route_body(content["id"], feed["id"], [output["id"]]))
    assert first.status_code == 201

    overlapping = route_body(
        content["id"],
        feed["id"],
        [output["id"]],
        start=future() + timedelta(minutes=30),
    )
    dry_run = client.post("/v1/routes/dry-run", json=overlapping).json()
    assert dry_run["feasible"] is False
    assert any(check["code"] == "output-conflict" for check in dry_run["checks"])

    second = create_route(client, overlapping)
    assert second.status_code == 409


def test_route_lifecycle_follows_delivery_transitions(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()
    route_id = route["id"]
    delivery_id = delivery_ids_for_route(client, route["id"])[0]

    client.post(f"/internal/deliveries/{delivery_id}/start", headers=OPERATOR_HEADERS)
    assert client.get(f"/v1/routes/{route_id}").json()["observed_state"] == "provisioning"

    client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")
    live = client.get(f"/v1/routes/{route_id}").json()
    assert live["observed_state"] == "live"
    assert live["health"] == "healthy"
    assert live["desired_state"] == "active"

    events = client.get(f"/v1/routes/{route_id}/events").json()
    assert [event["event_type"] for event in events].count("route.started") == 1

    # Re-reading / re-syncing does not duplicate the transition event.
    assert client.get(f"/v1/routes/{route_id}").status_code == 200
    events = client.get(f"/v1/routes/{route_id}/events").json()
    assert [event["event_type"] for event in events].count("route.started") == 1

    client.post(f"/internal/deliveries/{delivery_id}/stop", headers=OPERATOR_HEADERS)
    assert client.get(f"/v1/routes/{route_id}").json()["observed_state"] == "stopping"
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-stopped")
    completed = client.get(f"/v1/routes/{route_id}").json()
    assert completed["observed_state"] == "completed"
    assert completed["desired_state"] == "stopped"
    events = client.get(f"/v1/routes/{route_id}/events").json()
    assert [event["event_type"] for event in events].count("route.completed") == 1


def test_failed_delivery_marks_route_failed(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()
    delivery_id = delivery_ids_for_route(client, route["id"])[0]

    client.post(f"/internal/deliveries/{delivery_id}/start", headers=OPERATOR_HEADERS)
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-failed")

    updated = client.get(f"/v1/routes/{route['id']}").json()
    assert updated["observed_state"] == "failed"
    assert updated["health"] == "degraded"
    events = client.get(f"/v1/routes/{route['id']}/events").json()
    assert [event["event_type"] for event in events].count("route.failed") == 1


def test_cancel_and_stop_route(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)

    cancelled = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()
    response = client.post(f"/v1/routes/{cancelled['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["desired_state"] == "cancelled"
    assert response.json()["observed_state"] == "completed"
    cancelled_delivery_ids = delivery_ids_for_route(client, cancelled["id"])
    assert cancelled_delivery_ids
    assert (
        client.get(f"/v1/deliveries/{cancelled_delivery_ids[0]}").json()["state"]
        == "CANCELLED"
    )
    events = client.get(f"/v1/routes/{cancelled['id']}/events").json()
    assert [event["event_type"] for event in events].count("route.cancelled") == 1

    stopped = create_route(
        client,
        route_body(
            content["id"],
            feed["id"],
            [create_output(client, name="stop-output")["id"]],
        ),
    ).json()
    response = client.post(f"/v1/routes/{stopped['id']}/stop")
    assert response.status_code == 200
    assert response.json()["desired_state"] == "stopped"
    events = client.get(f"/v1/routes/{stopped['id']}/events").json()
    assert [event["event_type"] for event in events].count("route.stopped") == 1
    # A repeated stop is idempotent and does not add another event.
    assert client.post(f"/v1/routes/{stopped['id']}/stop").status_code == 200
    events = client.get(f"/v1/routes/{stopped['id']}/events").json()
    assert [event["event_type"] for event in events].count("route.stopped") == 1


def test_redundant_route_maps_to_delivery_redundant_pair(client, test_session_factory):
    content = create_content(client)
    primary_feed = create_feed(client, name="primary-feed", region="eu-west-1")
    redundant_feed = create_feed(client, name="redundant-feed", region="eu-west-1")
    primary_output = create_output(client, name="primary-out", region="eu-west-1")
    redundant_output = create_output(client, name="redundant-out", region="eu-west-1")

    body = route_body(
        content["id"],
        primary_feed["id"],
        [primary_output["id"]],
        redundant_feed_id=redundant_feed["id"],
        redundant_output_ids=[redundant_output["id"]],
        resilience="redundant",
    )
    response = create_route(client, body)
    assert response.status_code == 201, response.text
    route = response.json()

    with test_session_factory() as session:
        delivery = session.scalar(
            select(Delivery).where(Delivery.route_id == UUID(route["id"]))
        )
    assert str(delivery.source_id) == primary_feed["id"]
    assert str(delivery.redundant_source_id) == redundant_feed["id"]
    assert str(delivery.destination_id) == primary_output["id"]
    assert str(delivery.redundant_destination_id) == redundant_output["id"]


def test_operator_auth_audit_and_retry(client, test_session_factory):
    assert client.get("/internal/deliveries").status_code == 401
    assert (
        client.get(
            "/internal/deliveries", headers={"X-Operator-Token": "wrong"}
        ).status_code
        == 401
    )
    assert client.get("/internal/deliveries", headers=OPERATOR_HEADERS).status_code == 200

    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()
    delivery_id = delivery_ids_for_route(client, route["id"])[0]

    listing = client.get("/internal/deliveries", headers=OPERATOR_HEADERS).json()
    assert listing[0]["route_id"] == route["id"]
    assert listing[0]["route"]["desired_state"] == "scheduled"

    stop_output = create_output(client, name="stop-audit-out", region="eu-west-2")
    stop_route = create_route(
        client, route_body(content["id"], feed["id"], [stop_output["id"]])
    ).json()
    client.post(
        f"/internal/deliveries/{delivery_ids_for_route(client, stop_route['id'])[0]}/stop",
        headers=OPERATOR_HEADERS,
    )
    audit = client.get("/internal/audit", headers=OPERATOR_HEADERS).json()
    assert any(row["action"] == "delivery.stop" for row in audit)

    client.post(f"/v1/deliveries/{delivery_id}/pipeline-failed")
    retried = client.post(
        f"/internal/deliveries/{delivery_id}/retry", headers=OPERATOR_HEADERS
    )
    assert retried.status_code == 200
    assert retried.json()["state"] == "STARTING"

    audit = client.get("/internal/audit", headers=OPERATOR_HEADERS).json()
    assert any(row["action"] == "delivery.retry" for row in audit)

    # Outbox retry path: a stuck pending event is forced due now.
    with test_session_factory() as session:
        event = session.scalar(
            select(DeliveryOutboxEvent).where(
                DeliveryOutboxEvent.delivery_id == UUID(delivery_id),
                DeliveryOutboxEvent.status == "PENDING",
            )
        )
        event.attempts = 1
        event.last_error = "publisher outage"
        event.due_at = future(5)
        event_id = str(event.id)
        session.commit()

    retried_event = client.post(
        f"/internal/outbox/{event_id}/retry", headers=OPERATOR_HEADERS
    )
    assert retried_event.status_code == 200
    assert retried_event.json()["due_at"] <= datetime.now(timezone.utc).isoformat()
    audit = client.get("/internal/audit", headers=OPERATOR_HEADERS).json()
    assert any(row["action"] == "outbox.retry" for row in audit)


def test_operator_plan_and_control_room_read_models(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()

    plan = client.get(
        f"/internal/plans/{route['id']}", headers=OPERATOR_HEADERS
    )
    assert plan.status_code == 200
    assert plan.json()["plan"]["provider"] == "aws"
    assert plan.json()["deliveries"][0]["delivery_id"] == delivery_ids_for_route(
        client, route["id"]
    )[0]

    summary = client.get("/v1/control-room/summary").json()
    assert summary["scheduled_routes"] >= 1
    assert summary["outputs_in_use"] >= 1

    upcoming = client.get("/v1/control-room/upcoming").json()
    assert any(row["id"] == route["id"] for row in upcoming)

    activity = client.get("/v1/control-room/activity").json()
    assert any(row["event_type"] == "route.scheduled" for row in activity)
