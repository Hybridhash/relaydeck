"""A dock agent button whose agent is working shows a small green dot that blinks.

One shared timer per tab row (`AgentTabRow`). It runs only while the caller's `blink` signal is
true (the agent session is foreground and not paused) and a shown tab is working. No page
lifecycle handler is installed; backgrounding reaches the dock through that signal. Blinks
re-render only the tab row, never the composer. Synthetic agents; the interval is shortened.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.terminal.surfaces import agent_tab_row
from relaydeck.client.features.terminal.surfaces.agent_tab_row import DIM_OPACITY, AgentTabRow
from tests.support.host_harness import render_component, run
from tests.support.ui.instant_inventory import valid_inventory
from tests.support.ui.navigation import _StubTransport
from tests.support.ui.retained_root import _open, _render

TOKENS = {"raised": "#E9EAEE", "success": "#216A36"}
DOT = "terminal-agent-working-dot-a2"
SIBLING_RENDERS = {"count": 0}
BLINKS = {"active": 0}


@ft.component
def _Composer() -> Any:
    # Stands in for the composer: must not re-render while the dot blinks.
    SIBLING_RENDERS["count"] += 1
    return ft.Text("composer", key="composer-stand-in")


@ft.component
def _Dock() -> Any:
    status, set_status = ft.use_state("working")
    blink, set_blink = ft.use_state(True)
    mounted, set_mounted = ft.use_state(True)
    tabs = (("a1", "One", "idle"), ("a2", "Two", status))
    return ft.Column(
        controls=[
            AgentTabRow(
                visible=True,
                agent_tabs=tabs,
                agent_status=status,
                agent_title="Two",
                on_agent_tab=None,
                selected_agent="a2",
                tokens=TOKENS,
                blink=blink,
            )
            if mounted
            else ft.Text("unmounted", key="unmounted"),
            _Composer(),
            ft.Button("idle", key="set-idle", on_click=lambda e: set_status("idle")),
            ft.Button("working", key="set-working", on_click=lambda e: set_status("working")),
            ft.Button("background", key="set-background", on_click=lambda e: set_blink(False)),
            ft.Button("foreground", key="set-foreground", on_click=lambda e: set_blink(True)),
            ft.Button("unmount", key="unmount", on_click=lambda e: set_mounted(False)),
        ]
    )


@pytest.fixture
def counted_blink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_tab_row, "WORKING_BLINK_S", 0.01)
    original = agent_tab_row._blink

    async def counted(set_lit: Any) -> None:
        BLINKS["active"] += 1
        try:
            await original(set_lit)
        finally:
            BLINKS["active"] -= 1

    monkeypatch.setattr(agent_tab_row, "_blink", counted)
    SIBLING_RENDERS["count"] = 0
    BLINKS["active"] = 0


def _maybe(app: Any, key: str) -> Any:
    """`app.find` raises for a missing key; these checks expect absence as well as presence."""
    try:
        return app.find(key)
    except KeyError:
        return None


async def _sample_opacities(app: Any, rounds: int = 12) -> set[float]:
    seen: set[float] = set()
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        await app.settle()
        control = _maybe(app, DOT)
        if control is not None:
            seen.add(control.opacity)
    return seen


def test_dot_is_shown_only_for_working_tabs_and_labelled() -> None:
    async def scenario() -> None:
        app = await render_component(_Dock)
        try:
            await app.settle()
            assert app.find(DOT) is not None
            assert app.find("terminal-agent-tab-a2").tooltip == "Two, working"
            assert app.find("terminal-agent-tab-a1").tooltip is None
            assert _maybe(app, "terminal-agent-working-dot-a1") is None

            await app.set_props_and_fire("set-idle", {}, "click")
            await app.settle()
            assert _maybe(app, DOT) is None
            assert app.find("terminal-agent-tab-a2").tooltip is None
        finally:
            await app.set_props_and_fire("unmount", {}, "click")

    run(scenario())


def test_dot_blinks_while_blink_and_working_without_rendering_the_composer(
    counted_blink: None,
) -> None:
    async def scenario() -> None:
        app = await render_component(_Dock)
        try:
            await app.settle()
            assert BLINKS["active"] == 1
            renders_before = SIBLING_RENDERS["count"]
            assert await _sample_opacities(app) == {DIM_OPACITY, 1.0}
            assert SIBLING_RENDERS["count"] == renders_before
        finally:
            await app.set_props_and_fire("unmount", {}, "click")
        await asyncio.sleep(0)
        assert BLINKS["active"] == 0

    run(scenario())


def test_blink_stops_when_backgrounded_resumes_when_foregrounded_and_stops_when_idle_or_unmounted(
    counted_blink: None,
) -> None:
    async def scenario() -> None:
        app = await render_component(_Dock)
        try:
            await app.settle()
            assert BLINKS["active"] == 1

            await app.set_props_and_fire("set-background", {}, "click")
            await app.settle()
            await asyncio.sleep(0.02)
            assert BLINKS["active"] == 0
            assert await _sample_opacities(app, rounds=4) == {1.0}

            await app.set_props_and_fire("set-foreground", {}, "click")
            await app.settle()
            assert BLINKS["active"] == 1

            await app.set_props_and_fire("set-idle", {}, "click")
            await app.settle()
            await asyncio.sleep(0.02)
            assert BLINKS["active"] == 0
            assert _maybe(app, DOT) is None

            await app.set_props_and_fire("set-working", {}, "click")
            await app.settle()
            assert BLINKS["active"] == 1
        finally:
            await app.set_props_and_fire("unmount", {}, "click")
        await asyncio.sleep(0)
        assert BLINKS["active"] == 0

    run(scenario())


def test_a_parked_or_suspended_session_dock_never_blinks_and_follows_foreground(
    counted_blink: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mount-level: a parked session (`set_foreground(False)`) stops the blink and foregrounding
    restarts it. `suspend_input` (the pause path) is NOT covered here: it does not notify, so the
    dock would not re-render.
    """
    monkeypatch.setattr(_StubTransport, "snapshot", valid_inventory)

    async def scenario() -> None:
        app, _controller, _scripted, pool, _now = await _render(
            bridge=RouteBridge(route="/agents", width=390, height=844)
        )
        try:
            await _open(app, "home-mac:term-1")
            await app.settle()
            session = pool.foreground.session
            await asyncio.sleep(0.02)
            assert BLINKS["active"] == 1

            session.set_foreground(False)  # parked retained session
            await app.settle()
            await asyncio.sleep(0.02)
            assert BLINKS["active"] == 0

            session.set_foreground(True)
            await app.settle()
            assert BLINKS["active"] == 1

        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        await asyncio.sleep(0)
        assert BLINKS["active"] == 0

    run(scenario())
