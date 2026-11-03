"""Customer surface vs operator surface.

These tests pin the architectural boundary: customers see intent and capability
summaries, never execution types, provider identifiers, delivery ids or resolved plans.
"""

from tests.test_p0 import (
    OPERATOR_HEADERS,
    create_content,
    create_feed,
    create_output,
    create_route,
    delivery_ids_for_route,
    route_body,
)

CUSTOMER_ROUTE_KEYS = {
    "id",
    "content_id",
    "feed_id",
    "redundant_feed_id",
    "quality_preset",
    "resilience",
    "desired_state",
    "observed_state",
    "health",
    "planned_start",
    "planned_end",
    "is_open_ended",
    "encrypted",
    "stop_reason",
    "outputs",
    "plan_summary",
    "created_at",
}


def test_connections_expose_a_capability_summary_only(client):
    feed = create_feed(client)
    assert set(feed) == {"id", "name", "capability", "region", "details", "created_at"}
    assert set(feed["capability"]) == {"id", "kind", "name", "mode"}
    assert feed["capability"]["id"] == "srt-listener"
    assert "source_type" not in feed
    assert "config" not in feed
    assert "availability_zone" not in feed
    assert "stream_profile_id" not in feed
    # Only standard (non-provider) values are echoed.
    assert feed["details"] == {"min_latency_ms": 200}

    output = create_output(
        client,
        name="customer-hls",
        capability_id="hls",
        config={
            "url": "https://example.test/hls",
            "output_destination_type": "s3",
            "name_modifier": "live",
            "cdn_settings_type": "none",
        },
    )
    assert set(output) == {"id", "name", "capability", "region", "details", "created_at"}
    assert output["capability"]["id"] == "hls"
    assert "destination_type" not in output
    assert "config" not in output
    assert output["details"] == {
        "url": "https://example.test/hls",
        "name_modifier": "live",
    }

    listed = client.get("/v1/feeds").json()
    assert set(listed[0]) == {"id", "name", "capability", "region", "details", "created_at"}


