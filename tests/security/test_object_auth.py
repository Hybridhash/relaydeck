"""Object authorization on every mutating route + WS paths.

Anchored on the 014/015/021 suites (attachment/lease/command guards);
this module extends coverage to the routes they miss: push devices,
deep-link resolution, journal status reads, ticket minting, and
enrollments, plus anonymous access on every mutating route and
cross-lease/cross-device WS confinement.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.cursors import encode_cursor
from tests.security.helpers import (
    ALL_CAPS,
    NOW,
    SECRET,
    attach,
    auth,
    command_body,
    lease,
    make_client,
    mint_token,
    ticket,
)

MUTATING_ROUTES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("POST", "/v1/logout", {}),
    ("POST", "/v1/devices", {"device_id": "x", "token": "y", "platform": "android"}),
    ("POST", "/v1/devices/x/rotate", {"token": "y"}),
    ("DELETE", "/v1/devices/x", None),
    ("POST", "/v1/deep-links/resolve", {"token": "rdl_nope"}),
    ("POST", "/v1/enrollments", {"host_label": "h", "pubkey": "ssh-ed25519 AAAA"}),
    ("POST", "/v1/hosts/home-mac/commands", command_body()),
    ("POST", "/v1/hosts/home-mac/terminals/t/attachments", {"kind": "shell"}),
    ("POST", "/v1/terminal-attachments/att-1/leases", {"mode": "view"}),
    ("DELETE", "/v1/terminal-attachments/att-1", None),
    ("DELETE", "/v1/terminal-leases/lease-1", None),
    ("POST", "/v1/terminal-leases/lease-1/renew", {}),
    ("POST", "/v1/terminal-tickets", {"lease_id": "lease-1"}),
)


@pytest.mark.parametrize(("method", "path", "body"), MUTATING_ROUTES)
def test_anonymous_mutation_fails(method: str, path: str, body: dict[str, Any] | None) -> None:
    """No Bearer token (or a garbage one) on any mutating route fails."""
    client, _, _ = make_client()
    call = {"POST": client.post, "DELETE": client.delete}[method]
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    assert call(path, **kwargs).status_code == 401
    assert call(path, headers=auth("bogus-token"), **kwargs).status_code == 401


def test_anonymous_reads_fail() -> None:
    client, _, _ = make_client()
    assert client.get("/v1/me").status_code == 401
    assert client.get("/v1/hosts").status_code == 401
    assert client.get("/v1/hosts/home-mac/snapshot").status_code == 401
    assert client.get("/v1/hosts/home-mac/agents").status_code == 401
    assert client.get("/v1/hosts/home-mac/commands/cmd-1").status_code == 401


def test_cross_account_host_paths_fail() -> None:
    """Account B (own host grant) cannot touch account A's host objects."""
    client, state, _ = make_client(hosts=("home-mac", "other-mac"))
    token_a = mint_token(state, "acct-a", hosts=frozenset({"home-mac"}))
    token_b = mint_token(state, "acct-b", hosts=frozenset({"other-mac"}))
    att = attach(client, token_a)
    ls = lease(client, token_a, att["attachment_id"])

    # Snapshot / agents / command submit / journal status on A's host.
    assert client.get("/v1/hosts/home-mac/snapshot", headers=auth(token_b)).status_code == 404
    assert client.get("/v1/hosts/home-mac/agents", headers=auth(token_b)).status_code == 404
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(token_b), json=command_body("cmd-sec-x1")
    )
    assert r.json()["code"] == "not_found"
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(token_a), json=command_body("cmd-sec-a1")
    )
    assert r.status_code == 202
    assert (
        client.get("/v1/hosts/home-mac/commands/cmd-sec-a1", headers=auth(token_b)).status_code
        == 404
    )

    # Attachments / leases / tickets / renew / release / close.
    r = client.post(
        "/v1/hosts/home-mac/terminals/term-1/attachments",
        headers=auth(token_b),
        json={"kind": "shell"},
    )
    assert r.status_code == 404
    r = client.post(
        f"/v1/terminal-attachments/{att['attachment_id']}/leases",
        headers=auth(token_b),
        json={"mode": "view"},
    )
    assert r.status_code == 404
    r = client.post(
        "/v1/terminal-tickets", headers=auth(token_b), json={"lease_id": ls["lease_id"]}
    )
    # Host-grant check (app.py:1124) precedes the owner check: B holds no
    # grant for home-mac, so this is lease_expired, not not_found. The
    # owner branch is pinned by test_ticket_mint_is_lease_owner_scoped.
    assert r.json()["code"] == "lease_expired"
    r = client.post(f"/v1/terminal-leases/{ls['lease_id']}/renew", headers=auth(token_b), json={})
    assert r.status_code == 404
    r = client.delete(f"/v1/terminal-leases/{ls['lease_id']}", headers=auth(token_b))
    assert r.status_code == 404
    r = client.delete(f"/v1/terminal-attachments/{att['attachment_id']}", headers=auth(token_b))
    assert r.status_code == 404


def test_cross_host_command_submit_fails() -> None:
    """Path host and body host must agree, and the session must hold the host."""
    client, state, _ = make_client(hosts=("home-mac", "other-mac"))
    token = mint_token(state, hosts=frozenset({"home-mac"}))
    body = command_body("cmd-sec-x2", host_id="home-mac")
    r = client.post("/v1/hosts/other-mac/commands", headers=auth(token), json=body)
    assert r.json()["code"] == "forbidden"  # path/body host mismatch
    body2 = command_body("cmd-sec-x3", host_id="other-mac")
    r = client.post("/v1/hosts/other-mac/commands", headers=auth(token), json=body2)
    assert r.json()["code"] == "not_found"  # session lacks the host


def test_cross_device_lease_release_and_renew_fail() -> None:
    """A second device of the same account cannot release/renew the lease."""
    client, state, backend = make_client()
    access_a, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    access_b, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    assert state.sessions.lookup(access_a, NOW) is not None
    assert state.sessions.lookup(access_b, NOW) is not None
    att = attach(client, access_a)
    ls = lease(client, access_a, att["attachment_id"])
    assert (
        client.post(
            f"/v1/terminal-leases/{ls['lease_id']}/renew", headers=auth(access_b), json={}
        ).json()["code"]
        == "not_found"
    )
    assert (
        client.delete(f"/v1/terminal-leases/{ls['lease_id']}", headers=auth(access_b)).json()[
            "code"
        ]
        == "not_found"
    )
    # The holder's lease survives the foreign attempts.
    assert backend.open_lease(ls["lease_id"]) is not None


def test_push_devices_are_account_scoped() -> None:
    """Device rotate/delete of another account's install fails closed."""
    client, state, _ = make_client()
    token_a = mint_token(state, "acct-a")
    token_b = mint_token(state, "acct-b")
    r = client.post(
        "/v1/devices",
        headers=auth(token_a),
        json={"device_id": "install-a", "token": "tok-a", "platform": "android"},
    )
    assert r.status_code == 201
    assert (
        client.post(
            "/v1/devices/install-a/rotate",
            headers=auth(token_b),
            json={"token": "tok-evil"},
        ).status_code
        == 404
    )
    assert client.delete("/v1/devices/install-a", headers=auth(token_b)).status_code == 404
    # Unknown ids are indistinguishable from foreign ones.
    assert client.delete("/v1/devices/nope", headers=auth(token_a)).status_code == 404


