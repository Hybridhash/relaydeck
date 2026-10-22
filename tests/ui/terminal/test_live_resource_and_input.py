"""Live-resource and input integration, with synthetic Flet host sessions."""

from __future__ import annotations

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.components.appearance import AppearanceBridge
from relaydeck.client.components.design_system.terminal_window import KEY_ROW
from relaydeck.client.features.a11y.focus import focus_order
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.terminal.keys import (
    DOCK_KEYS,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    modifier_button_key,
)
from relaydeck.client.features.terminal.screen import AREA_KEY, STATUS_KEY, SURFACE_KEY
from relaydeck.client.features.terminal.surfaces import (
    AGENT_KEYBOARD_KEYS_KEY,
    AGENT_KEYBOARD_TEXT_KEY,
    AGENT_TAKEOVER_CONFIRM_KEY,
    AGENT_WORKFLOW_COMPOSER_KEY,
    AGENT_WORKFLOW_DIRECT_KEY,
)
from relaydeck.client.theme import MIN_TOUCH, palette
from tests.support.host_harness import HostApp, render_component, run
from tests.support.scripted_sessions import ScriptedSessions, make_scripted_sessions
from tests.support.terminal.direct_typing import open_compose
from tests.support.terminal.selection import _mark_ready, _open_with_snapshot
from tests.support.ui.open_terminal import AGENT_KEY, _TwoHostTransport


async def _open(width: int, dark: bool) -> tuple[HostApp, ScriptedSessions, RouteBridge]:
    sessions = make_scripted_sessions()
    transport = _TwoHostTransport()
    routes = RouteBridge(width=width)
    app = await render_component(
        Root,
        controller=SyncController(transport=transport, backoff=Backoff.seeded(1)),
        transport=transport,
        host_id="h1",
        bridge=routes,
        go=lambda route: None,
        session_factory=sessions.factory,
        appearance=AppearanceBridge(dark),
    )
    await app.settle()
    await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
    await app.settle()
    await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
    return app, sessions, routes


@pytest.mark.parametrize("width", [360, 768, 1280])
@pytest.mark.parametrize("dark", [False, True])
def test_integrated_agent_axes_preserve_draft_and_live_terminal(width: int, dark: bool) -> None:
    async def scenario() -> None:
        app, sessions, _routes = await _open(width, dark)
        terminal = app.find(SURFACE_KEY)
        session = sessions.factory.created[0]
        sidebar = app.find("inventory-sidebar")
        assert sidebar.visible == (width >= 1100)
        assert app.find("phone-nav-slot").visible is False
        if width < 1100:
            assert "inventory-query" not in focus_order(app.page)
        else:
            if dark:
                await app.set_props_and_fire("console-search", {}, "click")
            assert "inventory-query" in focus_order(app.page)
        if width < 1024:
            await open_compose(app)
        await app.set_props_and_fire(
            "composer-draft", {"value": "draft 中文 العربية"}, "change", "draft 中文 العربية"
        )
        assert app.find("composer-draft").max_lines == (2 if width < 1024 else 3)
        if width < 1024:
            await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
            assert app.find("mobile-keybar").visible
            assert app.find("key-enter").content.height == 34
            with pytest.raises(KeyError):
                app.find(AGENT_KEYBOARD_KEYS_KEY)
            assert app.find(SURFACE_KEY) is terminal
            assert app.find("composer-box").visible is False
            assert app.find("composer-draft").can_request_focus is False
            assert app.find("composer-draft").disabled is True
            assert session.composer_props()["draft"] == "draft 中文 العربية"
            await open_compose(app)
            assert app.find("composer-draft").value == "draft 中文 العربية"
            assert app.find("mobile-keybar").visible is False
        else:
            await app.set_props_and_fire(AGENT_KEYBOARD_KEYS_KEY, {}, "click")
            assert app.find("composer-draft").value == "draft 中文 العربية"
            # The desktop dock dedupes against the inline
            # row, so Enter lives inline now; every dock key keeps the
            # same 48px floor in its new pill location.
            with pytest.raises(KeyError):
                app.find("tools-key-enter")
            assert app.find("tools-keys-deduped") is not None
            assert app.find("shortcut-key-home").height >= MIN_TOUCH
            assert app.find("tools-grid-scroll").scroll == ft.ScrollMode.AUTO
            await app.set_props_and_fire(AGENT_WORKFLOW_DIRECT_KEY, {}, "click")
            assert app.find(SURFACE_KEY) is terminal
            with pytest.raises(KeyError):
                app.find("composer-draft")
            assert session.composer_props()["draft"] == "draft 中文 العربية"
            await app.set_props_and_fire("tools-page-terminal", {}, "click")
            inline = frozenset(name for name, _, _ in KEY_ROW)
            expected = [name for name, _, _ in DOCK_KEYS if name not in inline]
            assert expected
            for name in expected:
                assert app.find(f"shortcut-key-{name}").height >= MIN_TOUCH
            for mod in (MOD_CTRL, MOD_ALT, MOD_SHIFT):
                assert app.find(modifier_button_key(mod)) is not None
            await app.set_props_and_fire(AGENT_WORKFLOW_COMPOSER_KEY, {}, "click")
            assert app.find("composer-draft").value == "draft 中文 العربية"
            with pytest.raises(KeyError):
                app.find(AGENT_KEYBOARD_KEYS_KEY)
            await app.set_props_and_fire(AGENT_KEYBOARD_TEXT_KEY, {}, "click")
        assert app.find(SURFACE_KEY) is terminal
        assert sessions.factory.created == [session]
        assert len(sessions.streams.opened) == 1
        if width < 1024:
            assert app.find(AREA_KEY).border is None
            assert app.find(AREA_KEY).border_radius == 0
            # The status line is excluded from the tree (not
            # merely invisible) so short heights reclaim its rows.
            with pytest.raises(KeyError):
                app.find("terminal-toolbar")
        else:
            assert app.find(AREA_KEY).border.top.color == palette(dark)["line"]
            assert app.find(AREA_KEY).border_radius == 18
            assert app.find(STATUS_KEY).color == palette(dark)["terminal_muted"]
        if width < 1100:
            await app.set_props_and_fire("terminal-back", {}, "click")
        else:
            # Wide layouts have no Back: the list picks the
            # session, so leave through another destination instead.
            with pytest.raises(KeyError):
                app.find("terminal-back")
            if dark:
                await app.set_props_and_fire("console-rail-terminals", {}, "click")
            else:
                await app.set_props_and_fire("destinations", {"selected_index": 1}, "change")
        assert app.find("inventory-sidebar").visible is True
        assert app.find("detail-pane").visible == (width >= 1100)
        assert session.disposed is False and not session.foreground

    run(scenario())


