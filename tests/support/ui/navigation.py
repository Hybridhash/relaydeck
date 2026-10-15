"""Inventory state, stub controller/transport and shell/root rendering."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.components import AppShell
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.contracts.catalog import AgentCatalog
from relaydeck.domain import reducers
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
    scope_key,
)
from tests.support.host_harness import HostApp, render_component
from tests.support.scripted_sessions import make_scripted_sessions


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
            agent_catalog=AgentCatalog("ready", 1000, ("tool-a",), source="synthetic"),
        )
    }
    workspaces = {
        "w1": Workspace(id="w1", host_id="home-mac", label="web"),
        "w2": Workspace(id="w2", host_id="home-mac", label="ops"),
    }
    tabs = {
        "tab-1": Tab(
            id="tab-1", host_id="home-mac", workspace_id="w1", label="shell-a", pane_count=1
        ),
        "tab-2": Tab(
            id="tab-2", host_id="home-mac", workspace_id="w1", label="agent-x", pane_count=2
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
        )
    }
    state = AppState(
        connection=Connection(
            state=ConnectionState.LIVE,
            host_id="home-mac",
            cursor="c" * 32,
            last_sequence=3,
        ),
        inventory=Inventory(
            hosts=hosts,
            workspaces={scope_key(value.host_id, value.id): value for value in workspaces.values()},
            tabs={scope_key(value.host_id, value.id): value for value in tabs.values()},
            panes={},
            agents={
                scope_key(value.host_id, value.terminal_id): value for value in agents.values()
            },
        ),
        selection=Selection(),
    )
    if kw:
        state = replace(state, **kw)
    return state


def _noop(*args: Any) -> None:
    pass


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


class _StubController:
    def __init__(self) -> None:
        self.listeners: list[Any] = []
        self.backoff = Backoff.seeded(1)
        self.runs: list[str] = []
        self.state = AppState()

    def subscribe(self, listener: Any) -> None:
        self.listeners.append(listener)

    def unsubscribe(self, listener: Any) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    async def run(self, host_id: str, stop: Any = lambda: False) -> None:
        self.runs.append(host_id)
        while not stop():
            await asyncio.sleep(3600)

    def push(self, state: AppState) -> None:
        self.state = state
        for listener in self.listeners:
            listener(state)

    def apply_grants(self, entries: list[dict[str, Any]]) -> AppState:
        """Mirror `SyncController.apply_grants` (grant ownership)."""
        self.push(reducers.merge_hosts(self.state, entries))
        return self.state

    def accept_snapshot(self, host_id: str, doc: dict[str, Any]) -> AppState:
        from relaydeck.client.features.sync.controller import snapshot_from_wire

        self.push(reducers.apply_snapshot(self.state, snapshot_from_wire(doc)))
        return self.state

    def request_resync(self) -> None:
        pass

    def select(self, host_id: str, session_key: str, terminal_kind: str) -> AppState:
        """Mirror `SyncController.select` (selection ownership)."""
        self.push(reducers.select_session(self.state, host_id, session_key, terminal_kind))
        return self.state


class _StubTransport:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(command)
        return {"command_id": command["command_id"], "status": "accepted"}

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        command = next(command for command in self.submitted if command["command_id"] == command_id)
        agent = command["kind"] == "agent.start"
        return {
            **{
                key: command[key] for key in ("protocol", "command_id", "host_id", "kind", "target")
            },
            "status": "succeeded",
            "result": {
                "type": "agent_started" if agent else "tab_created",
                "agent_id" if agent else "tab_id": "term-1" if agent else "w1:t1",
            },
        }

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        import json
        from pathlib import Path

        doc: dict[str, Any] = json.loads(Path("tests/fixtures/gateway/snapshot.json").read_text())
        doc["host"]["id"] = host_id
        doc["host"]["epoch"] = self.submitted[-1]["host_epoch"]
        return doc


async def _render_root(
    state: AppState, route: str = "/", width: float = 1280
) -> tuple[HostApp, _StubController, _StubTransport, RouteBridge, list[str]]:
    controller = _StubController()
    transport = _StubTransport()
    bridge = RouteBridge(route=route, width=width)
    pushed: list[str] = []
    app = await render_component(
        Root,
        controller=controller,
        transport=transport,
        host_id="home-mac",
        bridge=bridge,
        go=pushed.append,
        session_factory=make_scripted_sessions().factory,
        account="tester",
    )
    controller.push(state)
    await app.settle()
    return app, controller, transport, bridge, pushed
