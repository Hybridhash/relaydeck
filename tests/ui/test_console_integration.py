"""Console presentation on the existing AppShell and real inventory callbacks."""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from tests.support.host_harness import run
from tests.support.ui.find_optional import find_or_none
from tests.support.ui.navigation import _render_shell, _state


def test_console_has_one_create_header_and_narrow_counts_keep_resize_and_footer() -> None:
    async def scenario() -> None:
        created: list[bool] = []
        refreshed: list[bool] = []
        continued: list[bool] = []
        app = await _render_shell(
            _state(),
            viewport_width=1440,
            on_start_agent=lambda: created.append(True),
            on_inventory_refresh=lambda: refreshed.append(True),
            on_inventory_more=lambda: continued.append(True),
            inventory_more=True,
            inventory_total=69,
        )
        active = list(iter_controls([app.find("agents-pane")], active_only=True))
        assert not any(control.key == "sidebar-controls" for control in active)
        assert not any(control.key == "start-agent-open" for control in active)
        assert sum(control.key == "console-header" for control in active) == 1
        assert app.find("agents-count").value == "1 of 69 agents"
        await app.set_props_and_fire("console-new-agent", {}, "click")
        assert created == [True]
        await app.set_props_and_fire("console-search", {}, "click")
        for _ in range(6):
            await app.set_props_and_fire("sidebar-narrow", {}, "click")
        assert app.find("inventory-sidebar").width == 240
        counts = app.find("console-counts")
        assert counts.scroll == ft.ScrollMode.AUTO
        assert [tile.width for tile in counts.controls] == [104, 104, 104]
        assert all(not tile.expand for tile in counts.controls)
        assert app.find("sidebar-narrow").disabled
        assert app.find("sidebar-widen").height >= 48
        await app.set_props_and_fire("sidebar-widen", {}, "click")
        assert app.find("inventory-sidebar").width == 264
        await app.set_props_and_fire("inventory-refresh", {}, "click")
        await app.set_props_and_fire("inventory-more", {}, "click")
        assert refreshed == continued == [True]
        await app.set_props_and_fire("console-collapse-toggle", {}, "click")
        assert app.find("inventory-sidebar").width == 80
        await app.set_props_and_fire("sidebar-show", {}, "click")
        assert app.find("inventory-sidebar").width == 264

    run(scenario())


def test_host_chip_and_search_filter_real_scoped_rows_without_resetting_query() -> None:
    async def scenario() -> None:
        selected: list[Any] = []
        app = await _render_shell(_state(), on_select_agent=selected.append)
        await app.set_props_and_fire("console-search", {}, "click")
        await app.set_props_and_fire("inventory-query", {"value": "web"}, "change", "web")
        # one host means no host row, so the scope is a space chip instead.
        assert find_or_none(app, "host-chip-home-mac") is None
        await app.set_props_and_fire("host-chip-space-home-mac:w1", {}, "click")
        assert app.find("inventory-query").value == "web"
        assert app.find("agents-count").value == "1 agents"
        await app.set_props_and_fire("agent-home-mac:term-1", {}, "click")
        assert selected == ["home-mac:term-1"]
        await app.set_props_and_fire("console-search", {}, "click")
        await app.set_props_and_fire("console-search", {}, "click")
        assert app.find("inventory-query").value == "web"

    run(scenario())


def test_console_column_hides_in_typing_mode_and_keeps_the_collapse_state() -> None:
    async def scenario() -> None:
        for collapse_first in (False, True):
            normal = await _render_shell(_state(), viewport_width=1440)
            if collapse_first:
                await normal.set_props_and_fire("console-collapse-toggle", {}, "click")
            expected = normal.find("inventory-sidebar").visible
            typing = await _render_shell(_state(), viewport_width=1440, typing_split=True)
            assert typing.find("inventory-sidebar").visible is False
            assert not typing.find("sidebar-resize").visible
            assert expected is True  # list (or strip) was showing before typing mode

    run(scenario())
