"""Actual HTTP cookie -> pinned Flet REGISTER transition, entirely in-process."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import msgpack  # type: ignore[import-untyped]
import pytest
from flet_web.fastapi.flet_app_manager import app_manager

from relaydeck.client.serve import (
    WebSessionStore,
    build_web_app,
    identity_for_page,
    make_relaydeck_main,
    web_pairing_flow,
)
from relaydeck.gateway.app import create_app
from tests.support.asgi_socket import AsgiSocket
from tests.support.companion.pairing_harness import Harness
from tests.support.pairing import ORIGIN


class BrowserSocket(AsgiSocket):
    def __init__(self, app: Any, cookie: str) -> None:
        super().__init__(app, "/ws")
        self.cookie = cookie

    async def open(self) -> BrowserSocket:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "wss",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"origin", ORIGIN.encode()), (b"cookie", self.cookie.encode())],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 443),
            "subprotocols": [],
        }
        await self.inbound.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(scope, self.inbound.get, self.outbound.put))
        assert (await self.receive())["type"] == "websocket.accept"
        return self

    async def register(self, flet_id: str = "") -> str:
        await self.send_bytes(
            b"\x00" + msgpack.packb([1, {"session_id": flet_id, "page_name": "", "page": {}}])
        )
        while True:
            reply = await self.receive()
            if reply["type"] == "websocket.close":
                return ""
            data = reply.get("bytes", b"")
            if data[:1] == b"\x00":
                action, body = msgpack.unpackb(data[1:], raw=False)
                if action == 1:
                    assert not body.get("error"), body
                    return str(body["session_id"])


def cookie(http: httpx.AsyncClient) -> str:
    return "; ".join(f"{key}={value}" for key, value in http.cookies.items())


@pytest.mark.parametrize(
    "finish", ["success", "cancel", "cancel-pending", "expire", "revoke", "restart"]
)
def test_real_browser_cookie_handshake_is_required_and_isolated(
    tmp_path: Path, finish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        gateway = create_app(h.state)
        store = WebSessionStore(ORIGIN, b"s" * 32, now_ms=lambda: h.clock[0])
        import flet as ft

        from relaydeck.client import resources as resources_module
        from tests.support.host_harness import HostApp

        owned: list[Any] = []
        navigation: list[Any] = []
        original_build = resources_module.build_resources

        def build(page: Any, **kwargs: Any) -> Any:
            resource = original_build(page, client=h.http, **kwargs)
            owned.append(resource)
            return resource

        async def launch(self: Any, url: str, **kwargs: Any) -> bool:
            navigation.append((url, kwargs))
            return True  # carrier navigation seam; cookie/auth remains real HTTP

        monkeypatch.setattr(resources_module, "build_resources", build)
        monkeypatch.setattr(ft.UrlLauncher, "launch_url", launch)
        production = make_relaydeck_main(
            store=store,
            base_url=ORIGIN,
            gateway_app=gateway,
            gateway_state=h.state,
            host_id="home-mac",
        )
        pages: asyncio.Queue[Any] = asyncio.Queue()

        async def main(page: Any) -> None:
            production(page)
            await asyncio.sleep(0.02)  # let the real Flet scheduler mount once
            await pages.put((page.session.id, identity_for_page(store, page), page))

        app, _ = build_web_app(
            gateway_state=h.state,
            gateway_app=gateway,
            flet_main=main,
            public_origin=ORIGIN,
            cookie_secret=b"s" * 32,
            store=store,
        )
        browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)
        sockets: list[BrowserSocket] = []
        await app_manager.start()
        try:
            assert (
                await browser.post("/v1/auth/pairing/browser", headers={"Origin": ORIGIN})
            ).status_code == 201
            initial = await BrowserSocket(app, cookie(browser)).open()
            sockets.append(initial)
            fid = await initial.register()
            page_id, identity, page = await asyncio.wait_for(pages.get(), 2)
            assert page_id == fid and identity is None
            assert HostApp(page.session, None).find("connect-begin") is not None
            assert not owned
            await HostApp(page.session, None).settle()  # mount real Flet effects before input
            flow = web_pairing_flow(store, fid, gateway)
            assert flow is not None
            flow.sleep = h.sleep
            if finish == "cancel-pending":

                async def wait_for_owner(interval: float) -> None:
                    await asyncio.Event().wait()

                flow.sleep = wait_for_owner
            await HostApp(page.session, None).set_props_and_fire("connect-begin", {}, "click")
            await HostApp(page.session, None).settle()
            if finish == "cancel-pending":
                assert flow.snapshot.phase == "waiting"
                assert not HostApp(page.session, None).find("connect-cancel").disabled
                await HostApp(page.session, None).set_props_and_fire("connect-cancel", {}, "click")
                assert flow.snapshot.phase == "setup" and not flow.credentials.values
                assert not store._sessions and not store.pairing_ready and not navigation
                return
            assert flow.snapshot.phase == "ready"
            assert navigation == [
                (ORIGIN + "/v1/auth/pairing/finish", {"web_only_window_name": "_self"})
            ]
            assert not flow.credentials.values  # server verifier is removed
            assert (
                identity_for_page(store, SimpleNamespace(session=SimpleNamespace(id=fid))) is None
            )
            assert "rd_session" not in browser.cookies  # anonymous WS did not grant Console
            await initial.close()

            # Another browser cannot consume the ready exchange or attach its Flet page.
            assert (
                await other.post("/v1/auth/pairing/browser", headers={"Origin": ORIGIN})
            ).status_code == 201
            assert (
                await other.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": fid},
                )
            ).status_code == 404
            assert fid in store.pairing_ready
            stolen = await BrowserSocket(app, cookie(other)).open()
            sockets.append(stolen)
            assert await stolen.register(fid) == ""

            if finish == "cancel":
                await flow.cancel()  # cancellation outside a Flet callback stays page-bound
            elif finish == "expire":
                h.clock[0] += 300_001  # held result lasts 5 minutes
            elif finish == "revoke":
                h.state.sessions.revoke_device("owner", store.pairing_ready[fid][2]["device_id"])
            elif finish == "restart":
                # Process restart loses memory-only web attempts/sessions.
                store.pairing_ready.clear()
            response = await browser.post(
                "/v1/auth/pairing/complete",
                headers={"Origin": ORIGIN},
                json={"flet_session_id": fid},
            )
            assert response.status_code == (
                200 if finish == "success" else 403 if finish == "revoke" else 404
            )
            if finish != "success":
                assert "rd_session" not in browser.cookies and not store._sessions
                return
            assert response.json() == {"ok": True}
            assert all(
                flag in response.headers["set-cookie"]
                for flag in ("HttpOnly", "Secure", "SameSite=Lax")
            )
            assert "access_token" not in response.text and "refresh_token" not in response.text
            assert (
                await browser.post(
                    "/v1/auth/pairing/complete",
                    headers={"Origin": ORIGIN},
                    json={"flet_session_id": fid},
                )
            ).status_code == 404
            protected = await BrowserSocket(app, cookie(browser)).open()
            sockets.append(protected)
            new_id = await protected.register()
            page_id, identity, page = await asyncio.wait_for(pages.get(), 2)
            assert page_id == new_id and identity is not None
            assert identity.account == "owner" and identity.device_id.startswith("device_")
            assert len(owned) == 1 and owned[0].props["account"] == "owner"
            assert HostApp(page.session, None).find("destinations") is not None
            sid = store.verify_cookie(browser.cookies["rd_session"])
            assert sid is not None and store.owner_of(new_id) == sid
            assert (
                await browser.post("/v1/auth/pairing/browser", headers={"Origin": ORIGIN})
            ).status_code == 204
        finally:
            for socket in sockets:
                await socket.close()
            await app_manager.shutdown()
            for resource in owned:
                await resource.close()
            await browser.aclose()
            await other.aclose()
            await h.close()

    asyncio.run(scenario())


def test_browser_bootstrap_and_completion_reject_csrf_and_ambiguous_bodies(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        gateway = create_app(h.state)
        app, store = build_web_app(
            gateway_state=h.state,
            gateway_app=gateway,
            flet_main=lambda page: None,
            public_origin=ORIGIN,
            cookie_secret=b"s" * 32,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as browser:
            for headers in (
                {},
                {"Origin": "null"},
                {"Origin": "https://evil.example"},
                [("Origin", ORIGIN), ("Origin", ORIGIN)],
            ):
                for route in ("browser", "complete"):
                    assert (
                        await browser.post("/v1/auth/pairing/" + route, headers=headers, json={})
                    ).status_code == 403
            for body in (
                "{}",
                '{"flet_session_id":null}',
                '{"flet_session_id":"x","extra":true}',
                '{"flet_session_id":"x","flet_session_id":"y"}',
                '"' + "x" * 513 + '"',
            ):
                assert (
                    await browser.post(
                        "/v1/auth/pairing/complete", headers={"Origin": ORIGIN}, content=body
                    )
                ).status_code == 400
            assert not store._sessions
        await h.close()

    asyncio.run(scenario())


def test_web_pairing_only_calls_configured_companion(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state")
        store = WebSessionStore(ORIGIN, b"s" * 32, now_ms=lambda: h.clock[0])
        browser_cookie = store.issue_browser_cookie().split(";", 1)[0].split("=", 1)[1]
        store.browser_pages["page"] = str(store.browser_identity(browser_cookie))
        flow = web_pairing_flow(store, "page", create_app(h.state))
        assert flow is not None
        await flow.begin("https://foreign.synthetic.ts.net", "home-mac", "Synthetic")
        assert flow.snapshot.phase == "failed" and not store.pairing_ready
        assert not h.state.pairing.pending(h.record, ORIGIN, h.clock[0])
        await h.close()

    asyncio.run(scenario())


def test_shipped_finish_and_bootstrap_scripts_use_http_cookie_transition(tmp_path: Path) -> None:
    from relaydeck.client.features.auth.web_recovery import RECOVERY_SCRIPT

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for shipped script behavioral check")

    async def fetch_script() -> str:
        h = Harness(tmp_path / "state")
        app, _ = build_web_app(
            gateway_state=h.state,
            gateway_app=create_app(h.state),
            flet_main=lambda page: None,
            public_origin=ORIGIN,
            cookie_secret=b"s" * 32,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as browser:
            response = await browser.get("/v1/auth/pairing/finish.js")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        await h.close()
        return response.text

    finish = tmp_path / "finish.js"
    finish.write_text(asyncio.run(fetch_script()))
    recovery = tmp_path / "recovery.js"
    recovery.write_text(RECOVERY_SCRIPT)
    harness = Path(__file__).resolve().parents[2] / "ui" / "bootstrap_scripts_browser.cjs"
    result = subprocess.run(
        [node, str(harness), str(finish), str(recovery)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr
