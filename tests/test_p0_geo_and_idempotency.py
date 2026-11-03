from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.domains.sources import Source
from tests.test_p0 import OPERATOR_HEADERS


def future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def create_content(client, name="geo-content"):
    response = client.post(
        "/v1/content",
        json={
            "name": name,
            "content_type": "channel",
            "owner": "operations",
            "description": "geo and idempotency test content",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_feed(client, name, region="eu-west-1"):
    response = client.post(
        "/v1/feeds",
        json={
            "name": name,
            "capability_id": "srt-listener",
            "region": region,
            "config": {
                "port": 5000,
                "min_latency_ms": 200,
                "whitelist_cidr": "192.0.2.0/24",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_output(client, name, region="eu-west-1"):
    response = client.post(
        "/v1/outputs",
        json={
            "name": name,
            "capability_id": "linear",
            "region": region,
            "config": {
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
):
    start = start or future()
    if end is None:
        end = start + timedelta(hours=1)
    body = {
        "content_id": content_id,
        "feed_id": feed_id,
        "redundant_feed_id": redundant_feed_id,
        "quality_preset": quality_preset,
        "resilience": resilience,
        "output_ids": output_ids,
        "planned_start": start.isoformat(),
        "planned_end": end.isoformat(),
        "is_open_ended": False,
        "encrypted": False,
    }
    if redundant_output_ids is not None:
        body["redundant_output_ids"] = redundant_output_ids
    return body


def create_route(client, body, idempotency_key=None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post("/v1/routes", json=body, headers=headers)


def test_geo_redundant_requires_primary_outputs_in_two_regions(client):
    content = create_content(client)
    primary_feed = create_feed(client, "geo-feed-primary", "eu-west-1")
    redundant_feed = create_feed(client, "geo-feed-redundant", "eu-west-1")
    eu_primary = create_output(client, "geo-out-eu", "eu-west-1")
    eu_redundant = create_output(client, "geo-out-eu-r", "eu-west-1")
    us_primary = create_output(client, "geo-out-us", "us-east-1")
    us_redundant = create_output(client, "geo-out-us-r", "us-east-1")

    body = route_body(
        content["id"],
        primary_feed["id"],
        [eu_primary["id"], us_primary["id"]],
        redundant_feed_id=redundant_feed["id"],
        redundant_output_ids=[eu_redundant["id"], us_redundant["id"]],
        resilience="geo-redundant",
    )
    dry_run = client.post("/v1/routes/dry-run", json=body)
    assert dry_run.status_code == 200, dry_run.text
    payload = dry_run.json()
    assert payload["feasible"] is True, payload["checks"]
    assert not any(
        check["code"] == "geo-redundancy-requires-two-regions"
        for check in payload["checks"]
    )
    assert "plan" not in payload
    assert payload["plan_summary"]["resilience"] == "geo-redundant"
    assert payload["plan_summary"]["output_count"] == 2

    # Region/workflow detail is operator-only.
    operator = client.post(
        "/internal/routes/dry-run", json=body, headers=OPERATOR_HEADERS
    ).json()
    assert operator["plan"]["region"] == "eu-west-1"

    created = create_route(client, body)
    assert created.status_code == 201, created.text

    # A redundant output does not add a region: one primary region still fails.
    same_region_primary = create_output(client, "geo-out-eu-2", "eu-west-1")
    same_region_redundant = create_output(client, "geo-out-eu-2-r", "eu-west-1")
    same_region = route_body(
        content["id"],
        primary_feed["id"],
        [same_region_primary["id"]],
        redundant_feed_id=redundant_feed["id"],
        redundant_output_ids=[same_region_redundant["id"]],
        resilience="geo-redundant",
        start=future(4),
    )
    rejected = client.post("/v1/routes/dry-run", json=same_region).json()
    assert rejected["feasible"] is False
    assert any(
        check["code"] == "geo-redundancy-requires-two-regions"
        for check in rejected["checks"]
    )
    assert create_route(client, same_region).status_code == 422


def test_redundant_pairs_must_be_same_region(client, test_session_factory):
    content = create_content(client)
    feed = create_feed(client, "pair-feed", "eu-west-1")
    output = create_output(client, "pair-out", "eu-west-1")
    redundant_output = create_output(client, "pair-out-r", "eu-west-1")

    cross_region_feed = create_feed(client, "pair-feed-us", "us-east-1")
    body = route_body(
        content["id"],
        feed["id"],
        [output["id"]],
        redundant_feed_id=cross_region_feed["id"],
        redundant_output_ids=[redundant_output["id"]],
        resilience="redundant",
    )
    payload = client.post("/v1/routes/dry-run", json=body).json()
    assert payload["feasible"] is False
    assert any(
        check["code"] == "redundant-feed-region-mismatch"
        for check in payload["checks"]
    )
    assert create_route(client, body).status_code == 422

    same_region_feed = create_feed(client, "pair-feed-eu", "eu-west-1")
    cross_region_output = create_output(client, "pair-out-us", "us-east-1")
    body = route_body(
        content["id"],
        feed["id"],
        [output["id"]],
        redundant_feed_id=same_region_feed["id"],
        redundant_output_ids=[cross_region_output["id"]],
        resilience="redundant",
    )
    payload = client.post("/v1/routes/dry-run", json=body).json()
    assert payload["feasible"] is False
    assert any(
        check["code"] == "redundant-output-region-mismatch"
        for check in payload["checks"]
    )
    assert create_route(client, body).status_code == 422

    # Same region but the same AZ is invalid when both zones are set internally.
    same_az_feed = create_feed(client, "pair-feed-az", "eu-west-1")
    primary_az_feed = create_feed(client, "pair-feed-az-primary", "eu-west-1")
    with test_session_factory() as session:
        for feed in (same_az_feed, primary_az_feed):
            stored = session.get(Source, UUID(feed["id"]))
            stored.availability_zone = "eu-west-1a"
        session.commit()
    body = route_body(
        content["id"],
        primary_az_feed["id"],
        [output["id"]],
        redundant_feed_id=same_az_feed["id"],
        redundant_output_ids=[redundant_output["id"]],
        resilience="redundant",
    )
    payload = client.post("/v1/routes/dry-run", json=body).json()
    assert payload["feasible"] is False
    assert any(
        check["code"] == "redundant-feed-same-az" for check in payload["checks"]
    )


def test_duplicate_redundant_feed_is_rejected(client):
    content = create_content(client)
    feed = create_feed(client, "dup-feed")
    primary_output = create_output(client, "dup-out-primary", region="eu-west-1")
    redundant_output = create_output(client, "dup-out-redundant", region="us-east-1")

    body = route_body(
        content["id"],
        feed["id"],
        [primary_output["id"]],
        redundant_feed_id=feed["id"],
        redundant_output_ids=[redundant_output["id"]],
        resilience="redundant",
    )

    dry_run = client.post("/v1/routes/dry-run", json=body).json()
    assert dry_run["feasible"] is False
    assert any(
        check["code"] == "duplicate-redundant-feed" and check["level"] == "error"
        for check in dry_run["checks"]
    )

    assert create_route(client, body).status_code == 422


def test_idempotency_key_binds_to_request_body(client):
    content = create_content(client)
    feed = create_feed(client, "idem-feed")
    output = create_output(client, "idem-out")

    start = future(2)
    body = route_body(content["id"], feed["id"], [output["id"]], start=start)
    first = create_route(client, body, idempotency_key="same-key-1")
    assert first.status_code == 201, first.text

    repeated = create_route(client, body, idempotency_key="same-key-1")
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["id"] == first.json()["id"]

    different = route_body(
        content["id"], feed["id"], [output["id"]], start=future(5)
    )
    conflict = create_route(client, different, idempotency_key="same-key-1")
    assert conflict.status_code == 409, conflict.text
    assert "different request" in conflict.json()["detail"]
