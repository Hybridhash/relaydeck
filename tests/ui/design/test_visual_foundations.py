"""visual foundations through real Flet dispatched renders.

Host-harness renders (real Flet 1.0.3 session, no Flutter): the
CompactRail and HostContextHeader proposal components plus the
reference presentation applied to AppShell (host context, bounded
scrollable lists, themed surfaces). All data is real AppState through
the existing selectors; all intent flows through the existing
callback shapes via dispatched control events.
"""

from __future__ import annotations

from dataclasses import replace

import flet as ft

from relaydeck.client.components import CompactRail, HostContextHeader
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.features.a11y.focus import (
    FOCUS_ORDER,
    check_focus_order,
    focus_order,
    iter_controls,
)
from relaydeck.client.features.inventory.components import DESTINATIONS
from relaydeck.client.theme import DARK, LIGHT, MIN_TOUCH
from relaydeck.domain.models import (
    Selection,
    scope_key,
)
from tests.support.host_harness import render_component, run
from tests.support.ui.find_optional import find_or_none as _find_or_none
from tests.support.ui.visual import _noop, _render_shell, _state


def test_compact_rail_dispatches_existing_callback_shape() -> None:
    async def scenario() -> None:
        selected: list[str] = []
        app = await render_component(
            CompactRail,
            destinations=DESTINATIONS,
            selected="agents",
            on_select=selected.append,
        )
        assert app.find("rd2-rail").width == 96
        assert app.find("rd2-rail-brand-label").value == "R"
        for dest_id, label, _ in DESTINATIONS:
            button = app.find(f"rd2-rail-{dest_id}")
            assert isinstance(button, ft.Button)
            assert app.find(f"rd2-rail-label-{dest_id}").value == label
            assert button.width is not None and button.width >= MIN_TOUCH
            assert button.tooltip == label
            assert button.height is not None and button.height >= MIN_TOUCH
        assert app.find("rd2-rail-agents").bgcolor == DARK["active_row_bg"]
        assert app.find("rd2-rail-terminals").bgcolor is None
        # Real dispatched click (not a direct method call).
        await app.set_props_and_fire("rd2-rail-terminals", {}, "click")
        assert selected == ["terminals"]
        # Keyboard focus order is destination order.
        assert focus_order(app.page) == tuple(f"rd2-rail-{d}" for d, _, _ in DESTINATIONS)

        narrow = await render_component(
            CompactRail,
            destinations=DESTINATIONS,
            selected="settings",  # Hosts is a Settings section
            on_select=_noop,
            compact=True,
            dark=False,
        )
        assert narrow.find("rd2-rail").width == 96
        assert narrow.find("rd2-rail").bgcolor == LIGHT["rail_bg"]
        assert narrow.find("rd2-rail-settings").bgcolor == LIGHT["active_row_bg"]

    run(scenario())


def test_host_context_header_variants() -> None:
    async def scenario() -> None:
        opened: list[bool] = []
        static = await render_component(
            HostContextHeader,
            host_label="Home Mac",
            status_text="Live",
            reachable=True,
        )
        assert static.find("host-context-label").value == "Home Mac"
        assert static.find("host-context-status-text").value == "Live"
        assert static.find("host-context-body").bgcolor == DARK["surface"]
        # Default variant adds no keyboard stops.
        assert focus_order(static.page) == ()

        actionable = await render_component(
            HostContextHeader,
            host_label="Home Mac",
            status_text="Offline",
            reachable=False,
            on_open=lambda: opened.append(True),
            dark=False,
        )
        switch = actionable.find("host-context-switch")
        assert switch.content == "Switch"
        assert switch.height is not None and switch.height >= MIN_TOUCH
        await actionable.set_props_and_fire("host-context-switch", {}, "click")
        assert opened == [True]
        assert focus_order(actionable.page) == ("host-context-switch",)

    run(scenario())


