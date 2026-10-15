"""Explicit HTTP recovery validates live authority, not the cookie alone."""

from __future__ import annotations

import httpx
import pytest

from relaydeck.client.serve import _Handshake, build_web_app
from relaydeck.gateway.app import create_app
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.host_harness import run


@pytest.mark.parametrize("identity", ["valid", "revoked", "device-mismatch", "anonymous"])
def test_explicit_recovery_checks_issued_principal_and_preserves_foreign_session(
    identity: str,
) -> None:
    async def scenario() -> None:
        state = _gateway_state()
        app, store = build_web_app(
            gateway_state=state,
            gateway_app=create_app(state),
            flet_main=lambda p: None,
            public_origin="http://127.0.0.1:8125",
            cookie_secret=b"s" * 32,
        )
        access, refresh = state.sessions.create(
            "synthetic", {"sessions.read"}, {"home-mac"}, state.now_ms(), device_id="issued-device"
        )
        foreign, _ = state.sessions.create(
            "synthetic", {"sessions.read"}, {"lab-mac"}, state.now_ms(), device_id="foreign-device"
        )
        sid = store.create_session(
            "synthetic",
            "wrong" if identity == "device-mismatch" else "issued-device",
            access,
            refresh,
            store.now_ms() + 900_000,
        )
        store.bind_flet(sid, "stale-page", _Handshake("", "synthetic", ""))
        if identity == "revoked":
            state.sessions.revoke_session(access)
        headers = {"Origin": store.public_origin}
        if identity != "anonymous":
            headers["Cookie"] = "rd_session=" + store.sign_sid(sid)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=store.public_origin
        ) as http:
            document = await http.get("/v1/auth/session/recovery")
            assert document.status_code == 200 and "recovery.js" in document.text
            assert document.headers["cache-control"] == "no-store"
            denied = await http.post(
                "/v1/auth/session/recovery",
                headers={**headers, "Origin": "https://foreign.example"},
            )
            assert denied.status_code == 403 and store.identity(sid) is not None
            response = await http.post("/v1/auth/session/recovery", headers=headers)
        assert response.status_code == (204 if identity == "valid" else 401)
        assert state.sessions.lookup(foreign, state.now_ms()) is not None
        if identity == "valid":
            assert store.owner_of("stale-page") == sid
            assert "set-cookie" not in response.headers
            assert state.sessions.lookup(access, state.now_ms()) is not None
        elif identity != "anonymous":
            assert store.owner_of("stale-page") is None
            assert store.identity(sid) is None
            assert "Max-Age=0" in response.headers["set-cookie"]
        else:
            assert store.identity(sid) is not None

    run(scenario())
