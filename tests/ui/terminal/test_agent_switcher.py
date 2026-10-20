"""Phone header agent switcher: title button, overlay list and the inert header hook-up."""

from __future__ import annotations

from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.surfaces.agent_header import build_agent_header
from relaydeck.client.features.terminal.surfaces.agent_switcher import (
    SwitcherList,
    SwitcherTitle,
    row_key,
)
from relaydeck.client.features.terminal.switcher import SwitcherRow, switcher_rows, working_count
from tests.support.host_harness import HostApp, render_component, run
from tests.support.ui.spaces_filter import _spaces_state

CURRENT = "home-mac:w1"


def _rows() -> Any:
    return switcher_rows(_spaces_state(), "home-mac", CURRENT, {})


def _texts(app: HostApp) -> list[str]:
    nodes = iter_controls([app.page.views[0].controls])
    return [str(c.value) for c in nodes if isinstance(c, ft.Text) and c.value]


def _semantics(app: HostApp, key: str) -> Any:
    """The Semantics wrapper whose content carries `key`."""
    for node in iter_controls([app.page.views[0].controls]):
        if isinstance(node, ft.Semantics) and getattr(node.content, "key", None) == key:
            return node
    raise KeyError(key)


def _title_props(working: int, opened: list[int] | None = None) -> dict[str, Any]:
    return {
        "title": "build-helper",
        "working": working,
        "open": False,
        "on_open": (lambda: opened.append(1)) if opened is not None else (lambda: None),
    }


def test_title_shows_the_working_badge_and_the_caret() -> None:
    async def scenario() -> None:
        app = await render_component(SwitcherTitle, **_title_props(3))
        assert app.find("agent-switcher-caret").icon == ft.Icons.ARROW_DROP_DOWN
        assert app.find("agent-detail-title").value == "build-helper"
        badge = app.find("agent-switcher-badge")
        assert badge.content.value == "● 3 working" and badge.width is None
        semantics = _semantics(app, "agent-switcher-open")
        assert semantics.label == "build-helper, switch agent, 3 working"
        assert semantics.button and semantics.expanded is False

    run(scenario())


def test_title_has_no_badge_at_zero_and_opens_on_tap() -> None:
    async def scenario() -> None:
        opened: list[int] = []
        app = await render_component(SwitcherTitle, **_title_props(0, opened))
        assert app.find("agent-switcher-caret")
        with pytest.raises(KeyError):
            app.find("agent-switcher-badge")
        button = app.find("agent-switcher-open")
        assert button.height == 28 and button.expand
        await app.set_props_and_fire("agent-switcher-open", {}, "click")
        assert opened == [1]

    run(scenario())


def _list_props(rows: Any, picked: list[str], closed: list[int]) -> dict[str, Any]:
    return {
        "rows": rows,
        "on_pick": picked.append,
        "on_close": lambda: closed.append(1),
        "top": 40,
    }


def test_list_renders_sections_and_rows_in_selector_order() -> None:
    async def scenario() -> None:
        rows = _rows()
        app = await render_component(SwitcherList, **_list_props(rows, [], []))
        texts = _texts(app)
        assert f"Working · {working_count(rows)}" in texts and "Others" in texts
        assert texts.index(f"Working · {working_count(rows)}") < texts.index("Others")
        keys = [
            c.key
            for c in iter_controls([app.page.views[0].controls])
            if str(getattr(c, "key", "")).startswith("agent-switcher-row-")
        ]
        assert keys == [row_key(row.key) for row in rows]
        card = app.find("agent-switcher-list")
        assert (card.top, card.left, card.right) == (40, 4, 4)
        assert card.border_radius == ft.BorderRadius.all(12)
        assert _semantics(app, "agent-switcher-row-home-mac:w1")

    run(scenario())


def test_tapping_a_row_picks_once_and_the_scrim_closes() -> None:
    async def scenario() -> None:
        picked: list[str] = []
        closed: list[int] = []
        app = await render_component(SwitcherList, **_list_props(_rows(), picked, closed))
        await app.set_props_and_fire(row_key("home-mac:d2"), {}, "click")
        assert picked == ["home-mac:d2"] and closed == []
        await app.set_props_and_fire("agent-switcher-scrim", {}, "tap")
        assert closed == [1]

    run(scenario())


def test_current_row_is_marked_in_its_label() -> None:
    async def scenario() -> None:
        app = await render_component(SwitcherList, **_list_props(_rows(), [], []))
        current = _semantics(app, row_key(CURRENT)).label
        other = _semantics(app, row_key("home-mac:d2")).label
        assert current.endswith("current") and "Atlas" in current and "working" in current
        assert "current" not in other

    run(scenario())


def test_single_agent_shows_the_empty_line() -> None:
    async def scenario() -> None:
        rows = switcher_rows(_spaces_state(), "lab-mac", "lab-mac:lab-b", {})
        app = await render_component(SwitcherList, **_list_props(rows, [], []))
        assert "No other agents on this host" in _texts(app)
        many = await render_component(SwitcherList, **_list_props(_rows(), [], []))
        assert "No other agents on this host" not in _texts(many)

    run(scenario())


def _header(**extra: Any) -> Any:
    return build_agent_header(
        agent_status="Working",
        agent_title="build-helper",
        attach_enabled=False,
        connection_live=True,
        connection_text="Live",
        host_line="home-mac",
        on_attach_picture=lambda: None,
        on_back=lambda: None,
        on_connection_action=None,
        on_disconnect=None,
        on_open_shell=None,
        on_operations=None,
        phone=True,
        picture_action=None,
        slim=True,
        terminal=TerminalViewState(),
        window="viewing",
        window_size=(80, 24),
        workflow="agent-composer",
        **extra,
    )


def _keys(app: HostApp) -> list[str]:
    return [
        str(c.key) for c in iter_controls([app.page.views[0].controls]) if getattr(c, "key", None)
    ]


def test_header_without_the_prop_renders_as_before_and_with_it_swaps_the_title() -> None:
    async def scenario() -> None:
        @ft.component
        def Plain() -> Any:
            return _header()

        @ft.component
        def Omitted() -> Any:
            return _header(switcher=None)

        @ft.component
        def Switched() -> Any:
            return _header(
                switcher=(
                    (SwitcherRow("h:t1", "build-helper", "web", "working", True),),
                    False,
                    lambda _name: None,
                ),
            )

        plain = await render_component(Plain)
        omitted = await render_component(Omitted)
        assert _keys(plain) == _keys(omitted)
        assert "agent-switcher-open" not in _keys(plain)
        switched = await render_component(Switched)
        keys = _keys(switched)
        assert "agent-switcher-open" in keys and keys.count("agent-detail-title") == 1
        assert "terminal-back" in keys and "agent-more" in keys

    run(scenario())