def test_shell_shows_host_context_from_real_state() -> None:
    async def scenario() -> None:
        app = await _render_shell(_state())
        # with one host the Console shows no host chips at all.
        assert _find_or_none(app, "host-chip-home-mac") is None
        assert app.find("agents-pane").bgcolor == ft.Colors.TRANSPARENT
        assert app.find("destinations").bgcolor == DARK["rail_bg"]

        offline_hosts = dict(_state().inventory.hosts)
        offline_hosts["home-mac"] = replace(offline_hosts["home-mac"], reachable=False)
        offline = replace(_state(), inventory=replace(_state().inventory, hosts=offline_hosts))
        offline_app = await _render_shell(offline)
        assert _find_or_none(offline_app, "host-chip-home-mac") is None

        light = await _render_shell(_state(), dark=False)
        assert light.find("agents-pane").bgcolor == LIGHT["surface"]
        assert light.find("host-context-body").bgcolor == LIGHT["surface"]

        narrow = await _render_shell(_state(), is_wide=False)
        assert _find_or_none(narrow, "host-chip-home-mac") is None
        assert narrow.find("console-nav-console").tooltip == "Console"

    run(scenario())


def test_shell_lists_are_bounded_and_active_row_marked() -> None:
    async def scenario() -> None:
        selected = _state(
            selection=Selection(host_id="home-mac", session_key=scope_key("home-mac", "term-1"))
        )
        app = await _render_shell(selected)
        tiles = app.find("agents-tiles")
        assert tiles.scroll == ft.ScrollMode.AUTO
        assert tiles.expand is True
        active = app.find(f"agent-{scope_key('home-mac', 'term-1')}")
        assert active.content.bgcolor == T.CARD_SELECTED and active.content.border.left.width == 6
        assert active.tooltip == "Tool-a agent, Working, web"
        # Real card content and scoped callbacks survive presentation changes.
        assert {"web", "WORKING"} <= {
            control.value for control in iter_controls([active]) if isinstance(control, ft.Text)
        }
        calls: list[str] = []
        wired = await _render_shell(selected, on_select_agent=calls.append)
        await wired.set_props_and_fire(f"agent-{scope_key('home-mac', 'term-2')}", {}, "click")
        assert calls == [scope_key("home-mac", "term-2")]

        shells = await _render_shell(selected, destination="terminals")
        assert shells.find("shells-tiles").scroll == ft.ScrollMode.AUTO
        assert shells.find(f"shell-{scope_key('home-mac', 'tab-1')}").tooltip == "shell-a"
        hosts = await _render_shell(selected, destination="settings")
        assert hosts.find("hosts-tiles").scroll == ft.ScrollMode.AUTO
        host_card = hosts.find("host-home-mac")
        assert any(
            isinstance(c, ft.Text) and c.value == "Home Mac" for c in iter_controls([host_card])
        )

    run(scenario())


def test_shell_explains_empty_lists() -> None:
    async def scenario() -> None:
        base = _state()
        empty = replace(base, inventory=replace(base.inventory, agents={}, tabs={}, panes={}))
        app = await _render_shell(empty)
        assert app.find("agents-count").value == "0 agents"
        assert "No agents" in str(app.find("agents-empty").value)
        terminals = await _render_shell(empty, destination="terminals")
        assert "0 SHELLS ON 0 HOSTS" in {
            c.value
            for c in iter_controls([terminals.find("terminals-header")])
            if isinstance(c, ft.Text)
        }
        assert "No shell" in str(terminals.find("shells-empty").value)

    run(scenario())


def test_shell_focus_order_unchanged() -> None:
    """The current Console and legacy destinations match declared action order."""

    async def scenario() -> None:
        selected = _state(selection=Selection(host_id="home-mac", session_key="term-1"))
        cases = {
            "inventory-agents": await _render_shell(selected),
            "inventory-terminals": await _render_shell(selected, destination="terminals"),
            "inventory-hosts-layout": await _render_shell(_state(), destination="settings"),
            "inventory-agents-narrow": await _render_shell(_state(), is_wide=False),
            "inventory-agents-narrow-detail": await _render_shell(selected, is_wide=False),
        }
        for name, app in cases.items():
            actual = focus_order(app.page)
            assert not check_focus_order(actual, FOCUS_ORDER[name]), f"{name}: {actual!r}"

    run(scenario())
