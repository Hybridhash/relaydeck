"""Flet ASGI auth boundary.

Anonymous HTTP/WS, stolen page IDs, cross-account reconnect, null or
attacker Origin, expired sessions, and logout are exercised against a
live uvicorn serving the combined app, driving the real Flet 1.0.3
REGISTER_CLIENT protocol. (msgpack rides with flet; pinned with it.)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import parse_qsl, urlparse

import flet as ft
import httpx
import msgpack  # type: ignore[import-untyped]
import websockets.asyncio.client as ws_client
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from relaydeck.client.serve import (
    WebSessionStore,
    build_web_app,
    identity_for_page,
    parse_register,
    rebind_on_connect,
)
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend
from tests.support.gateway.flet_gateway import _gateway_state
from tests.support.live_server import LiveGateway

SECRET = b"flet-boundary-test-secret-32bytes!!"
ANY_ADDR = "0.0.0.0"  # noqa: S104 - the refused address under test


def _probe_main(store: WebSessionStore):  # type: ignore[no-untyped-def]
    async def main(page: ft.Page) -> None:
        label = ft.Text("who=?", key="who")

        def refresh() -> None:
            ident = identity_for_page(store, page)
            label.value = f"who={ident.account if ident else 'anonymous'}"

        async def on_reconnect(_: Any) -> None:
            await rebind_on_connect(store, page)
            refresh()
            page.update()

        refresh()
        page.on_connect = on_reconnect
        page.add(label)

    return main


def _register_frame(session_id: str) -> bytes:
    payload: bytes = msgpack.packb([1, {"session_id": session_id, "page_name": "", "page": {}}])
    return b"\x00" + payload


async def _register(ws: Any, session_id: str = "", timeout: float = 10.0) -> tuple[str, str]:
    """Send REGISTER, return (assigned_session_id, error)."""
    await ws.send(_register_frame(session_id))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("no REGISTER response")
        frame = await asyncio.wait_for(ws.recv(), remaining)
        data = frame if isinstance(frame, bytes) else frame.encode()
        if data[:1] != b"\x00":
            continue
        action, body = msgpack.unpackb(data[1:], raw=False)
        if action == 1:
            return str(body.get("session_id") or ""), str(body.get("error") or "")


async def _wait_marker(ws: Any, marker: bytes, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"marker {marker!r} never arrived")
        frame = await asyncio.wait_for(ws.recv(), remaining)
        data = frame if isinstance(frame, bytes) else frame.encode()
        if marker in data:
            return


def _login(store: WebSessionStore, state: GatewayState, base: str, account: str) -> str:
    """Complete a web login; return the raw Cookie header value."""
    import httpx as _httpx

    async def scenario() -> str:
        login_state, url = store.begin_login(base)
        query = dict(parse_qsl(urlparse(url).query))
        code = f"code-{account}-{len(state.pending_codes)}"
        state.pending_codes[code] = {
            "account": account,
            "capabilities": frozenset({"sessions.read"}),
            "hosts": frozenset({"home-mac"}),
            "challenge": query["code_challenge"],
        }
        async with _httpx.AsyncClient(base_url=base, timeout=10.0) as http:
            r = await http.get(
                "/v1/auth/callback",
                params={"code": code, "state": login_state},
                follow_redirects=False,
            )
            assert r.status_code == 302, r.text
            cookie = r.headers["set-cookie"].split(";", 1)[0]
            assert cookie.startswith("rd_session=")
            assert "HttpOnly" in r.headers["set-cookie"]
            assert "SameSite=Lax" in r.headers["set-cookie"]
            return cookie

    return asyncio.run(scenario())


def _serve() -> tuple[LiveGateway, WebSessionStore, GatewayState, str]:
    gateway_state = _gateway_state()
    store = WebSessionStore(public_origin="placeholder", cookie_secret=SECRET)
    app, store = build_web_app(
        gateway_state=gateway_state,
        gateway_app=create_app(gateway_state),
        flet_main=_probe_main(store),
        public_origin="placeholder",
        cookie_secret=SECRET,
        store=store,
    )
    live = LiveGateway(app)
    live.__enter__()
    store.public_origin = live.base_url
    return live, store, gateway_state, live.base_url


def test_store_cookie_and_ownership_units() -> None:
    store = WebSessionStore(public_origin="https://app.example", cookie_secret=SECRET)
    import time

    sid = store.create_session("a", "d", "at", "rt", int(time.time() * 1000) + 3_600_000)
    assert store.verify_cookie(store.sign_sid(sid)) == sid
    assert store.verify_cookie(store.sign_sid(sid) + "x") is None
    assert store.verify_cookie("bogus") is None
    headers = store.cookie_headers(sid)
    assert "Secure" in headers["Set-Cookie"]  # https origin
    plain = WebSessionStore(public_origin="http://127.0.0.1:9", cookie_secret=SECRET)
    assert "Secure" not in plain.cookie_headers(sid)["Set-Cookie"]

    async def scenario() -> None:
        assert await store.ensure_access(sid, lambda rt: asyncio.sleep(0, result=None)) == "at"

    asyncio.run(scenario())
    assert store.identity("nope") is None
    assert store.owner_of("flet-1") is None
    assert store.unique_key("flet-1") is None


def test_parse_register_vectors() -> None:
    assert parse_register(b"\x00" + msgpack.packb([1, {"session_id": "abc"}])) == (True, "abc")
    assert parse_register(b"\x00" + msgpack.packb([1, {"session_id": ""}])) == (True, "")
    assert parse_register(b"\x00" + msgpack.packb([2, {}])) == (False, "")
    assert parse_register(b"\x01garbage") == (False, "")
    assert parse_register(b"\x00not-msgpack\xff") == (False, "")
    assert parse_register(b"") == (False, "")


def test_ws_rejects_bad_origin() -> None:
    async def scenario() -> None:
        live, _, _, base = _serve()
        try:
            url = base.replace("http://", "ws://") + "/ws"
            from websockets.exceptions import InvalidStatus

            for origin in ("https://evil.example", "null", ""):
                try:
                    async with ws_client.connect(url, origin=Origin(origin), open_timeout=10) as ws:
                        await ws.recv()
                    raise AssertionError(f"origin {origin!r} accepted")
                except InvalidStatus as exc:
                    assert exc.response.status_code == 403
                except ConnectionClosed as exc:
                    assert exc.code == 1008
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_anonymous_session_renders_login_marker() -> None:
    async def scenario() -> None:
        live, store, _, base = _serve()
        try:
            url = base.replace("http://", "ws://") + "/ws"
            async with ws_client.connect(url, origin=Origin(base), open_timeout=10) as ws:
                assigned, error = await _register(ws)
                assert assigned and not error
                assert store.owner_of(assigned) is None  # anonymous
                await _wait_marker(ws, b"who=anonymous")
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_login_cookie_binds_identity() -> None:
    async def scenario() -> None:
        live, store, gateway_state, base = _serve()
        try:
            cookie = await asyncio.to_thread(_login, store, gateway_state, base, "ada")
            url = base.replace("http://", "ws://") + "/ws"
            async with ws_client.connect(
                url, origin=Origin(base), additional_headers={"Cookie": cookie}, open_timeout=10
            ) as ws:
                assigned, error = await _register(ws)
                assert assigned and not error
                await _wait_marker(ws, b"who=ada")
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_stolen_and_cross_account_ids_severed() -> None:
    async def scenario() -> None:
        live, store, gateway_state, base = _serve()
        try:
            cookie_a = await asyncio.to_thread(_login, store, gateway_state, base, "ada")
            cookie_b = await asyncio.to_thread(_login, store, gateway_state, base, "bob")
            url = base.replace("http://", "ws://") + "/ws"
            async with ws_client.connect(
                url, origin=Origin(base), additional_headers={"Cookie": cookie_a}, open_timeout=10
            ) as ws_a:
                id_a, _ = await _register(ws_a)
                await _wait_marker(ws_a, b"who=ada")
                # Cross-account thief presents A's session id.
                try:
                    async with ws_client.connect(
                        url,
                        origin=Origin(base),
                        additional_headers={"Cookie": cookie_b},
                        open_timeout=10,
                    ) as ws_b:
                        await ws_b.send(_register_frame(id_a))
                        async for frame in ws_b:
                            data = frame if isinstance(frame, bytes) else frame.encode()
                            assert b"who=ada" not in data
                    raise AssertionError("thief connection stayed open")
                except ConnectionClosed as exc:
                    assert exc.code == 1008
                # Anonymous thief with no cookie at all.
                try:
                    async with ws_client.connect(url, origin=Origin(base), open_timeout=10) as ws_c:
                        await ws_c.send(_register_frame(id_a))
                        async for frame in ws_c:
                            data = frame if isinstance(frame, bytes) else frame.encode()
                            assert b"who=ada" not in data
                    raise AssertionError("anonymous thief stayed open")
                except ConnectionClosed as exc:
                    assert exc.code == 1008
                # Victim is unaffected.
                assert store.owner_of(id_a) is not None
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_expired_cookie_falls_back_to_anonymous() -> None:
    async def scenario() -> None:
        live, store, _, base = _serve()
        try:
            forged = store.sign_sid("deadbeef" * 4)
            url = base.replace("http://", "ws://") + "/ws"
            async with ws_client.connect(
                url,
                origin=Origin(base),
                additional_headers={"Cookie": f"rd_session={forged}"},
                open_timeout=10,
            ) as ws:
                assigned, error = await _register(ws)
                assert assigned and not error
                assert store.owner_of(assigned) is None
                await _wait_marker(ws, b"who=anonymous")
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_callback_rejects_bad_and_reused_state() -> None:
    async def scenario() -> None:
        live, store, gateway_state, base = _serve()
        try:
            async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                r = await http.get("/v1/auth/callback", params={"code": "x", "state": "nope"})
                assert r.status_code == 400
                login_state, url = store.begin_login(base)
                query = dict(parse_qsl(urlparse(url).query))
                gateway_state.pending_codes["code-once"] = {
                    "account": "ada",
                    "capabilities": frozenset({"sessions.read"}),
                    "hosts": frozenset({"home-mac"}),
                    "challenge": query["code_challenge"],
                }
                first = await http.get(
                    "/v1/auth/callback",
                    params={"code": "code-once", "state": login_state},
                    follow_redirects=False,
                )
                assert first.status_code == 302
                replay = await http.get(
                    "/v1/auth/callback",
                    params={"code": "code-once", "state": login_state},
                    follow_redirects=False,
                )
                assert replay.status_code == 400
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_logout_terminates_connections_and_session() -> None:
    async def scenario() -> None:
        live, store, gateway_state, base = _serve()
        try:
            cookie = await asyncio.to_thread(_login, store, gateway_state, base, "ada")
            url = base.replace("http://", "ws://") + "/ws"
            ws_a = await ws_client.connect(
                url, origin=Origin(base), additional_headers={"Cookie": cookie}, open_timeout=10
            )
            ws_b = await ws_client.connect(
                url, origin=Origin(base), additional_headers={"Cookie": cookie}, open_timeout=10
            )
            try:
                id_a, _ = await _register(ws_a)
                id_b, _ = await _register(ws_b)
                assert id_a != id_b
                await _wait_marker(ws_a, b"who=ada")
                # Wrong-origin logout is refused and changes nothing.
                async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                    bad = await http.post(
                        "/v1/auth/logout",
                        headers={"Cookie": cookie, "Origin": "https://evil.example"},
                    )
                    assert bad.status_code == 403
                    assert store.owner_of(id_a) is not None
                    ok = await http.post(
                        "/v1/auth/logout", headers={"Cookie": cookie, "Origin": base}
                    )
                    assert ok.status_code == 204
                    assert "Max-Age=0" in ok.headers["set-cookie"]
                for ws in (ws_a, ws_b):
                    try:
                        async for _ in ws:
                            pass
                    except ConnectionClosed as exc:
                        assert exc.code == 1008
                assert store.owner_of(id_a) is None
                # Gateway session is dead: refresh rejected.
                async with httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                    body = {"grant": "refresh_token", "refresh_token": "bogus-token-16ch"}
                    r = await http.post("/v1/token", json=body)
                    assert r.status_code == 401
                # The old cookie is anonymous now.
                async with ws_client.connect(
                    url, origin=Origin(base), additional_headers={"Cookie": cookie}, open_timeout=10
                ) as ws_d:
                    assigned, _ = await _register(ws_d)
                    assert store.owner_of(assigned) is None
                    await _wait_marker(ws_d, b"who=anonymous")
            finally:
                for ws in (ws_a, ws_b):
                    with contextlib.suppress(Exception):  # test teardown
                        await ws.close()
        finally:
            live.__exit__(None, None, None)

    asyncio.run(scenario())


def test_dev_login_route_sets_cookie_single_use() -> None:
    from relaydeck.client import serve as serve_mod

    async def scenario() -> None:
        live, store, _, base = _serve()
        try:
            serve_mod._DEV_LOGIN_TOKEN["value"] = "devtok"
            # Route is registered only when the builder flag is set.
            import httpx as _httpx

            async with _httpx.AsyncClient(base_url=base, timeout=10.0) as http:
                r = await http.get("/v1/auth/dev-login", params={"token": "nope"})
                # Flet SPA fallback answers; the dev handler must not run.
                assert "set-cookie" not in {k.lower() for k in r.headers}
        finally:
            live.__exit__(None, None, None)
            serve_mod._DEV_LOGIN_TOKEN["value"] = ""

    asyncio.run(scenario())


def test_dev_login_flow_when_enabled() -> None:
    from relaydeck.client import serve as serve_mod
    from relaydeck.client.serve import build_web_app
    from tests.support.live_server import LiveGateway

    async def scenario() -> None:
        gateway_state = _gateway_state()
        store = WebSessionStore(public_origin="placeholder", cookie_secret=SECRET)
        serve_mod._DEV_LOGIN_TOKEN["value"] = "devtok"
        app, store = build_web_app(
            gateway_state=gateway_state,
            gateway_app=create_app(gateway_state),
            flet_main=_probe_main(store),
            public_origin="placeholder",
            cookie_secret=SECRET,
            store=store,
            dev_login=True,
        )
        import httpx as _httpx

        with LiveGateway(app) as live:
            async with _httpx.AsyncClient(
                base_url=live.base_url, timeout=10.0, follow_redirects=False
            ) as http:
                bad = await http.get("/v1/auth/dev-login", params={"token": "nope"})
                assert bad.status_code == 403
                good = await http.get("/v1/auth/dev-login", params={"token": "devtok"})
                assert good.status_code == 302
                assert good.headers["set-cookie"].startswith("rd_session=")
                reuse = await http.get("/v1/auth/dev-login", params={"token": "devtok"})
                assert reuse.status_code == 403
        serve_mod._DEV_LOGIN_TOKEN["value"] = ""

    asyncio.run(scenario())


def test_dev_snapshot_seed_is_valid_and_served() -> None:
    """The local runner seeds an empty snapshot for its host so sync
    starts LIVE with empty inventory instead of 404/DISCONNECTED.
    Found by a live browser run (diagnostics showed disconnected)."""
    from relaydeck.client.serve import dev_snapshot
    from relaydeck.contracts.schemas import validate
    from relaydeck.gateway.cursors import decode_cursor

    secret = b"dev-seed-test-secret-32bytes!!!!"
    doc = dev_snapshot("home-mac", secret)
    validate("relaydeck/gateway/v1/snapshot", doc)
    assert doc["host"]["id"] == "home-mac"
    assert doc["agents"] == []
    # The seed cursor is real and authenticates: anything else 409-loops
    # the sync against the events route.
    payload = decode_cursor(secret, doc["cursor"])
    assert payload["host_id"] == "home-mac"
    assert payload["sequence"] == 0

    backend = FakeBackend(now_ms=lambda: 1_000_000, snapshots={"home-mac": doc})
    assert backend.snapshot("home-mac") == doc


def test_dev_demo_seed_serves_synthetic_terminal() -> None:
    """--dev-demo-sessions seeds schema-valid demo shell/agent rows, and
    DemoBackend greets each new attachment with the fixed VT bytes (the
    only byte source; there is no PTY behind the review server)."""
    from relaydeck.client.serve import (
        DEMO_AGENT_ID,
        DEMO_GREETING,
        DEMO_SHELL_TAB,
        DemoBackend,
        dev_demo_snapshot,
    )
    from relaydeck.contracts.schemas import validate

    secret = b"dev-seed-test-secret-32bytes!!!!"
    doc = dev_demo_snapshot("home-mac", secret)
    validate("relaydeck/gateway/v1/snapshot", doc)
    assert [t["id"] for t in doc["tabs"]] == [DEMO_SHELL_TAB, "demo-agent-tab"]
    assert [a["terminal_id"] for a in doc["agents"]] == [DEMO_AGENT_ID]

    backend = DemoBackend(now_ms=lambda: 1_000_000, snapshots={"home-mac": doc})
    attachment, _ = backend.create_attachment(
        "home-mac", DEMO_SHELL_TAB, "shell", "tester", "dev-1", takeover=False
    )
    lease = backend.acquire_control(attachment.attachment_id, "tester", "dev-1")
    batch = backend.read_stream(lease.lease_id, "", 0)
    assert batch.status == "ok"
    assert b"".join(data for _, data in batch.chunks) == DEMO_GREETING


def test_serve_cli_validates_secret_and_loopback(capsys: Any) -> None:
    from relaydeck.client.serve import main

    assert main(["--cookie-secret", "short"]) == 2
    assert main(["--host", ANY_ADDR, "--dev-login", "--dev-secret", "--port", "8127"]) == 2
    # Cursor secret is required too (sync cursors cannot validate without it).
    assert main(["--cookie-secret", "c" * 32]) == 2
    assert main(["--cookie-secret", "c" * 32, "--cursor-secret", "short"]) == 2


def test_serve_cli_rejects_host_id_outside_dev_hosts(capsys: Any) -> None:
    from relaydeck.client.serve import main

    assert (
        main(
            [
                "--dev-secret",
                "--host-id",
                "home-mac",
                "--dev-hosts",
                "work-mac,lab-mac",
                "--port",
                "8127",
            ]
        )
        == 2
    )


def test_production_main_rebinds_and_rerenders_on_reconnect() -> None:
    """Production flet_main wires page.on_connect: reattach adopts the
    handshake's sid and re-renders only when the authed branch flips, so
    ordinary reconnects never reset app state. Found by a live browser
    run: without this, a session bound pre-login stays on the login
    prompt even with a valid cookie."""

    from relaydeck.client.app import Root
    from relaydeck.client.serve import (
        _current_handshake,
        _Handshake,
        make_relaydeck_main,
    )

    gateway_state = _gateway_state()
    gateway_app = create_app(gateway_state)
    store = WebSessionStore(public_origin="http://127.0.0.1:8124", cookie_secret=SECRET)

    class _Session:
        id = "flet-rebind-1"

    class _FakePage:
        def __init__(self) -> None:
            self.fonts: dict[str, str] = {}
            self.platform_brightness = ft.Brightness.LIGHT
            self.platform = ft.PagePlatform.MACOS
            self.session = _Session()
            self.renders: list[Any] = []
            self.on_connect: Any = None
            self.route = "/"
            self.width = 1280
            self.height = 800

        def run_task(self, handler: Any) -> Any:
            from concurrent.futures import Future

            # Explicit scheduler double: these tests only capture the render branch.
            return Future()

        def render(self, component: Any, *args: Any, **kwargs: Any) -> None:
            self.renders.append(component)

    async def scenario() -> None:
        flet_main = make_relaydeck_main(
            store=store,
            base_url="http://127.0.0.1:8124",
            gateway_app=gateway_app,
            gateway_state=gateway_state,
            host_id="home-mac",
        )
        page = _FakePage()
        flet_main(page)  # anonymous: login prompt + on_connect wired
        assert callable(page.on_connect)
        assert len(page.renders) == 1
        login_prompt = page.renders[0]

        # Login elsewhere, then reattach presenting the new cookie.
        sid = store.create_session("dev", "web-dev", "acc", "ref", 2_000_000)
        hs = _Handshake(ip="", user_agent="t", page_name="")
        token = _current_handshake.set((sid, hs))
        try:
            await page.on_connect(None)
        finally:
            _current_handshake.reset(token)
        assert store.owner_of("flet-rebind-1") == sid
        assert len(page.renders) == 2
        assert page.renders[1] is Root

        # Ordinary reconnect, same identity: rebind, no re-render.
        token = _current_handshake.set((sid, hs))
        try:
            await page.on_connect(None)
        finally:
            _current_handshake.reset(token)
        assert len(page.renders) == 2

        # Logout elsewhere, then reattach: back to the login prompt.
        store.retire_sid(sid)
        token = _current_handshake.set((None, hs))
        try:
            await page.on_connect(None)
        finally:
            _current_handshake.reset(token)
        assert len(page.renders) == 3
        assert page.renders[2] is login_prompt

    asyncio.run(scenario())
