"""Revocation cuts UI + direct API on every path.

Session logout, device push-revoke, account revoke, and host revoke
are each driven to completion; afterwards the HTTP, WebSocket, and
ticket paths for the revoked principal all fail. Refresh-token reuse
(rotated or revoked) fails as well.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.gateway.app import revoke_account_and_close, revoke_host_and_close
from tests.security.helpers import (
    ALL_CAPS,
    NOW,
    attach,
    auth,
    command_body,
    lease,
    make_client,
    mint_token,
    ticket,
)


def test_logout_cuts_http_ws_ticket_and_lease() -> None:
    client, state, backend = make_client()
    access, refresh = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    att = attach(client, access)
    ls = lease(client, access, att["attachment_id"])
    assert client.post("/v1/logout", headers=auth(access), json={}).status_code == 200

    # HTTP: the bearer is dead.
    assert client.get("/v1/me", headers=auth(access)).status_code == 401
    r = client.post(
        "/v1/hosts/home-mac/commands", headers=auth(access), json=command_body("cmd-sec-r1")
    )
    assert r.status_code == 401
    # Refresh rotation of the revoked session fails.
    r = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert r.status_code == 401
    # The device lease died with the session: no ticket, no socket.
    assert backend.open_lease(ls["lease_id"]) is None
    r = client.post("/v1/terminal-tickets", headers=auth(access), json={"lease_id": ls["lease_id"]})
    assert r.status_code == 401


def test_logout_severs_live_terminal_socket() -> None:
    from relaydeck.gateway.app import create_app
    from tests.support.asgi_socket import AsgiSocket
    from tests.support.sync.transport_fixtures import _wait

    client, state, _ = make_client()
    access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    att = attach(client, access)
    ls = lease(client, access, att["attachment_id"])
    tick = ticket(client, access, ls["lease_id"])

    async def scenario() -> None:
        socket = await AsgiSocket(
            create_app(state), f"/v1/terminal/{ls['lease_id']}?ticket={tick}"
        ).open()
        try:
            await _wait(lambda: ls["lease_id"] in state.sockets.leases())
            assert client.post("/v1/logout", headers=auth(access), json={}).status_code == 200
            message = await socket.receive()
            assert message["type"] == "websocket.close" and message["code"] == 1008
            assert message["reason"] == "unauthenticated"
            assert ls["lease_id"] not in state.sockets.leases()
            assert state.sessions.lookup(access, NOW) is None
        finally:
            await socket.close()

    asyncio.run(scenario())


def test_refresh_reuse_fails() -> None:
    """A rotated refresh token is single-use: replaying it fails."""
    client, state, _ = make_client()
    _, refresh = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    first = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert first.status_code == 200
    replay = client.post("/v1/token", json={"grant": "refresh_token", "refresh_token": refresh})
    assert replay.status_code == 401


def test_device_delete_cuts_push_paths() -> None:
    """DELETE /v1/devices revokes the install subscription: hints and
    links minted for it stop resolving and the device must re-register."""
    client, state, _ = make_client()
    token = mint_token(state, "acct-1")
    now = state.now_ms()
    assert (
        client.post(
            "/v1/devices",
            headers=auth(token),
            json={"device_id": "install-9", "token": "tok-9", "platform": "android"},
        ).status_code
        == 201
    )
    minted = state.push.notify(
        account="acct-1",
        account_hosts=frozenset({"home-mac"}),
        host_id="home-mac",
        session_id="sess-1",
        event_id="evt-9",
        category="attention",
        now=now,
    )
    assert minted
    link = str(minted[0]["link_token"])
    note_id = str(minted[0]["notification_id"])
    assert client.delete("/v1/devices/install-9", headers=auth(token)).status_code == 200
    assert (
        client.post("/v1/deep-links/resolve", headers=auth(token), json={"token": link}).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/deep-links/resolve", headers=auth(token), json={"notification_id": note_id}
        ).status_code
        == 404
    )
    # Eligibility is gone too: no new notifications mint for a dead install.
    assert (
        state.push.notify(
            account="acct-1",
            account_hosts=frozenset({"home-mac"}),
            host_id="home-mac",
            session_id="sess-1",
            event_id="evt-10",
            category="attention",
            now=now,
        )
        == []
    )


def test_account_revoke_cuts_everything() -> None:
    client, state, backend = make_client()
    access, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), NOW)
    other, _ = state.sessions.create("acct-2", ALL_CAPS, frozenset({"home-mac"}), NOW)
    att = attach(client, access)
    ls = lease(client, access, att["attachment_id"])
    pre_ticket = ticket(client, access, ls["lease_id"])
    closed = asyncio.run(revoke_account_and_close(state, "acct-1"))
    assert closed == 0  # HTTP-only fixtures: no live sockets to sever

    assert client.get("/v1/me", headers=auth(access)).status_code == 401
    assert client.get("/v1/me", headers=auth(other)).status_code == 200  # scoped
    assert backend.open_lease(ls["lease_id"]) is None
    # A ticket minted before revocation opens nothing: the lease is dead.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/terminal/{ls['lease_id']}?ticket={pre_ticket}"):
            pass


def test_host_revoke_cuts_tunnel_and_leases() -> None:
    client, state, backend = make_client()
    token = mint_token(state)
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    revoked, _tunnels, _leases = asyncio.run(revoke_host_and_close(state, "home-mac"))
    assert revoked is True
    # The host token is dead: no new tunnel.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/connectors?token={host_token}"):
            pass
    # Host leases died: ticket minting and sockets fail.
    assert backend.open_lease(ls["lease_id"]) is None
    r = client.post("/v1/terminal-tickets", headers=auth(token), json={"lease_id": ls["lease_id"]})
    assert r.json()["code"] == "lease_expired"


def test_host_revoke_severs_live_tunnel() -> None:
    client, state, _ = make_client()
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
    with client.websocket_connect(f"/v1/connectors?token={host_token}") as ws:
        assert json.loads(ws.receive_text())["kind"] == "hello"
        asyncio.run(revoke_host_and_close(state, "home-mac"))
        try:
            ws.send_text(json.dumps({"kind": "ping"}))
        except RuntimeError:
            return  # server close already landed: the tunnel is severed
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
