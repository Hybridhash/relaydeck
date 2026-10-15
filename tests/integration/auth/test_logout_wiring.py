"""run_app logout wiring + flet_main teardown.

run_app must accept and forward a real logout callback (today the
Settings button substitutes lambda: None), and the served logout must
revoke the gateway session, retire the sid, and flip the live page to
the login branch without deleting the page's own Flet session.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from relaydeck.client.app import Root, run_app
from relaydeck.client.serve import (
    WebSessionStore,
    _current_handshake,
    _Handshake,
    make_relaydeck_main,
)
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend

SECRET = b"flet-boundary-test-secret-32bytes!!"
NOW = 2_000_000


class _CapPage:
    """Fake page capturing render calls (component + props)."""

    def __init__(self) -> None:
        self.fonts: dict[str, str] = {}
        self.platform_brightness = ft.Brightness.LIGHT
        self.platform = ft.PagePlatform.MACOS
        self.session = type("Session", (), {"id": "f1"})()
        self.route = "/"
        self.width = 1280
        self.height = 800
        self.on_connect: Any = None
        self.on_route_change: Any = None
        self.on_resize: Any = None
        self.renders: list[tuple[Any, dict[str, Any]]] = []

    def run_task(self, handler: Any) -> Any:
        from concurrent.futures import Future

        # Explicit scheduler double: these tests only capture the render branch.
        return Future()

    def render(self, component: Any, *args: Any, **kwargs: Any) -> None:
        self.renders.append((component, kwargs))


def test_run_app_forwards_on_logout() -> None:
    async def token() -> str:
        return "t"

    page = _CapPage()
    sentinel = lambda: None  # noqa: E731
    run_app(
        page,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:9",
        get_token=token,
        host_id="h1",
        on_logout=sentinel,
    )
    assert page.renders and page.renders[0][0] is Root
    assert page.renders[0][1]["on_logout"] is sentinel


def test_flet_main_logout_tears_down_and_shows_login() -> None:
    async def scenario() -> None:
        gateway_state = GatewayState(
            backend=FakeBackend(now_ms=lambda: NOW),
            cursor_secret=b"x" * 32,
            now_ms=lambda: NOW,
        )
        store = WebSessionStore(public_origin="http://127.0.0.1:9", cookie_secret=SECRET)
        access, refresh = gateway_state.sessions.create(
            "dev", frozenset({"sessions.read"}), frozenset({"h1"}), NOW, "web-dev"
        )
        sid = store.create_session("dev", "web-dev", access, refresh, NOW + 900_000)
        handshake = _Handshake(ip="", user_agent="t", page_name="")
        store.bind_flet(sid, "f1", handshake)
        flet_main = make_relaydeck_main(
            store=store,
            base_url="http://127.0.0.1:9",
            gateway_app=create_app(gateway_state),
            gateway_state=gateway_state,
            host_id="h1",
        )
        page = _CapPage()
        token = _current_handshake.set((sid, handshake))
        try:
            flet_main(page)
        finally:
            _current_handshake.reset(token)
        assert page.renders and page.renders[0][0] is Root
        on_logout = page.renders[0][1].get("on_logout")
        assert callable(on_logout)
        on_logout()
        for _ in range(50):
            if len(page.renders) > 1:
                break
            await asyncio.sleep(0.01)
        assert store.identity(sid) is None
        assert gateway_state.sessions.lookup(access, NOW) is None
        assert len(page.renders) == 2
        assert page.renders[1][0] is not Root

    asyncio.run(scenario())
