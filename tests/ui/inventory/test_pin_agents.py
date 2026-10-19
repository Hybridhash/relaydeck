"""Pin to top: menu row, card order, indicator and persistence through the real Root."""

from __future__ import annotations

import json
from typing import Any, cast

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import PREFS_KEY, TerminalPrefs, encode
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from tests.support.host_harness import HostApp, render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.navigation import _state, _StubController, _StubTransport
from tests.support.ui.spaces_filter import _spaces_state

KEY = "home-mac:term-1"


class _Store:
    def __init__(self, stored: TerminalPrefs | None = None) -> None:
        self.data = {PREFS_KEY: encode(stored)} if stored is not None else {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


async def _root(store: _Store, width: int) -> HostApp:
    controller = _StubController()
    sessions = make_scripted_sessions()

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            controls=[
                Root(
                    controller=cast(SyncController, controller),
                    transport=cast(HttpxGatewayTransport, _StubTransport()),
                    host_id="home-mac",
                    bridge=RouteBridge(width=width, height=844),
                    go=lambda route: None,
                    session_factory=sessions.factory,
                    account="synthetic",
                    preferences=store,
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
            ]
        )

    app = await render_component(Parent)
    controller.push(_spaces_state())
    await app.settle()
    return app


def _order(app: HostApp) -> list[str]:
    return [c.key.removeprefix("agent-swipe-") for c in app.find("agents-tiles").controls]


def _present(app: HostApp, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


async def _pin_via_menu(app: HostApp, key: str) -> None:
    await app.set_props_and_fire(f"agent-gesture-{key}", {}, "long_press_start")
    menu = app.find(f"agent-menu-{key}")
    assert menu.open
    await app.set_props_and_fire(f"agent-pin-{key}", {}, "click")
    assert not app.find(f"agent-menu-{key}").open  # closes natively first
    await app.session.dispatch_event(app.find(f"agent-menu-{key}")._i, "dismiss", "")
    await app.settle()
    assert not _present(app, f"agent-menu-{key}")


def _menu_labels(app: HostApp, key: str) -> list[str]:
    rows = app.find(f"agent-menu-{key}").content.content.controls
    return [row.content.controls[0].value for row in rows]


@pytest.mark.parametrize("width", [390, 1280])
def test_pin_moves_the_card_first_shows_the_indicator_and_unpin_restores(width: int) -> None:
    async def scenario() -> None:
        store = _Store()
        app = await _root(store, width)
        try:
            before = _order(app)
            target = "home-mac:d2"
            assert before[0] != target
            await app.set_props_and_fire(f"agent-gesture-{target}", {}, "long_press_start")
            assert _menu_labels(app, target) == ["Rename", "Pin to top", "Open terminal"]
            await app.set_props_and_fire(f"agent-gesture-{target}", {}, "secondary_tap")
            await app.set_props_and_fire(f"agent-pin-{target}", {}, "click")
            assert not app.find(f"agent-menu-{target}").open
            await app.session.dispatch_event(app.find(f"agent-menu-{target}")._i, "dismiss", "")
            await app.settle()
            assert _order(app)[0] == target
            assert _present(app, f"agent-pinned-{target}")
            assert json.loads(store.data[PREFS_KEY])["pinned_agents"] == [target]
            rest = [key for key in _order(app) if key != target]
            assert rest == [key for key in before if key != target]
            await app.set_props_and_fire(f"agent-gesture-{target}", {}, "long_press_start")
            assert _menu_labels(app, target) == ["Rename", "Unpin", "Open terminal"]
            await app.set_props_and_fire(f"agent-pin-{target}", {}, "click")
            await app.session.dispatch_event(app.find(f"agent-menu-{target}")._i, "dismiss", "")
            await app.settle()
            assert _order(app) == before and not _present(app, f"agent-pinned-{target}")
            assert json.loads(store.data[PREFS_KEY])["pinned_agents"] == []
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_pins_survive_a_remount_and_missing_agents_are_pruned(width: int) -> None:
    async def scenario() -> None:
        store = _Store(TerminalPrefs(pinned_agents=("home-mac:d2", "home-mac:gone")))
        app = await _root(store, width)
        try:
            assert _order(app)[0] == "home-mac:d2"
            assert _present(app, "agent-pinned-home-mac:d2")
            await _pin_via_menu(app, "home-mac:w2")  # a second pin prunes the dead one
            assert json.loads(store.data[PREFS_KEY])["pinned_agents"] == [
                "home-mac:d2",
                "home-mac:w2",
            ]
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        again = await _root(store, width)
        try:
            assert _order(again)[:2] == [
                "home-mac:w2",
                "home-mac:d2",
            ]  # status order inside the group
        finally:
            await again.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


@pytest.mark.parametrize("dark", [True, False])
@pytest.mark.parametrize("width", [390, 1280])
def test_both_themes_show_the_indicator_and_the_menu_row(width: int, dark: bool) -> None:
    async def scenario() -> None:
        saved: list[TerminalPrefs] = []

        @ft.component
        def Parent() -> Any:
            prefs, set_prefs = ft.use_state(TerminalPrefs())

            def on_prefs(value: TerminalPrefs) -> None:
                saved.append(value)
                set_prefs(value)

            return AppShell(
                state=_state(),
                is_wide=width >= 1100,
                viewport_width=width,
                destination="agents",
                command_transport=_Transport(),
                foreground=True,
                dark=dark,
                prefs=prefs,
                on_prefs=on_prefs,
                on_destination=lambda value: None,
                on_select_agent=lambda value: None,
                on_select_shell=lambda value: None,
                on_select_host=lambda value: None,
                on_banner_action=lambda: None,
                on_open_terminal=lambda: None,
                on_start_agent=lambda: None,
                on_new_terminal=lambda: None,
            )

        app = await render_component(Parent)
        assert not _present(app, f"agent-pinned-{KEY}")
        await app.set_props_and_fire(f"agent-gesture-{KEY}", {}, "long_press_start")
        assert _menu_labels(app, KEY) == ["Rename", "Pin to top", "Open terminal"]
        await app.set_props_and_fire(f"agent-pin-{KEY}", {}, "click")
        await app.settle()
        assert saved and saved[-1].pinned_agents == (KEY,)
        assert _present(app, f"agent-pinned-{KEY}")

    run(scenario())


class _Transport:
    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("pinning sends no command")

    async def command_status(self, host: str, command_id: str) -> dict[str, Any]:
        raise AssertionError("pinning sends no command")
