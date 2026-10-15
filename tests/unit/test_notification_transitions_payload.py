"""An upsert event without the optional payload is ignored, never raised out of the socket."""

import pytest

from relaydeck.gateway.notification_transitions import NotificationTransitions


@pytest.mark.parametrize("kind", ["agent.upserted", "pane.upserted"])
@pytest.mark.parametrize("payload", [None, "not-an-object"])
def test_upsert_without_object_payload_is_ignored(kind, payload):
    transitions = NotificationTransitions()
    event = {"type": kind, "resource_id": "w1:p1"}
    if payload is not None:
        event["payload"] = payload
    transitions.event("acct-1", "home-mac", event, 1_000)
    assert transitions.statuses == {} and transitions.pending == {}
