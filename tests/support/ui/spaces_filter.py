"""Spaces-filter state and counters."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.domain.models import AgentStatus, AppState, Host, Tab, Workspace, scope_key
from tests.support.ui.navigation import _state


def _spaces_state() -> AppState:
    state = _state()
    original = next(iter(state.inventory.agents.values()))
    agents = {}
    for host, space, name, status in (
        ("home-mac", "w1", "b1", AgentStatus.BLOCKED),
        ("home-mac", "w1", "w1", AgentStatus.WORKING),
        ("home-mac", "w2", "w2", AgentStatus.WORKING),
        ("home-mac", "w2", "d2", AgentStatus.DONE),
        ("lab-mac", "w1", "lab-b", AgentStatus.BLOCKED),
    ):
        agents[scope_key(host, name)] = replace(
            original,
            host_id=host,
            workspace_id=space,
            terminal_id=name,
            tab_id=f"agent-{name}",
            name=name,
            status=status,
        )
    workspaces = {
        scope_key(host, space): Workspace(id=space, host_id=host, label=label)
        for host, space, label in (
            ("home-mac", "w1", "Atlas"),
            ("home-mac", "w2", "litestar-fullstack"),
            ("home-mac", "w3", "Empty"),
            ("lab-mac", "w1", "Lab"),
        )
    }
    tabs = {
        scope_key(host, space): Tab(id=space, host_id=host, workspace_id=space, label=label)
        for host, space, label in (
            ("home-mac", "w1", "deck shell"),
            ("home-mac", "w2", "litestar shell"),
            ("lab-mac", "w1", "lab shell"),
        )
    }
    tabs.update(
        {
            scope_key(agent.host_id, agent.tab_id): Tab(
                id=agent.tab_id, host_id=agent.host_id, workspace_id=agent.workspace_id
            )
            for agent in agents.values()
        }
    )
    return replace(
        state,
        inventory=replace(
            state.inventory,
            agents=agents,
            tabs=tabs,
            workspaces=workspaces,
            hosts={**state.inventory.hosts, "lab-mac": Host(id="lab-mac", label="Lab Mac")},
        ),
    )


def _counter_values(app: Any) -> list[int]:
    return [
        int(node.value)
        for tile in app.find("console-counts").controls
        for node in iter_controls([tile])
        if isinstance(node, ft.Text) and node.value.isdigit()
    ]
