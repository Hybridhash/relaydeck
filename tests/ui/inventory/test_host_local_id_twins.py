"""Host-local id twins across h1/h2 through the shipped root.

Repeated workspace/tab/pane/agent ids must coexist: both twins list,
selection/detail/routes carry scoped keys, back navigation restores the
list, and losing h2's grant keeps h1's twins selectable. Driven with
real Flet dispatched events over a scripted two-host transport.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import flet as ft

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import GatewayTransport
from relaydeck.domain.models import scope_key
from tests.support.host_harness import HostApp, render_component, run
from tests.support.metadata_page import snapshot_page
from tests.support.scripted_sessions import make_scripted_sessions

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "gateway"

AGENT_BARE = "term-1"  # fixture bare ids, repeated on every host
SHELL_TAB_BARE = "w1:t2"


def _snapshot(host_id: str) -> dict[str, Any]:
    """Fixture-shaped doc (passes gateway schema validation) with the
    SAME bare ids on every host; only labels/names differ per host."""
    import json

    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["host"]["id"] = host_id
    doc["host"]["epoch"] = f"{host_id}-e1"
    doc["cursor"] = f"c-{host_id}"
    for item in doc["workspaces"]:
        item["host_id"] = host_id
        item["label"] = f"ws-{host_id}"
    for item in doc["tabs"]:
        item["host_id"] = host_id
        item["label"] = f"tab-{host_id}" if item["id"] == "w1:t1" else f"shell-{host_id}"
    for item in doc["panes"]:
        item["host_id"] = host_id
    for item in doc["agents"]:
        item["host_id"] = host_id
        item["name"] = f"agent-{host_id}"
        item["title"] = f"agent-{host_id} | demo"
    return doc


def _host_entry(host_id: str) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "label": host_id,
        "epoch": f"{host_id}-e1",
        "reachable": True,
        "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
    }


class _TwinTransport(GatewayTransport):
    def __init__(self) -> None:
        self.runs: list[str] = []
        self.grants = ["h1", "h2"]

    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        return {"hosts": [_host_entry(h) for h in self.grants]}

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        self.runs.append(host_id)
        return _snapshot(host_id)

    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        return snapshot_page(_snapshot(host_id), limit, page_token, project, status, query)

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        for _ in ():
            yield SseFrame("metadata", "{}")

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


async def _render(
    route: str = "/", host_id: str = "h1", width: float = 1280
) -> tuple[HostApp, SyncController, _TwinTransport, RouteBridge, list[str]]:
    transport = _TwinTransport()
    controller = SyncController(transport=transport, backoff=Backoff.seeded(1))
    bridge = RouteBridge(route=route, width=width)
    pushed: list[str] = []
    app = await render_component(
        Root,
        controller=controller,
        transport=transport,
        host_id=host_id,
        bridge=bridge,
        go=pushed.append,
        session_factory=make_scripted_sessions().factory,
        account="tester",
    )
    await app.settle()
    return app, controller, transport, bridge, pushed


def test_twins_coexist_after_host_switch() -> None:
    async def scenario() -> None:
        app, controller, transport, _bridge, _pushed = await _render()
        assert transport.runs == ["h1"]
        assert (
            app.find(f"agent-{scope_key('h1', AGENT_BARE)}").tooltip
            == "Tool-a agent, Working, agent-h1"
        )
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        await app.set_props_and_fire("host-h2", {}, "click")
        await app.settle()
        assert transport.runs == ["h1", "h2"]
        # Both twins coexist in one inventory; the h2 snapshot did not
        # orphan or overwrite the h1 entries.
        assert set(controller.state.inventory.agents) == {
            scope_key("h1", AGENT_BARE),
            scope_key("h2", AGENT_BARE),
        }
        assert set(controller.state.inventory.workspaces) == {"h1:w1", "h2:w1"}
        assert set(controller.state.inventory.panes) == {
            scope_key("h1", "w1:p1"),
            scope_key("h1", "w1:p2"),
            scope_key("h2", "w1:p1"),
            scope_key("h2", "w1:p2"),
        }
        # Per-host counts stay correct.
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        for host_key in ("host-h1", "host-h2"):
            card = app.find(host_key)
            assert any(
                isinstance(c, ft.Text) and c.value == "1 agent · 2 tabs"
                for c in iter_controls([card])
            )
        # Both agent twins list side by side with distinct titles.
        await app.set_props_and_fire("console-rail-console", {}, "click")
        assert (
            app.find(f"agent-{scope_key('h1', AGENT_BARE)}").tooltip
            == "Tool-a agent, Working, agent-h1"
        )
        assert (
            app.find(f"agent-{scope_key('h2', AGENT_BARE)}").tooltip
            == "Tool-a agent, Working, agent-h2"
        )
        # Shell twins (same bare s1) list side by side too.
        await app.set_props_and_fire("console-rail-terminals", {}, "click")
        assert app.find(f"shell-{scope_key('h1', SHELL_TAB_BARE)}").tooltip == "shell-h1"
        assert app.find(f"shell-{scope_key('h2', SHELL_TAB_BARE)}").tooltip == "shell-h2"

    run(scenario())


def test_scoped_selection_detail_and_back() -> None:
    async def scenario() -> None:
        app, controller, _transport, _bridge, pushed = await _render(width=390)
        await app.set_props_and_fire("console-nav-settings", {}, "click")
        await app.set_props_and_fire("host-h2", {}, "click")
        await app.settle()
        await app.set_props_and_fire("console-nav-console", {}, "click")
        await app.set_props_and_fire(f"agent-{scope_key('h2', AGENT_BARE)}", {}, "click")
        await app.settle()
        # Route, domain selection, and detail all name the h2 twin.
        assert pushed[-1] == f"/agents/{scope_key('h2', AGENT_BARE)}"
        selection = controller.state.selection
        assert (selection.host_id, selection.session_key) == ("h2", scope_key("h2", AGENT_BARE))
        # Selecting opens the agent window straight away.
        assert app.find("agent-detail-title").value == "agent-h2"
        # Back returns to the list with both twins intact.
        await app.set_props_and_fire("terminal-back", {}, "click")
        await app.settle()
        assert app.find(f"agent-{scope_key('h1', AGENT_BARE)}") is not None
        assert app.find(f"agent-{scope_key('h2', AGENT_BARE)}") is not None
        assert controller.state.selection.session_key == ""

    run(scenario())


def test_grant_loss_keeps_survivor_selectable() -> None:
    async def scenario() -> None:
        app, controller, transport, _bridge, _pushed = await _render()
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        await app.set_props_and_fire("host-h2", {}, "click")
        await app.settle()
        assert set(controller.state.inventory.agents) == {
            scope_key("h1", AGENT_BARE),
            scope_key("h2", AGENT_BARE),
        }
        # h2 loses its grant: its twins drop, h1's survive.
        transport.grants = ["h1"]
        await app.set_props_and_fire("host-h1", {}, "click")
        await app.settle()
        assert set(controller.state.inventory.agents) == {scope_key("h1", AGENT_BARE)}
        assert set(controller.state.inventory.tabs) == {
            scope_key("h1", "w1:t1"),
            scope_key("h1", SHELL_TAB_BARE),
        }
        await app.set_props_and_fire("console-rail-console", {}, "click")
        assert (
            app.find(f"agent-{scope_key('h1', AGENT_BARE)}").tooltip
            == "Tool-a agent, Working, agent-h1"
        )
        try:
            app.find(f"agent-{scope_key('h2', AGENT_BARE)}")
            missing = False
        except KeyError:
            missing = True
        assert missing
        # The survivor still selects and shows its own detail.
        await app.set_props_and_fire(f"agent-{scope_key('h1', AGENT_BARE)}", {}, "click")
        await app.settle()
        assert controller.state.selection.session_key == scope_key("h1", AGENT_BARE)
        assert app.find("agent-detail-title").value == "agent-h1"

    run(scenario())
