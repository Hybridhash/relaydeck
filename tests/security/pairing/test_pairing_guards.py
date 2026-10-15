"""Real gateway guards stay closed until a scoped Mac approval is redeemed."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relaydeck.contracts.pairing import DEVICE_CAPABILITIES
from relaydeck.gateway.app import create_app
from tests.security.helpers import auth
from tests.support.pairing import ORIGIN, foundation, proof


def test_scoped_session_refresh_revoke_and_pre_auth_denial(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    client = TestClient(create_app(state))
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    assert client.post("/v1/pairing/requests", json=body).status_code == 404
    store.open_window(host, ORIGIN, now[0], 120)
    row = client.post("/v1/pairing/requests", json=body).json()
    for path in ("/v1/me", "/v1/hosts", "/v1/hosts/home-mac/agents"):
        assert client.get(path).status_code == 401
    assert (
        client.post(
            "/v1/hosts/home-mac/terminals/term-1/attachments",
            json={"kind": "shell", "takeover": False},
        ).status_code
        == 401
    )
    assert client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof()).status_code == 404
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/v1/terminal/unknown"):
            pytest.fail("anonymous terminal accepted")
    assert closed.value.code == 1008
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    response = client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof())
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    me = client.get("/v1/me", headers=auth(tokens["access_token"])).json()
    assert me["account"] == "owner"
    assert me["device_id"] == row["device_id"]
    assert me["hosts"] == ["home-mac"]  # lab-mac has the SAME owner; must not inherit it.
    assert set(me["capabilities"]) == set(DEVICE_CAPABILITIES)
    assert (
        client.get("/v1/hosts/lab-mac/agents", headers=auth(tokens["access_token"])).status_code
        == 404
    )
    assert (
        client.post("/v1/enrollments", headers=auth(tokens["access_token"]), json={}).status_code
        == 403
    )
    assert (
        state.sessions.notification_devices("owner", "lab-mac", now[0], owned=True) == frozenset()
    )
    assert client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof()).status_code == 404
    rotated = client.post(
        "/v1/token", json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]}
    ).json()
    assert client.get("/v1/me", headers=auth(rotated["access_token"])).json() == me
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.delete(
            f"/v1/devices/{me['device_id']}", headers=auth(rotated["access_token"])
        ).status_code
        == 200
    )
    assert client.get("/v1/me", headers=auth(rotated["access_token"])).status_code == 401
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": rotated["refresh_token"]}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "attack",
    [
        "origin",
        "host",
        "verifier",
        "claims",
        "header",
        "size",
        "label",
        "duplicate",
        "duplicate-origin",
    ],
)
def test_request_and_redemption_attacks(tmp_path: Path, attack: str) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    client = TestClient(create_app(state))
    row = client.post("/v1/pairing/requests", json=body).json()
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    if attack in {"origin", "host", "verifier"}:
        key, value = {
            "origin": ("origin", "https://other.synthetic.invalid"),
            "host": ("host_id", "lab-mac"),
            "verifier": ("verifier", "w" * 43),
        }[attack]
        response = client.post(
            f"/v1/pairing/{row['request_id']}/redeem", json=proof(**{key: value})
        )
    elif attack == "claims":
        response = client.post(
            "/v1/pairing/requests",
            json={
                **body,
                "account": "admin",
                "capabilities": ["hosts.enroll"],
                "device_id": "chosen",
            },
        )
    elif attack == "header":
        response = client.post("/v1/pairing/requests", json=body, headers={"Origin": "null"})
    elif attack == "duplicate-origin":
        response = client.post(
            "/v1/pairing/requests", json=body, headers=[("Origin", ORIGIN), ("Origin", ORIGIN)]
        )
    elif attack == "duplicate":
        response = client.post(
            "/v1/pairing/requests", content=json.dumps(body)[:-1] + ', "host_id": "home-mac"}'
        )
    elif attack == "label":
        response = client.post(
            "/v1/pairing/requests", json={**body, "device_label": "unsafe\x1b[31m"}
        )
    else:
        response = client.post("/v1/pairing/requests", content=b"x" * 2049)
    assert response.status_code in (400, 403, 404, 413), response.text
    assert not state.sessions._by_access
    assert client.post(f"/v1/pairing/{row['request_id']}/approve", json={}).status_code == 404


def test_wrong_verifier_burns_across_readers_and_closed_window_discards_approval(
    tmp_path: Path,
) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    store.open_window(host, ORIGIN, now[0], 120)
    client = TestClient(create_app(state))
    row = client.post("/v1/pairing/requests", json=body).json()
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    for _ in range(5):
        assert (
            client.post(
                f"/v1/pairing/{row['request_id']}/status", json=proof(verifier="w" * 43)
            ).status_code
            == 404
        )
    assert client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof()).status_code == 404
    row = client.post("/v1/pairing/requests", json=body).json()
    store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    store.close_window()
    assert client.post(f"/v1/pairing/{row['request_id']}/redeem", json=proof()).status_code == 404


def test_enrollment_rotation_does_not_restore_a_paired_grant(tmp_path: Path) -> None:
    state, now, body = foundation(tmp_path / "state")
    store = state.pairing
    assert store is not None
    host = state.hosts.record_for_host("home-mac")
    assert host is not None
    store.open_window(host, ORIGIN, now[0], 120)
    client = TestClient(create_app(state))
    rows = [client.post("/v1/pairing/requests", json=body).json() for _ in range(2)]
    for row in rows:
        store.decide(row["request_id"], row["matching_code"], host, ORIGIN, now[0], approve=True)
    tokens = client.post(f"/v1/pairing/{rows[0]['request_id']}/redeem", json=proof()).json()
    state.hosts.revoke("home-mac")
    state.hosts.enroll(host.host_id, host.label, host.account, host.pubkey, host.fingerprint)
    assert client.get("/v1/me", headers=auth(tokens["access_token"])).status_code == 401
    assert (
        client.post(
            "/v1/token", json={"grant": "refresh_token", "refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(f"/v1/pairing/{rows[1]['request_id']}/redeem", json=proof()).status_code == 404
    )
