"""Host/device management rendering."""

from __future__ import annotations

import time
from typing import Any, cast

import flet as ft
import httpx

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.connector.pubkey import fingerprint_of_raw, generate, openssh_to_raw
from relaydeck.gateway.app import create_app
from tests.security.helpers import ALL_CAPS, make_state
from tests.support.host_harness import render_component
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController


async def _render(width: int = 360) -> tuple[Any, ...]:
    state, backend = make_state()
    now = int(time.time() * 1000)
    state.now_ms = lambda: now
    seed, public = generate()
    fingerprint = fingerprint_of_raw(openssh_to_raw(public))
    host_token = state.hosts.enroll("home-mac", "Home", "acct-1", public, fingerprint)
    token, _ = state.sessions.create("acct-1", ALL_CAPS, frozenset({"home-mac"}), now, "this-login")
    other, refresh = state.sessions.create(
        "acct-1", ALL_CAPS, frozenset({"home-mac"}), now, "other-login"
    )

    async def get_token() -> str:
        return token

    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
    )
    transport = HttpxGatewayTransport("http://gw", get_token, http)
    controller = _StubController()
    sessions = make_scripted_sessions()
    logged_out: list[bool] = []

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            controls=[
                Root(
                    controller=cast(SyncController, controller),
                    transport=transport,
                    host_id="home-mac",
                    bridge=RouteBridge(route="/", width=width),
                    go=lambda route: None,
                    session_factory=sessions.factory,
                    account="acct-1",
                    on_logout=lambda: logged_out.append(True),
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
            ]
        )

    app = await render_component(Parent)
    controller.push(_state())
    await app.settle()
    return app, http, state, controller, public, fingerprint, host_token, other, refresh, logged_out