def test_route_representation_has_no_plan_or_kernel_ids(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()

    assert set(route) == CUSTOMER_ROUTE_KEYS
    assert "resolved_plan" not in route
    assert "plan_version" not in route
    assert "delivery_ids" not in route
    assert set(route["plan_summary"]) == {
        "effective_quality_preset",
        "estimated_start",
        "output_count",
        "resilience",
        "audio_tracks",
    }

    detail = client.get(f"/v1/routes/{route['id']}").json()
    assert set(detail) == CUSTOMER_ROUTE_KEYS

    plan = client.get(f"/v1/routes/{route['id']}/plan").json()
    assert set(plan) == {"route_id", "plan_summary"}
    assert "workflow" not in plan["plan_summary"]
    assert "region" not in plan["plan_summary"]
    assert "resources" not in plan["plan_summary"]

    upcoming = client.get("/v1/control-room/upcoming").json()
    assert all("resolved_plan" not in row for row in upcoming)


def test_route_events_drop_kernel_metadata(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    route = create_route(
        client, route_body(content["id"], feed["id"], [output["id"]])
    ).json()
    delivery_id = delivery_ids_for_route(client, route["id"])[0]

    client.post(
        f"/internal/deliveries/{delivery_id}/start", headers=OPERATOR_HEADERS
    )
    client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")

    events = client.get(f"/v1/routes/{route['id']}/events").json()
    started = next(event for event in events if event["event_type"] == "route.started")
    assert "delivery_states" not in started["metadata"]
    assert set(started["metadata"]) <= {
        "previous_state",
        "observed_state",
        "planned_start",
        "planned_end",
        "output_count",
        "resilience",
    }


def test_capabilities_and_presets_have_public_and_operator_views(client):
    public = client.get("/v1/capabilities/srt-listener").json()
    assert "legacy_source_type" not in public
    assert "legacy_destination_type" not in public
    assert "plan_recipe" not in public
    assert "transcode_recipe" not in public
    assert "fixed_config" not in public

    operator = client.get(
        "/internal/capabilities/srt-listener", headers=OPERATOR_HEADERS
    ).json()
    assert operator["legacy_source_type"] == "srtlistenersource"
    assert operator["plan_recipe"]
    assert operator["fixed_config"] == {}

    public_preset = next(
        preset
        for preset in client.get("/v1/quality-presets").json()["quality_presets"]
        if preset["id"] == "1080p"
    )
    assert "profile" not in public_preset

    operator_preset = next(
        preset
        for preset in client.get(
            "/internal/quality-presets", headers=OPERATOR_HEADERS
        ).json()["quality_presets"]
        if preset["id"] == "1080p"
    )
    assert operator_preset["profile"]["id"] == "profile-1080p"


def test_dry_run_returns_a_summary_not_a_plan(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    body = route_body(content["id"], feed["id"], [output["id"]])

    payload = client.post("/v1/routes/dry-run", json=body).json()
    assert payload["feasible"] is True
    assert "plan" not in payload
    assert set(payload["plan_summary"]) == {
        "effective_quality_preset",
        "estimated_start",
        "output_count",
        "resilience",
        "audio_tracks",
    }

    operator = client.post(
        "/internal/routes/dry-run", json=body, headers=OPERATOR_HEADERS
    ).json()
    assert operator["plan"]["provider"] == "aws"
    assert operator["plan"]["workflow"]
    assert operator["plan"]["resources"]


def test_legacy_execution_surface_is_operator_only(raw_client):
    for path in (
        "/v1/sources",
        "/v1/destinations",
        "/v1/stream-profiles",
        "/v1/deliveries",
    ):
        assert raw_client.get(path).status_code == 401, path

    # The same paths work with the operator token.
    assert (
        raw_client.get("/v1/sources", headers=OPERATOR_HEADERS).status_code == 200
    )


def test_customer_and_legacy_names_are_separate_domains(
    client, test_session_factory
):
    from app.domains.destinations import Destination
    from app.domains.sources import Source

    with test_session_factory() as session:
        session.add_all(
            [
                Source(
                    name="shared-name",
                    source_type="srtlistenersource",
                    capability_id=None,
                    region="eu-west-1",
                    config={},
                ),
                Destination(
                    name="shared-name",
                    destination_type="hlsdestination",
                    capability_id=None,
                    region="eu-west-1",
                    config={},
                ),
            ]
        )
        session.commit()

    # A customer connection may reuse the hidden legacy name...
    assert create_feed(client, name="shared-name")["capability"]["id"] == "srt-listener"
    assert create_output(client, name="shared-name")["capability"]["id"] == "linear"

    # ...but customer names are still unique within the customer domain.
    duplicate_feed = client.post(
        "/v1/feeds",
        json={
            "name": "shared-name",
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {
                "port": 5000,
                "min_latency_ms": 200,
                "whitelist_cidr": "192.0.2.0/24",
            },
        },
    )
    assert duplicate_feed.status_code == 409
    duplicate_output = client.post(
        "/v1/outputs",
        json={
            "name": "shared-name",
            "capability_id": "linear",
            "region": "eu-west-1",
            "config": {
                "ip_address": "192.0.2.10",
                "port": 6000,
                "ip_destination_type": "rtp",
            },
        },
    )
    assert duplicate_output.status_code == 409


def test_customer_connections_do_not_expose_legacy_rows(
    client, test_session_factory
):
    from app.domains.destinations import Destination
    from app.domains.sources import Source

    with test_session_factory() as session:
        legacy_source = Source(
            name="legacy-source",
            source_type="srtlistenersource",
            capability_id=None,
            region="eu-west-1",
            config={},
        )
        legacy_output = Destination(
            name="legacy-output",
            destination_type="hlsdestination",
            capability_id=None,
            region="eu-west-1",
            config={},
        )
        session.add_all([legacy_source, legacy_output])
        session.commit()
        source_id, output_id = str(legacy_source.id), str(legacy_output.id)

    assert client.get("/v1/feeds").json() == []
    assert client.get("/v1/outputs").json() == []
    assert client.get(f"/v1/feeds/{source_id}").status_code == 404
    assert client.get(f"/v1/outputs/{output_id}").status_code == 404
    assert client.patch(
        f"/v1/feeds/{source_id}", json={"name": "renamed"}
    ).status_code == 404
    assert client.patch(
        f"/v1/outputs/{output_id}", json={"name": "renamed"}
    ).status_code == 404
    assert client.delete(f"/v1/feeds/{source_id}").status_code == 404
    assert client.delete(f"/v1/outputs/{output_id}").status_code == 404