def test_deep_link_resolve_is_account_scoped() -> None:
    """A link minted for A resolves only for A; forged links fail closed."""
    client, state, _ = make_client()
    token_a = mint_token(state, "acct-a")
    token_b = mint_token(state, "acct-b")
    now = state.now_ms()
    state.push.register_device("acct-a", "install-a", "tok-a", "android", now)
    minted = state.push.notify(
        account="acct-a",
        account_hosts=frozenset({"home-mac"}),
        host_id="home-mac",
        session_id="sess-1",
        event_id="evt-1",
        category="attention",
        now=now,
    )
    assert minted, "notify should mint for an active device"
    link = str(minted[0]["link_token"])
    note_id = str(minted[0]["notification_id"])
    good = client.post("/v1/deep-links/resolve", headers=auth(token_a), json={"token": link})
    assert good.status_code == 200, good.text
    assert (
        client.post(
            "/v1/deep-links/resolve", headers=auth(token_b), json={"token": link}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/deep-links/resolve",
            headers=auth(token_a),
            json={"token": "rdl_forged0000000000000000000001"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/deep-links/resolve",
            headers=auth(token_a),
            json={"notification_id": note_id},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/deep-links/resolve",
            headers=auth(token_b),
            json={"notification_id": note_id},
        ).status_code
        == 404
    )


def test_ticket_mint_is_lease_owner_scoped() -> None:
    """Only the lease-owning account mints a socket ticket for it."""
    client, state, _ = make_client()
    token_a = mint_token(state, "acct-a")
    token_b = mint_token(state, "acct-b")
    att = attach(client, token_a)
    ls = lease(client, token_a, att["attachment_id"])
    assert ticket(client, token_a, ls["lease_id"]).startswith("rdt_")
    r = client.post(
        "/v1/terminal-tickets", headers=auth(token_b), json={"lease_id": ls["lease_id"]}
    )
    assert r.json()["code"] == "not_found"
    # A viewer (no terminal.view) cannot mint either.
    weak = mint_token(state, "acct-a", caps=frozenset({"sessions.read"}))
    r = client.post("/v1/terminal-tickets", headers=auth(weak), json={"lease_id": ls["lease_id"]})
    assert r.json()["code"] == "unsupported_capability"


def test_ws_ticket_bound_to_lease() -> None:
    """A ticket minted for lease X opens no other lease's socket."""
    client, state, _ = make_client()
    token = mint_token(state)
    att = attach(client, token)
    first = lease(client, token, att["attachment_id"])
    # Second attachment/lease on another terminal for the cross target.
    att2 = attach(client, token, terminal_id="term-2")
    second = lease(client, token, att2["attachment_id"])
    good = ticket(client, token, first["lease_id"])
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/terminal/{second['lease_id']}?ticket={good}"):
            pass
    # The ticket is single-use: even the right lease now fails.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/terminal/{first['lease_id']}?ticket={good}"):
            pass


def test_ws_input_frames_bound_to_lease_and_generation() -> None:
    """Input frames naming another lease, or a future generation, kill the socket."""
    client, state, backend = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    generation = int(ls["generation"])
    tick = ticket(client, token, ls["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick}") as ws:
        ws.send_bytes(terminal_wire.encode_text("lease-other", generation, 1, b"x"))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert backend.input_logs.get(ls["lease_id"], []) == []

    # Wrong-lease input closes the control socket and releases that lease.
    # The wrong-generation case gets a separate, live authorization boundary.
    ls = lease(client, token, att["attachment_id"])
    generation = int(ls["generation"])
    tick2 = ticket(client, token, ls["lease_id"])
    with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={tick2}") as ws:
        ws.send_bytes(terminal_wire.encode_text(ls["lease_id"], generation + 9, 1, b"x"))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert backend.input_logs.get(ls["lease_id"], []) == []


def test_enrollment_create_needs_capability() -> None:
    client, state, _ = make_client()
    weak = mint_token(state, caps=frozenset({"sessions.read"}))
    r = client.post(
        "/v1/enrollments",
        headers=auth(weak),
        json={"host_label": "h", "pubkey": "ssh-ed25519 AAAA"},
    )
    assert r.json()["code"] == "unsupported_capability"


def test_agents_page_token_bound_to_host() -> None:
    """A page cursor minted for host A is refused on host B."""
    client, state, _ = make_client(hosts=("home-mac", "other-mac"))
    token = mint_token(state, hosts=frozenset({"home-mac", "other-mac"}))
    foreign = encode_cursor(SECRET, {"host_id": "other-mac", "offset": 0})
    r = client.get("/v1/hosts/home-mac/agents", headers=auth(token), params={"page_token": foreign})
    assert r.json()["code"] == "stale_epoch"
    forged = encode_cursor(b"wrong-secret-32bytes-padding!!", {"host_id": "home-mac"})
    r = client.get("/v1/hosts/home-mac/agents", headers=auth(token), params={"page_token": forged})
    assert r.json()["code"] == "stale_epoch"


def test_cross_account_error_reveals_nothing() -> None:
    """Foreign-object failures are bare not_found: no holder, owner, or digest."""
    client, state, _ = make_client()
    token_a = mint_token(state, "acct-a")
    token_b = mint_token(state, "acct-b")
    att = attach(client, token_a)
    ls = lease(client, token_a, att["attachment_id"])
    bodies = [
        client.post(
            f"/v1/terminal-attachments/{att['attachment_id']}/leases",
            headers=auth(token_b),
            json={"mode": "view"},
        ).json(),
        client.post(
            "/v1/terminal-tickets", headers=auth(token_b), json={"lease_id": ls["lease_id"]}
        ).json(),
        client.get("/v1/hosts/home-mac/commands/cmd-sec-a1", headers=auth(token_b)).json(),
    ]
    for body in bodies:
        assert body["code"] == "not_found"
        # details, when present, is an empty scrubbed object: no holder,
        # owner, or digest may ride a foreign-object failure.
        assert body.get("details", {}) == {}
        assert "acct-a" not in json.dumps(body)
