"""Paired Console grants manage only their issued credential/push identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway.app import GatewayState, create_app
from tests.security.helpers import auth
from tests.support.pairing import ORIGIN, foundation, proof

OTHER = "other-host-device"


def paired_device(path: Path) -> tuple[TestClient, GatewayState, dict[str, Any], str]:
    state, clock, body = foundation(path)
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, clock[0], 120)
    client = TestClient(create_app(state))
    request = client.post("/v1/pairing/requests", json=body).json()
    store.decide(
        request["request_id"], request["matching_code"], host, ORIGIN, clock[0], approve=True
    )
    tokens = client.post(f"/v1/pairing/{request['request_id']}/redeem", json=proof()).json()
    own = request["device_id"]
    return client, state, tokens, own


def test_paired_device_is_invisible_to_foreign_admin_actions_and_refresh(tmp_path: Path) -> None:
    client, state, tokens, own = paired_device(tmp_path / "state")
    foreign_access, foreign_refresh = state.sessions.create(
        "owner", DEVICE_CAPABILITIES, ("lab-mac",), state.now_ms(), OTHER
    )
    foreign_session = state.sessions.lookup(foreign_access, state.now_ms())
    subscription = state.push.register_device(
        "owner", OTHER, "synthetic-other-push", "android", state.now_ms()
    )
    headers = auth(tokens["access_token"])
    for refreshed in (False, True):
        if refreshed:
            rotated = client.post(
                "/v1/token",
                json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]},
            )
            assert rotated.status_code == 200
            headers = auth(rotated.json()["access_token"])
        devices = client.get("/v1/devices", headers=headers).json()
        assert [row["device_id"] for row in devices["devices"]] == [own]
        assert devices["current_device_id"] == own and not devices["truncated"]
        assert (
            client.post(
                "/v1/devices",
                headers=headers,
                json={"device_id": OTHER, "token": "attack-register", "platform": "android"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/v1/devices/{OTHER}/rotate", headers=headers, json={"token": "attack-rotate"}
            ).status_code
            == 404
        )
        assert client.delete(f"/v1/devices/{OTHER}", headers=headers).status_code == 404
        assert state.sessions.lookup(foreign_access, state.now_ms()) == foreign_session
        assert state.push.token_for("owner", OTHER) == subscription.token
        assert state.push.device_active("owner", OTHER)
    # The other credential's refresh is still valid; paired foreign attempts never consume it.
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": foreign_refresh}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/devices",
            headers=headers,
            json={"device_id": own, "token": "synthetic-own-push", "platform": "android"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/v1/devices/{own}/rotate", headers=headers, json={"token": "synthetic-own-rotated"}
        ).status_code
        == 200
    )
    assert state.push.token_for("owner", own) == "synthetic-own-rotated"
    assert client.delete(f"/v1/devices/{own}", headers=headers).status_code == 200
    assert not state.push.device_active("owner", own)
    assert client.get("/v1/me", headers=headers).status_code == 401
    assert state.push.token_for("owner", OTHER) == subscription.token


def test_paired_metadata_hides_account_wide_truncation(tmp_path: Path) -> None:
    client, state, tokens, own = paired_device(tmp_path / "state")
    for i in range(201):
        state.sessions.create(
            "owner", DEVICE_CAPABILITIES, ("lab-mac",), state.now_ms(), f"other-{i}"
        )
        state.push.register_device(
            "owner", f"push-only-{i}", f"synthetic-push-{i}", "android", state.now_ms()
        )
    response = client.get("/v1/devices", headers=auth(tokens["access_token"]))
    assert response.status_code == 200
    assert [row["device_id"] for row in response.json()["devices"]] == [own]
    assert not response.json()["truncated"]


def test_paired_logout_ignores_another_device_ref_and_preserves_its_outbox(tmp_path: Path) -> None:
    client, state, tokens, own = paired_device(tmp_path / "state")
    foreign_access, foreign_refresh = state.sessions.create(
        "owner", DEVICE_CAPABILITIES, ("lab-mac",), state.now_ms(), OTHER
    )
    foreign = state.push.register_device(
        "owner", OTHER, "synthetic-other-push", "android", state.now_ms()
    )
    state.push.register_device("owner", own, "synthetic-own-push", "android", state.now_ms())
    notes = state.push.notify(
        account="owner",
        account_hosts=frozenset({"lab-mac"}),
        host_id="lab-mac",
        session_id="synthetic-session",
        event_id="synthetic-event",
        category="attention",
        now=state.now_ms(),
        device_ids=frozenset({OTHER}),
    )
    assert len(notes) == 1
    note = state.push.get_notification(notes[0]["notification_id"])
    assert (
        client.post(
            "/v1/logout",
            headers=auth(tokens["access_token"]),
            json={"device_ref": foreign.device_ref},
        ).status_code
        == 200
    )
    assert state.push.token_for("owner", OTHER) == foreign.token
    assert state.push.get_notification(notes[0]["notification_id"]) == note
    assert state.sessions.lookup(foreign_access, state.now_ms()) is not None
    assert state.sessions.refresh(foreign_refresh, state.now_ms()) is not None
    assert not state.push.device_active("owner", own)
    assert client.get("/v1/me", headers=auth(tokens["access_token"])).status_code == 401
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


def test_notification_refs_are_bound_to_paired_device_and_host(tmp_path: Path) -> None:
    client, state, tokens, own = paired_device(tmp_path / "state")
    state.push.register_device("owner", own, "synthetic-own-push", "android", state.now_ms())
    state.push.register_device("owner", OTHER, "synthetic-other-push", "android", state.now_ms())
    notes = []
    for host, device, event in [
        ("home-mac", own, "own"),
        ("home-mac", OTHER, "foreign-device"),
        ("lab-mac", own, "foreign-host"),
    ]:
        notes.extend(
            state.push.notify(
                account="owner",
                account_hosts=frozenset({host}),
                host_id=host,
                session_id="synthetic-session",
                event_id=event,
                category="attention",
                now=state.now_ms(),
                device_ids=frozenset({device}),
            )
        )
    assert len(notes) == 3
    headers = auth(tokens["access_token"])
    for i, note in enumerate(notes):
        for key, value in [
            ("token", note["link_token"]),
            ("notification_id", note["notification_id"]),
        ]:
            response = client.post("/v1/deep-links/resolve", headers=headers, json={key: value})
            assert response.status_code == (200 if i == 0 else 404)
    assert state.push.token_for("owner", OTHER) == "synthetic-other-push"


def test_unpaired_owner_keeps_explicit_account_device_management(tmp_path: Path) -> None:
    client, state, tokens, own = paired_device(tmp_path / "state")
    owner, _ = state.sessions.create(
        "owner", DEVICE_CAPABILITIES, ("lab-mac",), state.now_ms(), "local-owner"
    )
    headers = auth(owner)
    assert own in {
        row["device_id"] for row in client.get("/v1/devices", headers=headers).json()["devices"]
    }
    assert (
        client.post(
            "/v1/devices",
            headers=headers,
            json={"device_id": own, "token": "owner-managed-push", "platform": "android"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/v1/devices/{own}/rotate", headers=headers, json={"token": "owner-rotated-push"}
        ).status_code
        == 200
    )
    assert client.delete(f"/v1/devices/{own}", headers=headers).status_code == 200
    assert client.get("/v1/me", headers=auth(tokens["access_token"])).status_code == 401
