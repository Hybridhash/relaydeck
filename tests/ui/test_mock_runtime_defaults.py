"""Regressions for the user-visible first-launch theme and width selection."""

import flet as ft
import pytest

from relaydeck.client.components.appearance import configure_page_theme
from relaydeck.client.resources import build_resources
from tests.support.host_harness import render_component, run
from tests.support.ui.visual import _render_shell, _state


def test_shipped_theme_keeps_mock_on_light_os_and_brightness_events() -> None:
    async def scenario() -> None:
        app = await render_component(ft.Column)
        app.session.apply_patch(app.page._i, {"platform_brightness": "light"})
        appearance = configure_page_theme(app.page)
        assert app.page.theme_mode == ft.ThemeMode.DARK
        assert appearance.dark
        for value in ("dark", "light"):
            app.session.apply_patch(app.page._i, {"platform_brightness": value})
            await app.session.dispatch_event(app.page._i, "platform_brightness_change", value)
            assert appearance.dark

    run(scenario())


@pytest.mark.parametrize("width", [390, 768, 1440])
def test_real_resource_bridge_uses_initial_page_size_and_later_resize(width: int) -> None:
    async def scenario() -> None:
        app = await render_component(ft.Column)
        app.session.apply_patch(app.page._i, {"width": width, "height": 844})
        resources = build_resources(
            app.page,
            base_url="http://127.0.0.1:8125",
            get_token=lambda: "synthetic",
            host_id="home-mac",
        )
        try:
            assert resources.bridge.state.width == width
            assert resources.bridge.state.is_wide == (width >= 1100)
            assert resources.props["appearance"].dark
            for next_width in (1440, 390, 1100):
                app.session.apply_patch(app.page._i, {"width": next_width, "height": 1000})
                await app.session.dispatch_event(
                    app.page._i, "resize", {"width": next_width, "height": 1000}
                )
                assert resources.bridge.state.width == next_width
                assert resources.bridge.state.is_wide == (next_width >= 1100)
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1440])
def test_mock_count_tiles_fit_default_phone_and_desktop_content(width: int) -> None:
    async def scenario() -> None:
        app = await _render_shell(_state(), viewport_width=width, dark=True)
        sidebar = app.find("inventory-sidebar")
        assert sidebar.padding == 0
        assert sidebar.bgcolor == ft.Colors.TRANSPARENT
        counts = app.find("console-counts")
        available = 380 if width >= 1100 else width - 32
        assert sum(tile.width for tile in counts.controls) + 20 == pytest.approx(available)
        assert app.find("rail-slot").visible == (width >= 600)
        assert app.find("phone-nav-slot").visible == (width < 600)

    run(scenario())