def test_drag_keyboard_collapse_and_breakpoint_keep_engine_identity() -> None:
    async def scenario() -> None:
        app, sessions, routes = await _open(1280, True)
        terminal = app.find(SURFACE_KEY)
        session = sessions.factory.created[0]
        assert app.find("inventory-sidebar").width == 380
        await app.set_props_and_fire("console-search", {}, "click")
        await app.set_props_and_fire("sidebar-widen", {}, "click")
        assert app.find("inventory-sidebar").width == 404
        await app.set_props_and_fire(
            "sidebar-resize",
            {},
            "horizontal_drag_update",
            {
                "l": {"x": 0, "y": 0},
                "g": {"x": 0, "y": 0},
                "pd": -900,
            },
        )
        assert app.find("inventory-sidebar").width == 240
        await app.set_props_and_fire(
            "sidebar-resize",
            {},
            "horizontal_drag_update",
            {
                "l": {"x": 0, "y": 0},
                "g": {"x": 0, "y": 0},
                "pd": 900,
            },
        )
        assert app.find("inventory-sidebar").width == 420
        await app.set_props_and_fire("console-collapse-toggle", {}, "click")
        assert app.find("inventory-sidebar").width == 80
        assert app.find("console-strip") is not None
        assert "inventory-query" not in focus_order(app.page)
        assert app.find(SURFACE_KEY) is terminal
        await app.set_props_and_fire("sidebar-show", {}, "click")
        assert app.find("inventory-sidebar").width == 420
        for width in (768, 360, 1280):
            routes.set_size(width, 800)
            await app.settle()
            assert app.find(SURFACE_KEY) is terminal
        assert app.find("inventory-sidebar").width == 420
        assert sessions.factory.created == [session]
        assert len(sessions.ops.attaches) == 1 and not session.disposed
        assert sessions.ops.releases == []

    run(scenario())


def test_read_only_selection_fences_input_and_keeps_engine() -> None:
    async def scenario() -> None:
        app, sessions, _routes = await _open(360, True)
        await app.set_props_and_fire("agent-workflow-toggle", {}, "click")
        session = sessions.factory.created[0]
        terminal, sent = _mark_ready(session)
        assert terminal.input_enabled is True
        await _open_with_snapshot(app, terminal, sent, "synthetic output")
        assert app.find(SURFACE_KEY) is terminal
        assert terminal.input_enabled is False
        assert app.find("agent-input-dock").disabled is True
        assert "agent-workflow-toggle" not in focus_order(app.page)
        assert app.find("terminal-surface-stack").fit == ft.StackFit.EXPAND
        await app.set_props_and_fire("phone-select-done", {}, "click")
        assert terminal.input_enabled is True
        assert app.find("agent-input-dock").disabled is False
        assert "terminal-tools-button" in focus_order(app.page)
        assert app.find(SURFACE_KEY) is terminal
        assert len(sessions.streams.opened) == 1

    run(scenario())
