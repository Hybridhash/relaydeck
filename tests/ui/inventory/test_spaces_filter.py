"""Visible workspace filters over actual AppShell rows and device preferences."""

from __future__ import annotations

from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.paging import InventoryFilters
from relaydeck.client.features.settings.prefs import TerminalPrefs
from tests.support.host_harness import button_label, render_component, run
from tests.support.ui.find_optional import chips_with
from tests.support.ui.spaces_filter import _counter_values, _spaces_state
from tests.support.ui.window_design import _wrap_violations


async def _render_spaces(
    width: int, destination: str, *, dark: bool = True, initial_space: str = ""
) -> tuple[Any, list[TerminalPrefs], list[InventoryFilters]]:
    changed: list[TerminalPrefs] = []
    filters: list[InventoryFilters] = []

    @ft.component
    def Parent() -> Any:
        prefs, set_prefs = ft.use_state(TerminalPrefs(space_key=initial_space))

        def update(value: TerminalPrefs) -> None:
            changed.append(value)
            set_prefs(value)

        return AppShell(
            state=_spaces_state(),
            is_wide=width >= 1100,
            viewport_width=width,
            destination=destination,
            on_destination=lambda value: None,
            on_select_agent=lambda value: None,
            on_select_shell=lambda value: None,
            on_select_host=lambda value: None,
            on_banner_action=lambda: None,
            on_open_terminal=lambda: None,
            on_start_agent=lambda: None,
            on_new_terminal=lambda: None,
            prefs=prefs,
            on_prefs=update,
            on_inventory_filters=filters.append,
            dark=dark,
        )

    return await render_component(Parent), changed, filters


def _space_items(row: Any, shells: bool) -> list[Any]:
    """Space chips only. the Console's one row also holds the host chips."""
    return list(row.controls) if shells else chips_with(row, "host-chip-space-")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("destination", ["agents", "terminals"])
