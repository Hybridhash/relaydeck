"""tunnel metadata-sync attack surface (snapshot/event push).

Forged host fields, schema-invalid snapshots, snapshots bound to
another host, invalid events, and replayed event ids: every forgery
fails the tunnel closed (1008) and stores nothing; idempotent re-push
stores once. Localhost only, synthetic fixtures.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.connector.fixture import fixture_event, fixture_snapshot
from tests.security.helpers import attach, auth, lease, make_client, mint_token


def _tunnel_token(state: Any, host_id: str = "home-mac") -> str:
    return str(state.hosts.enroll(host_id, host_id, "acct-1"))


def test_snapshot_forged_outer_host_closes_and_stores_nothing() -> None:
    client, state, backend = make_client()
    before = dict(backend.snapshots["home-mac"])
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        ws.send_text(
            json.dumps(
                {
                    "kind": "snapshot",
                    "host_id": "other-mac",  # forged: tunnel is home-mac
                    "snapshot": fixture_snapshot("other-mac"),
                }
            )
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
    assert backend.snapshots["home-mac"] == before
    assert "other-mac" not in backend.snapshots


def test_snapshot_bound_to_other_host_closes() -> None:
    client, state, backend = make_client()
    before = dict(backend.snapshots["home-mac"])
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        # Outer binding is honest but the document names another host.
        ws.send_text(
            json.dumps(
                {
                    "kind": "snapshot",
                    "host_id": "home-mac",
                    "snapshot": fixture_snapshot("other-mac"),
                }
            )
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
    assert backend.snapshots["home-mac"] == before


def test_snapshot_schema_invalid_closes_and_stores_nothing() -> None:
    client, state, backend = make_client()
    before = dict(backend.snapshots["home-mac"])
    host_token = _tunnel_token(state)
    bad = fixture_snapshot("home-mac")
    del bad["agents"]  # required key: schema-invalid
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        ws.send_text(json.dumps({"kind": "snapshot", "host_id": "home-mac", "snapshot": bad}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
    assert backend.snapshots["home-mac"] == before


def test_event_forged_host_closes_and_logs_nothing() -> None:
    client, state, backend = make_client()
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        ws.send_text(
            json.dumps(
                {
                    "kind": "event",
                    "host_id": "other-mac",
                    "event": fixture_event("host.status", "ev-x-1"),
                }
            )
        )
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
    assert backend.event_logs.get("home-mac", []) == []
    assert "other-mac" not in backend.event_logs


def test_event_invalid_type_and_missing_resource_close() -> None:
    for partial in (
        fixture_event("no.such.type", "ev-x-2"),
        fixture_event("agent.upserted", "ev-x-3"),  # resource_id required
        {"type": "host.status"},  # host_epoch required
    ):
        client, state, backend = make_client()
        host_token = _tunnel_token(state)
        with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
            ws.receive_text()  # hello
            ws.send_text(json.dumps({"kind": "event", "host_id": "home-mac", "event": partial}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1008
        assert backend.event_logs.get("home-mac", []) == []


def test_event_replay_dedupes_by_event_id() -> None:
    client, state, backend = make_client()
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        for _ in range(3):
            ws.send_text(
                json.dumps(
                    {
                        "kind": "event",
                        "host_id": "home-mac",
                        "event": dict(
                            fixture_event("tab.upserted", "ev-dedupe-1", resource_id="w1:t1"),
                            host_epoch=backend.snapshot("home-mac")["host"]["epoch"],
                        ),
                    }
                )
            )
        ws.send_text(json.dumps({"kind": "ping"}))
        assert json.loads(ws.receive_text()) == {"kind": "pong"}
        ws.send_text(json.dumps({"kind": "bye"}))
    log = backend.event_logs["home-mac"]
    assert [e["event_id"] for e in log] == ["ev-dedupe-1"]
    assert log[0]["sequence"] == 1


def test_server_owned_event_fields_cannot_be_forged() -> None:
    client, state, backend = make_client()
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        ws.send_text(
            json.dumps(
                {
                    "kind": "event",
                    "host_id": "home-mac",
                    "event": {
                        "event_id": "ev-forge-1",
                        "host_epoch": "epoch-7",
                        "type": "host.status",
                        "observed_at": "2026-10-04T00:00:00Z",
                        # Connector-minted sequence/cursor/protocol/host
                        # are ignored: the server stamps its own.
                        "sequence": 999,
                        "cursor": "forged",
                        "protocol": "evil.proto",
                        "host_id": "other-mac",
                    },
                }
            )
        )
        ws.send_text(json.dumps({"kind": "ping"}))
        assert json.loads(ws.receive_text()) == {"kind": "pong"}
        ws.send_text(json.dumps({"kind": "bye"}))
    (stored,) = backend.event_logs["home-mac"]
    assert stored["sequence"] == 1
    assert stored["cursor"] != "forged"
    assert stored["protocol"] == "relaydeck.gateway.v1"
    assert stored["host_id"] == "home-mac"


def test_snapshot_push_does_not_disturb_live_leases() -> None:
    client, state, backend = make_client()
    token = mint_token(state)
    attachment = attach(client, token)
    held = lease(client, token, attachment["attachment_id"])
    incoming = fixture_snapshot("home-mac")
    incoming["host"]["epoch"] = backend.snapshot("home-mac")["host"]["epoch"]
    incoming["workspaces"][0]["label"] = "same-epoch metadata update"
    host_token = _tunnel_token(state)
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        ws.receive_text()  # hello
        ws.send_text(
            json.dumps(
                {
                    "kind": "snapshot",
                    "host_id": "home-mac",
                    "snapshot": incoming,
                }
            )
        )
        ws.send_text(json.dumps({"kind": "bye"}))
    # Metadata replaced, but the ownership plane is untouched: the held
    # lease still renews and still mints tickets.
    renew = client.post(
        f"/v1/terminal-leases/{held['lease_id']}/renew", headers=auth(token), json={}
    )
    assert renew.status_code == 200, renew.text
    assert backend.snapshots["home-mac"]["host"]["epoch"] == incoming["host"]["epoch"]
    assert backend.snapshots["home-mac"]["workspaces"][0]["label"] == "same-epoch metadata update"
    assert state.backend.open_lease(held["lease_id"]) is not None
