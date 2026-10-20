"""the phone and iPad Console gives the agents the room, and stays honest.

Real AppShell render with synthetic state (the same worlds the other Console tests use). Each test
names the problem it guards.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.client.features.usage import screen_model as sm
from relaydeck.client.features.usage.components.cards import console_card
from tests.support.host_harness import render_component, run
from tests.support.ui.find_optional import chips_with, find_or_none
from tests.support.ui.navigation import _state
from tests.support.ui.spaces_filter import _spaces_state


async def _render(state: Any, *, width: int = 390, refresh: Any = None) -> Any:
    @ft.component
    def Parent() -> Any:
        prefs, set_prefs = ft.use_state(TerminalPrefs(space_key=""))
        return AppShell(
            state=state,
            is_wide=width >= 1100,
            viewport_width=width,
            destination="agents",
            on_destination=lambda value: None,
            on_select_agent=lambda value: None,
            on_select_shell=lambda value: None,
            on_select_host=lambda value: None,
            on_banner_action=lambda: None,
            on_open_terminal=lambda: None,
            on_start_agent=lambda: None,
            on_new_terminal=lambda: None,
            prefs=prefs,
            on_prefs=set_prefs,
            on_inventory_filters=lambda value: None,
            on_inventory_refresh=refresh,
            dark=True,
        )

    return await render_component(Parent)


# -- the host row: gone with one host ----------------------------------------------------------


def test_one_host_shows_no_host_row_on_the_phone() -> None:
    async def scenario() -> None:
        app = await _render(_state(), width=390)
        assert find_or_none(app, "host-chip-home-mac") is None
        assert find_or_none(app, "console-hosts") is None
        assert app.find("console-filter-chips") is not None  # the spaces still filter

    run(scenario())


def test_several_hosts_share_one_scrollable_row_with_the_spaces() -> None:
    async def scenario() -> None:
        app = await _render(_spaces_state(), width=390)
        row = app.find("console-filter-chips")
        assert row.scroll == ft.ScrollMode.AUTO and not row.wrap
        keys = [key for key in (getattr(item, "key", None) for item in row.controls) if key]
        assert "host-chip-home-mac" in keys or any(
            "host-chip-home-mac" == getattr(getattr(item, "content", None), "key", None)
            for item in row.controls
        )
        assert chips_with(row, "host-chip-space-"), "the space chips are in the same row"
        assert find_or_none(app, "console-hosts") is None  # no second row of host chips

    run(scenario())


# -- chip tap target: 44 px or more, a compact visible pill --------------------------------------


def test_chip_tap_target_is_at_least_44_with_a_34_px_pill() -> None:
    async def scenario() -> None:
        app = await _render(_spaces_state(), width=390)
        chip = app.find("host-chip-space-home-mac:w1")
        assert chip.height >= T.MIN_TOUCH >= 44
        assert chip.content.height == 34  # the drawn pill

    run(scenario())


# -- refresh: an icon in the header, not a button at the bottom ---------------------------------


def test_refresh_is_an_icon_in_the_header_and_not_at_the_bottom() -> None:
    async def scenario() -> None:
        app = await _render(_state(), width=390, refresh=lambda: None)
        header_refresh = app.find("console-refresh")
        assert header_refresh.tooltip == "Refresh agents"
        assert find_or_none(app, "inventory-refresh") is None or (
            find_or_none(app, "inventory-refresh").visible is False
        )

    run(scenario())


def test_the_header_shows_the_count_as_small_inline_text() -> None:
    async def scenario() -> None:
        app = await _render(_state(), width=390)
        count = app.find("agents-count")
        assert count.value == "1 agents"
        assert count.size <= 12
        assert count.color == T.TEXT_MUTED

    run(scenario())


# -- content never renders under the header --------------------------------------------------------


def test_the_agents_pane_stacks_its_header_above_the_list_with_no_overlay() -> None:
    async def scenario() -> None:
        app = await _render(_state(), width=390)
        pane = app.find("agents-pane")
        overlays = [c for c in iter_controls([pane]) if isinstance(c, ft.Stack)]
        # Only the list area stacks (its loading bar); nothing can sit under the header.
        assert [c.key for c in overlays] == ["agents-list-area"]
        header = app.find("console-header")
        chips = app.find("console-filter-chips")
        assert not {header, chips} & set(iter_controls([overlays[0]]))
        agents = app.find("agents-list")
        flow = [c for c in iter_controls([pane]) if c in (header, chips, agents)]
        assert flow.index(header) < flow.index(chips) < flow.index(agents)

    run(scenario())


# -- the usage strip: two lines, Partial as a badge, honest unavailable and capacity -----------

_TOKENS = {
    "text": T.TEXT,
    "text_muted": T.TEXT_MUTED,
    "warn": "#FFB020",
    "surface": T.CARD,
    "border": T.BORDER,
}


def _strip(model: sm.ConsoleCardModel, wide: bool = False) -> ft.Control:
    return console_card(model, tokens=_TOKENS, on_open=lambda: None, wide=wide)


def _container(model: sm.ConsoleCardModel, wide: bool = False) -> ft.Container:
    """The strip's own container, inside Semantics and the full-width Row."""
    found = [
        c
        for c in iter_controls([_strip(model, wide)])
        if getattr(c, "key", None) == "console-usage-card"
    ]
    assert len(found) == 1
    assert isinstance(found[0], ft.Container)
    return found[0]


def _controls(root: Any) -> list[Any]:
    return list(iter_controls([root]))


def test_the_strip_is_two_lines_with_a_partial_badge_and_the_snapshot_age_in_the_tooltip() -> None:
    model = sm.ConsoleCardModel(
        available=True,
        capacity=False,
        headline="Today $149.02 · 1.07B tokens",
        pick="Agent A 48% weekly · resets Sun",
        age="snapshot just now",
        stale_day=False,
        partial=True,
    )
    card = _strip(model)
    strip = _container(model)
    assert strip.key == "console-usage-card"
    assert len(strip.content.controls) == 2  # line 1 and line 2 only
    assert any(getattr(c, "key", None) == "console-partial" for c in _controls(strip))
    assert "snapshot just now" not in str(strip.content.controls[1].value)
    assert strip.tooltip == "Open Usage · snapshot just now"
    assert strip.expand is True  # full width of the column (inside a Row)
    assert "Agent A 48% weekly" in card.label


def test_a_fully_priced_day_has_no_partial_badge() -> None:
    model = sm.ConsoleCardModel(
        True, False, "Today $2.00 · 1.00M tokens", "Agent A 10% weekly", "", False
    )
    strip = _container(model)
    assert not any(getattr(c, "key", None) == "console-partial" for c in _controls(strip))


def test_unavailable_and_capacity_still_say_so_and_never_read_as_no_usage() -> None:
    unavailable = sm.ConsoleCardModel(
        False, False, "Usage is unavailable right now.", "", "", False
    )
    strip = _container(unavailable)
    headline = next(c for c in _controls(strip) if getattr(c, "key", None) == "console-headline")
    assert headline.value == "Usage is unavailable right now."
    assert headline.color == _TOKENS["warn"]
    capacity = sm.ConsoleCardModel(False, True, "Too much data today · open Usage", "", "", False)
    text = _container(capacity).content.controls[0].controls[0].value
    assert "Too much data today" in text
    assert "No usage" not in text