def test_visible_spaces_filter_rows_counts_and_host_reset(width: int, destination: str) -> None:
    async def scenario() -> None:
        app, changed, filters = await _render_spaces(width, destination)
        baseline_wraps = _wrap_violations(app)
        shells = destination == "terminals"
        row = app.find("shells-spaces" if shells else "console-filter-chips")
        prefix = "shell-space-chip-" if shells else "host-chip-space-"
        assert row.scroll == ft.ScrollMode.AUTO and not row.wrap
        assert all(
            not child.expand and child.content.content.height == 48
            for child in _space_items(row, shells)
        )
        assert [button_label(child) for child in _space_items(row, shells)] == [
            "All spaces",
            "Atlas · 1" if shells else "Atlas · 2",
            "Empty · 0",
            "Lab · 1",
            "litestar-fullstack · 1" if shells else "litestar-fullstack · 2",
        ]
        active = list(
            iter_controls([app.find("shells-pane" if shells else "agents-pane")], active_only=True)
        )
        assert row in active
        await app.set_props_and_fire(f"{prefix}home-mac:w1", {}, "click")
        assert changed[-1].space_key == "home-mac:w1"
        if shells:
            assert [card.key for card in app.find("shells-tiles").controls] == ["shell-home-mac:w1"]
        else:
            assert _counter_values(app) == [1, 1, 0]
            assert [card.key for card in app.find("agents-tiles").controls] == [
                "agent-home-mac:b1",
                "agent-home-mac:w1",
            ]
            assert app.find("agents-count").value == "2 agents"
            assert app.find("inventory-project").value == "home-mac:w1"
            assert filters[-1].project_host == "home-mac" and filters[-1].project_id == "w1"
        await app.set_props_and_fire(
            "shell-host-chip-lab-mac" if shells else "host-chip-lab-mac", {}, "click"
        )
        assert changed[-1].space_key == ""
        row = app.find("shells-spaces" if shells else "console-filter-chips")
        assert [button_label(child) for child in _space_items(row, shells)] == [
            "All spaces",
            "Lab · 1",
        ]
        if not shells:
            assert _counter_values(app) == [1, 0, 0]
            assert app.find("agents-count").value == "1 agents"
            assert filters[-1] == InventoryFilters(host_id="lab-mac")
        await app.set_props_and_fire(f"{prefix}lab-mac:w1", {}, "click")
        assert changed[-1].space_key == "lab-mac:w1"
        await app.set_props_and_fire(f"{prefix}all", {}, "click")
        assert changed[-1].space_key == ""
        assert _wrap_violations(app) == baseline_wraps

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("destination", ["agents", "terminals"])
def test_light_theme_spaces_remain_visible_and_scoped_under_host_chips(
    width: int, destination: str
) -> None:
    async def scenario() -> None:
        app, changed, _filters = await _render_spaces(width, destination, dark=False)
        shells = destination == "terminals"
        row_key = "shells-spaces" if shells else "console-filter-chips"
        pane = app.find("shells-pane-bounds" if shells else "agents-pane-bounds")
        if shells:
            host_key = "shells-hosts"
            assert (
                pane.controls.index(app.find(row_key))
                == pane.controls.index(app.find(host_key)) + 1
            )
        else:
            assert app.find(row_key) in pane.controls  # one merged row, placed in the pane
        assert app.find(row_key) in list(iter_controls([pane], active_only=True))
        await app.set_props_and_fire(
            "shell-space-chip-home-mac:w2" if shells else "host-chip-space-home-mac:w2", {}, "click"
        )
        assert changed[-1].space_key == "home-mac:w2"
        tiles = app.find("shells-tiles" if shells else "agents-tiles")
        assert [node.key for node in tiles.controls] == (
            ["shell-home-mac:w2"] if shells else ["agent-home-mac:d2", "agent-home-mac:w2"]
        )

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("destination", ["agents", "terminals"])
@pytest.mark.parametrize("dark", [True, False])
def test_space_chips_announce_one_selected_choice_after_restore_selection_and_host_reset(
    width: int, destination: str, dark: bool
) -> None:
    async def scenario() -> None:
        app, changed, _filters = await _render_spaces(
            width, destination, dark=dark, initial_space="home-mac:w2"
        )
        shells = destination == "terminals"
        row_key = "shells-spaces" if shells else "console-filter-chips"
        prefix = "shell-space-chip-" if shells else "host-chip-space-"

        def announced(key: str, label: str) -> None:
            row = app.find(row_key)
            assert row.scroll == ft.ScrollMode.AUTO and not row.wrap
            space_items = _space_items(row, shells)
            assert all(isinstance(item, ft.MergeSemantics) for item in space_items)
            annotations = [item.content for item in space_items]
            assert all(isinstance(item, ft.Semantics) for item in annotations)
            active = [item for item in annotations if item.selected is True]
            assert len(active) == 1 and active[0].content.key == key
            assert active[0].label == label
            for item in annotations:
                assert item.selected is not None
                assert item.label.endswith(", selected" if item.selected else ", not selected")
                button = item.content
                # Keep the supported native focus/tap action; do not exclude
                # button semantics to fake a selected accessible label.
                assert not item.exclude_semantics
                assert isinstance(button, ft.TextButton) and button.height == 48
                assert button.on_click is not None
                assert button.content.content.semantics_label == ""
            assert app.find(key)._i == active[0].content._i

        # Device preference restoration uses the same selected-state source.
        announced(
            f"{prefix}home-mac:w2",
            "litestar-fullstack, 1 terminal, selected"
            if shells
            else "litestar-fullstack, 2 agents, selected",
        )
        button_id = app.find(f"{prefix}home-mac:w1")._i
        await app.set_props_and_fire(f"{prefix}home-mac:w1", {}, "click")
        announced(
            f"{prefix}home-mac:w1",
            "Atlas, 1 terminal, selected" if shells else "Atlas, 2 agents, selected",
        )
        assert app.find(f"{prefix}home-mac:w1")._i == button_id
        assert changed[-1].space_key == "home-mac:w1"
        await app.set_props_and_fire(
            "shell-host-chip-lab-mac" if shells else "host-chip-lab-mac", {}, "click"
        )
        announced(f"{prefix}all", "All spaces, selected")
        assert changed[-1].space_key == ""
        await app.set_props_and_fire(f"{prefix}lab-mac:w1", {}, "click")
        announced(
            f"{prefix}lab-mac:w1",
            "Lab, 1 terminal, selected" if shells else "Lab, 1 agent, selected",
        )
        await app.set_props_and_fire(f"{prefix}all", {}, "click")
        announced(f"{prefix}all", "All spaces, selected")

    run(scenario())
