"""A status change updates a console card in place and never moves it."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.components.lists import (
    LOADING_BAR_DELAY_S,
    AgentsList,
)
from relaydeck.client.features.inventory.selectors import AgentRow
from relaydeck.domain.models import Agent, AgentStatus, AppState, scope_key
from tests.support.host_harness import render_component, run
from tests.support.ui.navigation import _noop, _state

COUNT = 8


def _with_agents(working: str = "") -> AppState:
    base = _state()
    agents = {
        scope_key("home-mac", f"term-{i}"): Agent(
            terminal_id=f"term-{i}",
            host_id="home-mac",
            workspace_id="w1",
            tab_id="tab-2",
            pane_id=f"p{i}",
            name=f"agent-{i:02d}",
            agent="tool-a",
            status=AgentStatus.WORKING if f"term-{i}" == working else AgentStatus.DONE,
            focused=False,
        )
        for i in range(COUNT)
    }
    return replace(base, inventory=replace(base.inventory, agents=agents))


def test_a_status_change_patches_the_card_where_it_is() -> None:
    setter: list[Any] = []

    @ft.component
    def Parent() -> Any:
        state, set_state = ft.use_state(_with_agents())
        setter[:] = [set_state]
        return AppShell(
            state=state,
            is_wide=True,
            destination="agents",
            on_destination=_noop,
            on_select_agent=_noop,
            on_select_shell=_noop,
            on_select_host=_noop,
            on_banner_action=_noop,
            on_open_terminal=_noop,
            on_start_agent=_noop,
            on_new_terminal=_noop,
            on_back=_noop,
        )

    def cards(app: Any) -> list[tuple[str, int]]:
        tiles = app.find("agents-tiles").controls
        return [(node.key, node._i) for node in tiles]

    async def scenario() -> None:
        app = await render_component(Parent)
        await app.settle()
        before = cards(app)
        assert len(before) == COUNT
        setter[0](_with_agents(working="term-5"))
        await app.settle()
        assert cards(app) == before  # same order, same controls
        tile = app.find("agent-home-mac:term-5")
        assert "Working" in str(tile.tooltip)

    run(scenario())


def test_the_loading_bar_overlays_the_list_and_waits_out_quick_loads() -> None:
    async def scenario() -> None:
        row = AgentRow("home-mac:t1", "agent", "", AgentStatus.DONE, focused=False)
        layouts = []
        for busy in (False, True):
            app = await render_component(
                AgentsList,
                rows=(row,),
                selected="",
                on_select=_noop,
                on_start_agent=_noop,
                busy=busy,
            )
            column = app.find("agents-list-bounds")
            layouts.append([getattr(control, "key", None) for control in column.controls])
            bar = app.find("inventory-page-loading")
            assert bar.visible is False  # a quick load never shows it
            if busy:
                await asyncio.sleep(LOADING_BAR_DELAY_S + 0.1)
                await app.settle()
                assert app.find("inventory-page-loading").visible is True
        assert layouts[0] == layouts[1]  # the bar takes no space: nothing shifts

    run(scenario())
