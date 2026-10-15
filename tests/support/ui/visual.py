"""Visual shell rendering helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from relaydeck.client.features.inventory.components import AppShell
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    Connection,
    ConnectionState,
    Host,
    Inventory,
    Selection,
    Tab,
    Workspace,
)
from tests.support.host_harness import HostApp, render_component


def _noop(*args: Any) -> None:
    pass


def _state(**kw: Any) -> AppState:
    hosts = {
        "home-mac": Host(
            id="home-mac",
            label="Home Mac",
            epoch="epoch-7",
            last_seen_ms=1_000,
            reachable=True,
            protocol=22,
            version="0.9.1",
        )
    }
    workspaces = {"w1": Workspace(id="w1", host_id="home-mac", label="web")}
    tabs = {
        "tab-1": Tab(
            id="tab-1", host_id="home-mac", workspace_id="w1", label="shell-a", pane_count=1
        ),
    }
    agents = {
        "term-1": Agent(
            terminal_id="term-1",
            host_id="home-mac",
            workspace_id="w1",
            tab_id="tab-2",
            pane_id="p1",
            name="web",
            agent="tool-a",
            status=AgentStatus.WORKING,
            focused=True,
        ),
        "term-2": Agent(
            terminal_id="term-2",
            host_id="home-mac",
            workspace_id="w1",
            tab_id="tab-2",
            pane_id="p2",
            name="ops",
            agent="tool-a",
            status=AgentStatus.BLOCKED,
        ),
    }
    state = AppState(
        connection=Connection(
            state=ConnectionState.LIVE, host_id="home-mac", cursor="c" * 32, last_sequence=3
        ),
        inventory=Inventory(hosts=hosts, workspaces=workspaces, tabs=tabs, panes={}, agents=agents),
        selection=Selection(host_id="home-mac"),
    )
    if kw:
        state = replace(state, **kw)
    return state


async def _render_shell(state: AppState, **kw: Any) -> HostApp:
    props: dict[str, Any] = {
        "state": state,
        "is_wide": True,
        "destination": "agents",
        "on_destination": _noop,
        "on_select_agent": _noop,
        "on_select_shell": _noop,
        "on_select_host": _noop,
        "on_banner_action": _noop,
        "on_open_terminal": _noop,
        "on_start_agent": _noop,
        "on_new_terminal": _noop,
        "on_back": _noop,
    }
    props.update(kw)
    return await render_component(AppShell, **props)
