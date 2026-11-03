from uuid import UUID

from app.domains.route_lifecycle import sync_route_lifecycle
from app.domains.routes import Route
from tests.test_p0 import (
    create_content,
    create_feed,
    create_output,
    create_route,
    delivery_ids_for_route,
    route_body,
)

LEGACY_CANCEL_DETAIL = "Delivery can no longer be cancelled"
INTERNAL_STOP_DETAIL = "Delivery cannot be stopped"


def event_types(client, route_id):
    events = client.get(f"/v1/routes/{route_id}/events").json()
    return [event["event_type"] for event in events]


def start_and_go_live(client, delivery_id, operator_headers):
    started = client.post(
        f"/internal/deliveries/{delivery_id}/start", headers=operator_headers
    )
    assert started.status_code == 200, started.text
    live = client.post(f"/v1/deliveries/{delivery_id}/pipeline-started")
    assert live.status_code == 200, live.text
    assert live.json()["state"] == "IN_PROGRESS"


def create_single_leg_route(client, name="review-output"):
    content = create_content(client)
    feed = create_feed(client)
    output = create_output(client, name=name)
    route = create_route(client, route_body(content["id"], feed["id"], [output["id"]]))
    assert route.status_code == 201, route.text
    return route.json()


def test_legacy_cancel_maps_all_cancelled_route_to_cancelled(
    client, test_session_factory
):
    route = create_single_leg_route(client)
    route_id = route["id"]
    delivery_id = delivery_ids_for_route(client, route["id"])[0]

    cancelled = client.patch(f"/v1/deliveries/{delivery_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"

    updated = client.get(f"/v1/routes/{route_id}").json()
    assert updated["observed_state"] == "completed"
    assert updated["desired_state"] == "cancelled"
    assert updated["stop_reason"] == "CANCELLED"

    events = event_types(client, route_id)
    assert events.count("route.cancelled") == 1
    assert events.count("route.completed") == 0
    cancelled_events = [
        event
        for event in client.get(f"/v1/routes/{route_id}/events").json()
        if event["event_type"] == "route.cancelled"
    ]
    assert cancelled_events[0]["level"] == "neutral"

    # A re-entrant route cancel is a no-op, and a direct re-sync derives the same state.
    assert client.post(f"/v1/routes/{route_id}/cancel").status_code == 200
    with test_session_factory() as session:
        sync_route_lifecycle(session, session.get(Route, UUID(route_id)))
        session.commit()

    events = event_types(client, route_id)
    assert events.count("route.cancelled") == 1
    assert events.count("route.completed") == 0
    updated = client.get(f"/v1/routes/{route_id}").json()
    assert updated["desired_state"] == "cancelled"
    assert updated["observed_state"] == "completed"


def test_internal_stop_rejects_a_stopping_delivery(client, operator_headers):
    route = create_single_leg_route(client)
    delivery_id = delivery_ids_for_route(client, route["id"])[0]
    start_and_go_live(client, delivery_id, operator_headers)

    first = client.post(
        f"/internal/deliveries/{delivery_id}/stop", headers=operator_headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "STOPPING"

    repeated = client.post(
        f"/internal/deliveries/{delivery_id}/stop", headers=operator_headers
    )
    assert repeated.status_code == 409
    assert repeated.json()["detail"] == INTERNAL_STOP_DETAIL


def test_legacy_cancel_rejects_a_stopping_delivery(client, operator_headers):
    route = create_single_leg_route(client)
    delivery_id = delivery_ids_for_route(client, route["id"])[0]
    start_and_go_live(client, delivery_id, operator_headers)

    assert (
        client.post(
            f"/internal/deliveries/{delivery_id}/stop", headers=operator_headers
        ).status_code
        == 200
    )

    repeated = client.patch(f"/v1/deliveries/{delivery_id}/cancel")
    assert repeated.status_code == 409
    assert repeated.json()["detail"] == LEGACY_CANCEL_DETAIL


def test_legacy_cancel_rejects_a_complete_delivery(client, operator_headers):
    route = create_single_leg_route(client)
    delivery_id = delivery_ids_for_route(client, route["id"])[0]
    start_and_go_live(client, delivery_id, operator_headers)

    assert (
        client.post(
            f"/internal/deliveries/{delivery_id}/stop", headers=operator_headers
        ).status_code
        == 200
    )
    completed = client.post(f"/v1/deliveries/{delivery_id}/pipeline-stopped")
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "COMPLETE"

    rejected = client.patch(f"/v1/deliveries/{delivery_id}/cancel")
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == LEGACY_CANCEL_DETAIL


def test_natural_completion_still_emits_route_completed(client, operator_headers):
    route = create_single_leg_route(client)
    route_id = route["id"]
    delivery_id = delivery_ids_for_route(client, route["id"])[0]
    start_and_go_live(client, delivery_id, operator_headers)

    assert (
        client.post(
            f"/internal/deliveries/{delivery_id}/stop", headers=operator_headers
        ).status_code
        == 200
    )
    assert client.post(f"/v1/deliveries/{delivery_id}/pipeline-stopped").status_code == 200

    updated = client.get(f"/v1/routes/{route_id}").json()
    assert updated["observed_state"] == "completed"
    assert updated["desired_state"] == "stopped"

    events = event_types(client, route_id)
    assert events.count("route.completed") == 1
    assert events.count("route.cancelled") == 0


def test_stopping_a_cancelled_route_is_a_no_op(client):
    route = create_single_leg_route(client)
    route_id = route["id"]

    assert client.post(f"/v1/routes/{route_id}/cancel").status_code == 200

    stopped = client.post(f"/v1/routes/{route_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["desired_state"] == "cancelled"
    assert stopped.json()["stop_reason"] == "CANCELLED"

    events = event_types(client, route_id)
    assert events.count("route.cancelled") == 1
    assert events.count("route.stopped") == 0
