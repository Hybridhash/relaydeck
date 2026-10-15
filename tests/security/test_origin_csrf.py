"""Origin/CSRF on gateway WS + write routes.

`test_flet_auth_boundary` proves the Flet /ws handshake and the
cookie-logout CSRF gate live; this module extends to the gateway
surface without duplicating those cases:

* gateway terminal + connector sockets: anonymous handshakes fail for
  every Origin variant (missing, null, spoofed, exact);
* gateway write routes: no-Bearer writes fail for every Origin
  variant; a valid web cookie is NOT accepted as gateway auth;
* gateway issues no cookies and honors only the Authorization header,
  which browsers never send ambiently -- CSRF is structurally
  impossible on these paths (documented, then asserted);
* Flet logout gate extension: a missing Origin is refused like a
  spoofed one (that test covers spoofed live; the same middleware line is
  driven directly here).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from relaydeck.client.serve import COOKIE_NAME, FletGuardMiddleware, WebSessionStore
from tests.security.helpers import attach, auth, command_body, lease, make_client, mint_token

ORIGINS: tuple[str | None, ...] = (None, "", "null", "https://evil.example")


def _origin_headers(origin: str | None) -> dict[str, str]:
    return {} if origin is None else {"Origin": origin}


@pytest.mark.parametrize("origin", ORIGINS)
def test_terminal_ws_anonymous_fails_every_origin(origin: str | None) -> None:
    client, state, _ = make_client()
    token = mint_token(state)
    att = attach(client, token)
    ls = lease(client, token, att["attachment_id"])
    headers = _origin_headers(origin)
    for target in (
        f"/v1/terminal/{ls['lease_id']}",
        f"/v1/terminal/{ls['lease_id']}?ticket=rdt_bogus",
    ):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(target, headers=headers):
                pass


@pytest.mark.parametrize("origin", ORIGINS)
def test_connector_ws_anonymous_fails_every_origin(origin: str | None) -> None:
    client, state, _ = make_client()
    state.hosts.enroll("home-mac", "Home", "acct-1")
    headers = _origin_headers(origin)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/connectors", headers=headers):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/v1/connectors?token=bogus", headers=headers):
            pass


@pytest.mark.parametrize("origin", ORIGINS)
def test_write_routes_reject_anonymous_every_origin(origin: str | None) -> None:
    client, _, _ = make_client()
    headers = _origin_headers(origin)
    assert client.post("/v1/logout", headers=headers, json={}).status_code == 401
    r = client.post("/v1/hosts/home-mac/commands", headers=headers, json=command_body("cmd-sec-o1"))
    assert r.status_code == 401
    r = client.post(
        "/v1/hosts/home-mac/terminals/t/attachments",
        headers=headers,
        json={"kind": "shell"},
    )
    assert r.status_code == 401
    r = client.post("/v1/terminal-tickets", headers=headers, json={"lease_id": "lease-1"})
    assert r.status_code == 401


@pytest.mark.parametrize("origin", ORIGINS)
def test_valid_bearer_works_regardless_of_origin(origin: str | None) -> None:
    """Bearer tokens are not ambient browser credentials, so gateway
    routes intentionally ignore Origin: auth strength is identical for
    every variant (valid token works, invalid fails)."""
    client, state, _ = make_client()
    token = mint_token(state)
    headers = {**auth(token), **_origin_headers(origin)}
    assert client.get("/v1/me", headers=headers).status_code == 200
    bad = {**auth("bogus"), **_origin_headers(origin)}
    assert client.get("/v1/me", headers=bad).status_code == 401


def test_web_cookie_is_not_gateway_auth() -> None:
    """A signed Flet-session cookie presented to gateway routes (with or
    without an exact Origin) authenticates nothing: the gateway honors
    only the Authorization header."""
    client, _, _ = make_client()
    store = WebSessionStore(public_origin="https://gw.example", cookie_secret=b"x" * 32)
    sid = store.create_session("acct-1", "dev-1", "acc", "ref", store.now_ms() + 900_000)
    cookie = f"{COOKIE_NAME}={store.sign_sid(sid)}"
    for headers in ({"Cookie": cookie}, {"Cookie": cookie, "Origin": "https://gw.example"}):
        assert client.get("/v1/me", headers=headers).status_code == 401
        r = client.post(
            "/v1/hosts/home-mac/commands", headers=headers, json=command_body("cmd-sec-o2")
        )
        assert r.status_code == 401


def test_gateway_sets_no_cookies() -> None:
    client, state, _ = make_client()
    token = mint_token(state)
    assert "set-cookie" not in client.get("/v1/me", headers=auth(token)).headers
    r = client.post("/v1/logout", headers=auth(token), json={})
    assert r.status_code == 200
    assert "set-cookie" not in r.headers


def test_flet_logout_refuses_missing_and_spoofed_origin() -> None:
    """Direct drive of the Flet middleware logout gate: only the exact
    public origin passes; missing, null, and spoofed origins get 403."""

    async def attempt(origin: str | None) -> int:
        store = WebSessionStore(public_origin="https://gw.example", cookie_secret=b"x" * 32)
        seen: list[str] = []

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            seen.append("passed")
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = FletGuardMiddleware(downstream, store)
        headers = [] if origin is None else [(b"origin", origin.encode())]
        scope: dict[str, Any] = {
            "type": "http",
            "path": "/v1/auth/logout",
            "method": "POST",
            "headers": headers,
        }
        statuses: list[int] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                statuses.append(int(message["status"]))

        await middleware(scope, receive, send)
        assert statuses, "middleware sent no response"
        return statuses[0]

    assert asyncio.run(attempt(None)) == 403
    assert asyncio.run(attempt("null")) == 403
    assert asyncio.run(attempt("https://evil.example")) == 403
    assert asyncio.run(attempt("https://gw.example")) == 204
