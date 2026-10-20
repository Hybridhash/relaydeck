"""Foundation integration on real Flet sessions; no rendered-pixel claims."""

from __future__ import annotations

import flet as ft
import pytest
from flet.messaging.protocol import MessageAction

from relaydeck.client.app import Root
from relaydeck.client.components.appearance import (
    build_theme,
    chrome_style,
    configure_page_theme,
)
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.terminal.screen import SURFACE_KEY
from relaydeck.client.features.terminal.surfaces import AGENT_TAKEOVER_CONFIRM_KEY
from relaydeck.client.theme import MIN_TOUCH, palette
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.open_terminal import AGENT_KEY, _TwoHostTransport
from tests.support.ui.visual import _render_shell, _state


@pytest.mark.parametrize("dark", [False, True])
def test_explicit_flet_roles_and_control_states(dark: bool) -> None:
    t = palette(dark)
    theme = build_theme(dark)
    scheme = theme.color_scheme
    assert scheme is not None
    assert (scheme.primary, scheme.on_primary) == (t["accent"], t["on_accent"])
    assert (scheme.surface, scheme.on_surface) == (t["surface"], t["text"])
    assert scheme.surface_container_lowest == t["bg"]
    assert scheme.outline == t["outline"]
    assert theme.color_scheme_seed is None
    assert theme.text_theme is not None
    assert theme.text_theme.body_medium is not None
    assert theme.text_theme.body_medium.color == t["text"]
    style = chrome_style(dark)
    assert isinstance(style.bgcolor, dict)
    assert isinstance(style.color, dict)
    assert isinstance(style.side, dict)
    assert style.bgcolor[ft.ControlState.HOVERED] == t["active_row_bg"]
    assert style.bgcolor[ft.ControlState.FOCUSED] == t["active_row_bg"]
    assert style.color[ft.ControlState.DISABLED] == t["disabled_text"]
    assert style.side[ft.ControlState.FOCUSED].color == t["accent_text"]
    primary = chrome_style(dark, primary=True)
    assert isinstance(primary.color, dict)
    assert primary.color[ft.ControlState.DEFAULT] == t["on_accent"]


@pytest.mark.parametrize("payload_kind", ["scalar", "typed", "page-state"])
def test_page_system_theme_event_and_bridge_cleanup(payload_kind: str) -> None:
    async def scenario() -> None:
        app = await _render_shell(_state())
        bridge = configure_page_theme(app.page, mode=ft.ThemeMode.SYSTEM)
        assert app.page.theme_mode == ft.ThemeMode.SYSTEM
        assert app.page.theme.color_scheme.surface == palette(False)["surface"]
        assert app.page.dark_theme.color_scheme.surface == palette(True)["surface"]
        assert app.page.padding == 0
        changed: list[bool] = []
        unsubscribe = bridge.subscribe(changed.append)
        received: list[ft.Event[ft.Page]] = []
        handler = app.page.on_platform_brightness_change

        def capture(event: ft.Event[ft.Page]) -> None:
            received.append(event)
            handler(event)

        app.page.on_platform_brightness_change = capture

        async def brightness(value: str) -> None:
            # Pinned carrier sends UPDATE_CONTROL_PROPS before CONTROL_EVENT.
            app.session.apply_patch(app.page._i, {"platform_brightness": value})
            payload = (
                {"brightness": value}
                if payload_kind == "typed"
                else (value if payload_kind == "scalar" else None)
            )
            await app.session.dispatch_event(app.page._i, "platform_brightness_change", payload)
            await app.settle()
            assert app.page.platform_brightness == ft.Brightness(value)
            assert bridge.dark == (value == "dark")
            assert isinstance(received[-1], ft.PlatformBrightnessChangeEvent) == (
                payload_kind == "typed"
            )
            if payload_kind != "typed":
                assert not hasattr(received[-1], "brightness")

        for value in ("dark", "dark", "light", "light", "dark"):
            await brightness(value)
        assert changed == [True, False, True]
        # Unknown payload falls back to the valid, already-patched page mode.
        await app.session.dispatch_event(app.page._i, "platform_brightness_change", "unknown")
        assert bridge.dark is True and changed == [True, False, True]
        unsubscribe()
        await brightness("light")
        assert changed == [True, False, True]
        assert app.page.theme_mode == ft.ThemeMode.SYSTEM
        assert not any(message.action == MessageAction.SESSION_CRASHED for message in app.conn.sent)

    run(scenario())


