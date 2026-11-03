"""Catalog corrections validated against the classic Arqade services.

Covers must-fix items 1-10 in docs/arqade-counterpart-review.md, while asserting that
execution detail stays on the operator surface.
"""

from datetime import timedelta
from uuid import UUID

from app.domains.sources import Source
from tests.test_p0 import (
    OPERATOR_HEADERS,
    create_content,
    create_feed,
    create_output,
    create_route,
    future,
    route_body,
)


def dry_run(client, body):
    response = client.post("/v1/routes/dry-run", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def operator_dry_run(client, body):
    response = client.post(
        "/internal/routes/dry-run", json=body, headers=OPERATOR_HEADERS
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_rist_is_an_ipsource_capability(client, test_session_factory):
    public = client.get("/v1/capabilities/rist").json()
    assert public["kind"] == "transport"
    assert "legacy_source_type" not in public
    assert {field["name"] for field in public["required_fields"]} == {
        "port",
        "whitelist_cidr",
        "max_bitrate_bps",
    }

    operator = client.get(
        "/internal/capabilities/rist", headers=OPERATOR_HEADERS
    ).json()
    assert operator["legacy_source_type"] == "ipsource"

    feed = create_feed(
        client,
        name="rist-feed",
        capability_id="rist",
        config={"port": 5000, "whitelist_cidr": "192.0.2.0/24"},
    )
    assert feed["capability"]["id"] == "rist"
    assert "config" not in feed

    with test_session_factory() as session:
        stored = session.get(Source, UUID(feed["id"]))
        assert stored.source_type == "ipsource"
        assert stored.config["protocol"] == "rist"

    content = create_content(client)
    output = create_output(client)
    payload = dry_run(client, route_body(content["id"], feed["id"], [output["id"]]))
    assert payload["feasible"] is True, payload["checks"]
    assert not any(
        check["code"] == "capability-not-executable" for check in payload["checks"]
    )


def test_mediaconnect_requires_an_entitlement_arn(client):
    capability = client.get("/v1/capabilities/mediaconnect").json()
    fields = {field["name"] for field in capability["required_fields"]}
    assert "entitlement_arn" in fields
    assert "flow_arn" not in fields
    assert "legacy_source_type" not in capability

    missing = client.post(
        "/v1/feeds",
        json={
            "name": "mc-feed-missing",
            "capability_id": "mediaconnect",
            "region": "eu-west-1",
            "config": {},
        },
    )
    assert missing.status_code == 422

    feed = create_feed(
        client,
        name="mc-feed",
        capability_id="mediaconnect",
        config={"entitlement_arn": "arn:aws:mediaconnect:eu-west-1:123:entitlement:a:b"},
    )
    assert feed["capability"]["id"] == "mediaconnect"
    # The entitlement ARN is provider detail and is not echoed back.
    assert feed["details"] == {}


def test_transport_fields_and_types_are_validated(client):
    missing_latency = client.post(
        "/v1/feeds",
        json={
            "name": "srt-missing-latency",
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {"port": 5000, "whitelist_cidr": "192.0.2.0/24"},
        },
    )
    assert missing_latency.status_code == 422

    bad_cidr = client.post(
        "/v1/feeds",
        json={
            "name": "srt-bad-cidr",
            "capability_id": "srt-listener",
            "region": "eu-west-1",
            "config": {
                "port": 5000,
                "min_latency_ms": 200,
                "whitelist_cidr": "not-a-cidr",
            },
        },
    )
    assert bad_cidr.status_code == 422

    zixi_missing_whitelist = client.post(
        "/v1/feeds",
        json={
            "name": "zixi-missing-whitelist",
            "capability_id": "zixi",
            "region": "eu-west-1",
            "config": {"port": 5000},
        },
    )
    assert zixi_missing_whitelist.status_code == 422

    bad_ip_type = client.post(
        "/v1/outputs",
        json={
            "name": "bad-ip-type",
            "capability_id": "linear",
            "region": "eu-west-1",
            "config": {
                "ip_address": "192.0.2.10",
                "port": 6000,
                "ip_destination_type": "srt",
            },
        },
    )
    assert bad_ip_type.status_code == 422


def test_dash_requires_cdn_arn_only_for_cdn_egress(client):
    cdn_missing = client.post(
        "/v1/outputs",
        json={
            "name": "dash-cdn-missing",
            "capability_id": "dash",
            "region": "eu-west-1",
            "config": {"egress_access": "cdn"},
        },
    )
    assert cdn_missing.status_code == 422

    cdn = create_output(
        client,
        name="dash-cdn",
        capability_id="dash",
        config={
            "egress_access": "cdn",
            "cdn_distribution_arn": "arn:aws:cloudfront::123:distribution/ABC",
        },
    )
    assert cdn["capability"]["id"] == "dash"
    assert cdn["details"] == {"egress_access": "cdn"}
    assert "url" not in {
        field["name"]
        for field in client.get("/v1/capabilities/dash").json()["required_fields"]
    }

    public = create_output(
        client,
        name="dash-public",
        capability_id="dash",
        config={"egress_access": "public"},
    )
    assert public["capability"]["id"] == "dash"
    assert public["details"] == {"egress_access": "public"}


def test_archive_transcodes_and_the_plan_binds_a_profile(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(
        client,
        name="archive-out",
        capability_id="archive",
        config={"bucket_name": "archive-bucket", "folder_path": "live"},
    )
    body = route_body(
        content["id"],
        feed["id"],
        [output["id"]],
        quality_preset="passthrough",
    )
    customer = dry_run(client, body)
    assert customer["feasible"] is True, customer["checks"]
    assert customer["effective_quality_preset"] == "1080p"
    assert "plan" not in customer
    assert customer["plan_summary"]["effective_quality_preset"] == "1080p"
    assert customer["plan_summary"]["output_count"] == 1

    plan = operator_dry_run(client, body)["plan"]
    assert plan["stream_profile"]["id"] == "profile-1080p"
    assert plan["audio_tracks"] == 4
    assert "transcode:1080p" in plan["resources"]
    assert "stream_profile:profile-1080p" in plan["resources"]
    assert "audio:4" in plan["resources"]
    assert any(
        resource.endswith(":medialive_archive_group") for resource in plan["resources"]
    )
    assert plan["workflow"] == "s3_same_region_single"


def test_rtmp_forces_one_audio_track(client):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(
        client,
        name="rtmp-out",
        capability_id="rtmp",
        config={
            "url": "rtmp://example.test/live",
            "stream_name": "secret-key",
        },
    )
    plan = operator_dry_run(
        client, route_body(content["id"], feed["id"], [output["id"]])
    )["plan"]
    assert plan["audio_tracks"] == 1
    assert "audio:1" in plan["resources"]
    assert plan["workflow"] == "rtmp_same_region_single"


def test_cross_region_plan_uses_the_router_workflow(client):
    content = create_content(client)
    feed = create_feed(client, region="eu-west-1")
    output = create_output(client, name="us-out", region="us-east-1")
    plan = operator_dry_run(
        client, route_body(content["id"], feed["id"], [output["id"]])
    )["plan"]
    assert plan["cross_region"] is True
    assert plan["workflow"] == "direct_cross_region_with_router_single"
    assert "router:mediaconnect_router" in plan["resources"]

    hls_output = create_output(
        client,
        name="hls-out",
        capability_id="hls",
        config={
            "url": "https://example.test/hls",
            "output_destination_type": "s3",
            "name_modifier": "live",
            "cdn_settings_type": "none",
        },
    )
    hls = operator_dry_run(
        client,
        route_body(
            content["id"], feed["id"], [hls_output["id"]], start=future(4)
        ),
    )
    assert hls["plan"]["workflow"] == "hls_same_region_single"


def test_schedule_rules_match_the_existing_platform(client):
    event = create_content(client, name="event-one", content_type="event")
    feed = create_feed(client)
    output = create_output(client)

    open_ended = dry_run(
        client,
        route_body(
            event["id"],
            feed["id"],
            [output["id"]],
            is_open_ended=True,
            end=None,
        ),
    )
    assert open_ended["feasible"] is False
    assert any(
        check["code"] == "event-open-ended" for check in open_ended["checks"]
    )

    channel = create_content(client, name="channel-two")
    start = future()
    too_short = dry_run(
        client,
        route_body(
            channel["id"],
            feed["id"],
            [output["id"]],
            start=start,
            end=start + timedelta(minutes=5),
        ),
    )
    assert too_short["feasible"] is False
    assert any(
        check["code"] == "schedule-too-short" for check in too_short["checks"]
    )

    minimum = dry_run(
        client,
        route_body(
            channel["id"],
            feed["id"],
            [output["id"]],
            start=start,
            end=start + timedelta(minutes=10),
        ),
    )
    assert minimum["feasible"] is True, minimum["checks"]


def test_previously_missing_output_capabilities_are_served(client):
    catalog = client.get("/v1/capabilities").json()
    formats = {item["id"]: item for item in catalog["formats"]}
    assert len(formats) == 11
    for capability_id in (
        "srt-listener-output",
        "mediaconnect-output",
        "zixipush",
        "zixipull",
        "mediashuttle",
    ):
        assert capability_id in formats
        assert "legacy_destination_type" not in formats[capability_id]

    operator_catalog = client.get(
        "/internal/capabilities", headers=OPERATOR_HEADERS
    ).json()
    operator_formats = {item["id"]: item for item in operator_catalog["formats"]}
    for capability_id in (
        "srt-listener-output",
        "mediaconnect-output",
        "zixipush",
        "zixipull",
        "mediashuttle",
    ):
        assert operator_formats[capability_id]["legacy_destination_type"]

    zixi_push = create_output(
        client,
        name="zixi-push-out",
        capability_id="zixipush",
        config={"ip_address": "192.0.2.20", "port": 7000},
    )
    assert zixi_push["capability"]["id"] == "zixipush"

    shuttle = create_output(
        client, name="shuttle-out", capability_id="mediashuttle", config={}
    )
    assert shuttle["capability"]["id"] == "mediashuttle"


def test_passthrough_has_no_profile(client):
    preset = {
        item["id"]: item
        for item in client.get("/v1/quality-presets").json()["quality_presets"]
    }["passthrough"]
    assert "profile" not in preset
    assert preset["ladder"] == []

    operator_preset = {
        item["id"]: item
        for item in client.get(
            "/internal/quality-presets", headers=OPERATOR_HEADERS
        ).json()["quality_presets"]
    }["passthrough"]
    assert operator_preset["profile"] is None

    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client)
    plan = operator_dry_run(
        client,
        route_body(
            content["id"],
            feed["id"],
            [output["id"]],
            quality_preset="passthrough",
        ),
    )["plan"]
    assert plan["stream_profile"] is None
    assert not any(
        resource.startswith("transcode:") for resource in plan["resources"]
    )


def test_route_create_rejects_a_short_schedule(client):
    channel = create_content(client, name="channel-short")
    feed = create_feed(client)
    output = create_output(client)
    start = future()
    response = create_route(
        client,
        route_body(
            channel["id"],
            feed["id"],
            [output["id"]],
            start=start,
            end=start + timedelta(minutes=5),
        ),
    )
    assert response.status_code == 422
    assert any(
        check["code"] == "schedule-too-short"
        for check in response.json()["detail"]["checks"]
    )
