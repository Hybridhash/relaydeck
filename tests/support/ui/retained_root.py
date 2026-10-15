"""Retained-session Root rendering helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import flet as ft

from relaydeck.client.app import Root
from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.client.retained_sessions import RetainedSessions
from relaydeck.domain.models import Host
from tests.support.host_harness import render_component
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController, _StubTransport


async def _render(
    *,
    bridge: RouteBridge | None = None,
    appearance: AppearanceBridge | None = None,
    pending_commands: Any = None,
    touch: bool = False,
    go: Any = None,
    preferences: Any = None,
) -> tuple[Any, ...]:
    controller = _StubController()
    sessions = make_scripted_sessions()
    now = [1000]
    pool = RetainedSessions(clock=lambda: now[0])
    state = _state()
    first = state.inventory.agents["home-mac:term-1"]
    agents = dict(state.inventory.agents)
    for index in range(2, 7):
        agents[f"home-mac:term-{index}"] = replace(
            first, terminal_id=f"term-{index}", name=f"agent-{index}"
        )
    agents["other-mac:term-1"] = replace(first, host_id="other-mac", name="other-host")
    hosts = {
        **state.inventory.hosts,
        "other-mac": Host(id="other-mac", label="Other Mac", epoch="epoch-8", reachable=True),
    }
    state = replace(state, inventory=replace(state.inventory, agents=agents, hosts=hosts))
    bridge = bridge or RouteBridge(route="/", width=1280)

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        foreground, set_foreground = ft.use_state(True)
        return ft.Column(
            controls=[
                Root(
                    controller=cast(SyncController, controller),
                    transport=cast(HttpxGatewayTransport, _StubTransport()),
                    host_id="home-mac",
                    bridge=bridge,
                    appearance=appearance,
                    go=go or (lambda route: None),
                    preferences=preferences,
                    session_factory=sessions.factory,
                    account="synthetic",
                    retained_sessions=pool,
                    pending_commands=pending_commands,
                    foreground=foreground,
                    touch=touch,
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
                ft.Button(
                    "Background", key="test-background", on_click=lambda e: set_foreground(False)
                ),
                ft.Button(
                    "Foreground", key="test-foreground", on_click=lambda e: set_foreground(True)
                ),
            ]
        )

    app = await render_component(Parent)
    controller.push(state)
    await app.settle()
    return app, controller, sessions, pool, now


def _active(app: Any, key: str) -> Any:
    """First control with `key` outside disabled (parked) subtrees."""
    for control in iter_controls([app.page.views[0].controls], active_only=True):
        if getattr(control, "key", None) == key:
            return control
    raise KeyError(key)


async def _open(app: Any, key: str) -> None:
    await app.set_props_and_fire(f"agent-{key}", {}, "click")
    await app.settle()
    await app.settle()
    await app.set_props_and_fire("agent-takeover-confirm", {}, "click")
    await app.settle()