@pytest.mark.parametrize(
    "width,list_size,rail_size", [(360, None, 0), (768, None, 96), (1280, 380, 96)]
)
@pytest.mark.parametrize("dark", [False, True])
def test_inventory_uses_bounded_flex_chain_and_explicit_row_text(
    width: int, list_size: int | None, rail_size: int, dark: bool
) -> None:
    async def scenario() -> None:
        app = await _render_shell(_state(), viewport_width=width, dark=dark)
        t = palette(dark)
        assert app.find("inventory-frame").expand is True
        assert app.find("inventory-content").expand is True
        assert app.find("agents-pane-bounds").expand is True
        assert app.find("agents-list-bounds").expand is True
        assert app.find("agents-tiles").expand is True
        assert app.find("agents-pane").width == list_size
        if rail_size:
            assert app.find("destinations").width == rail_size
            assert app.find("inventory-split").expand is True
        else:
            assert app.find("destinations").height >= MIN_TOUCH
        row = app.find("agent-home-mac:term-1")
        if dark:
            assert isinstance(row, ft.TextButton)
            assert row.tooltip == ("Tool-a agent, Working, web" if width >= 1024 else None)
            badge = app.find("agent-badge-home-mac:term-1")
            assert badge.content.value == "Tool-a"
            assert row.content.padding.top + row.content.padding.bottom == 24
            assert any(
                isinstance(control, ft.Text) and control.value == "web"
                for control in iter_controls([row])
            )
            await app.set_props_and_fire("console-search", {}, "click")
        else:
            assert row.title.color == t["text"]
            subtitle_row = row.subtitle
            assert isinstance(subtitle_row, ft.Row)
            assert subtitle_row.controls[0].key == "agent-badge-home-mac:term-1"
            assert subtitle_row.controls[0].content.value == "Tool-a"
            subtitle = subtitle_row.controls[1]
            assert subtitle.color == t["muted"]
            assert row.title.max_lines == 1
            assert row.title.tooltip == (row.title.value if width >= 1024 else None)
            assert row.min_height >= MIN_TOUCH
        field = app.find("inventory-query")
        assert field.color == t["text"]
        assert field.fill_color == t["raised"]
        assert field.border[ft.ControlState.FOCUSED].side.color == t["accent_text"]
        queries: list[str] = []
        # The real tree's handler preserves existing search payload handling.
        await app.set_props_and_fire("inventory-query", {"value": "ops"}, "change", "ops")
        assert app.find("agents-count").value == "1 agents"
        queries.append(app.find("inventory-query").value)
        assert queries == ["ops"]

    run(scenario())


def test_theme_and_viewport_updates_preserve_live_session_and_draft() -> None:
    async def scenario() -> None:
        sessions = make_scripted_sessions()
        transport = _TwoHostTransport()
        controller = SyncController(transport=transport, backoff=Backoff.seeded(1))
        routes = RouteBridge(width=1280)
        # Configure the real Page before mounting Root, as the shipped entry
        # points do; brightness reaches its existing subscribed bridge.
        app = await render_component(ft.Column)
        appearance = configure_page_theme(app.page, mode=ft.ThemeMode.SYSTEM)
        app.page.render(
            Root,
            controller=controller,
            transport=transport,
            host_id="h1",
            bridge=routes,
            go=lambda route: None,
            session_factory=sessions.factory,
            appearance=appearance,
        )
        app.session.get_page_patch()
        await app.settle()
        assert app.find("agents-pane").bgcolor == palette(False)["surface"]
        await app.set_props_and_fire(f"agent-{AGENT_KEY}", {}, "click")
        await app.settle()
        await app.set_props_and_fire(AGENT_TAKEOVER_CONFIRM_KEY, {}, "click")
        await app.settle()
        session = sessions.factory.created[0]
        await app.set_props_and_fire(
            "composer-draft", {"value": "keep هذا draft"}, "change", "keep هذا draft"
        )
        terminal = app.find(SURFACE_KEY)
        input_token = session.input_token
        app.session.apply_patch(app.page._i, {"platform_brightness": "dark"})
        await app.session.dispatch_event(app.page._i, "platform_brightness_change", "dark")
        routes.set_size(360, 800)
        await app.settle()
        assert session.phase == "live" and not session.disposed
        assert sessions.factory.created == [session]
        assert len(sessions.streams.opened) == 1
        assert app.find(SURFACE_KEY) is terminal
        assert session.input_token == input_token
        assert app.find("composer-draft").value == "keep هذا draft"
        assert app.find("composer-draft").fill_color == palette(True)["raised"]
        assert app.find("composer-draft").max_lines == 2
        app.session.apply_patch(app.page._i, {"platform_brightness": "light"})
        await app.session.dispatch_event(app.page._i, "platform_brightness_change", "light")
        routes.set_size(768, 1024)
        await app.settle()
        assert app.find(SURFACE_KEY) is terminal
        assert app.find("composer-draft").value == "keep هذا draft"
        assert app.find("composer-draft").fill_color == palette(False)["raised"]
        await app.set_props_and_fire("terminal-back", {}, "click")
        await app.settle()
        assert app.find("agents-pane").width is None
        assert app.find("agents-pane").bgcolor == palette(False)["surface"]
        assert app.page.theme_mode == ft.ThemeMode.SYSTEM
        assert not any(message.action == MessageAction.SESSION_CRASHED for message in app.conn.sent)

    run(scenario())
