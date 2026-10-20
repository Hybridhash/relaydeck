"""Host switching through the shipped root.

Selecting a second host must switch the authoritative sync source
(controller.run follows the selection), clean up the previous
subscription, and preserve the first host's synced inventory slice.
Driven with real Flet dispatched events; the controller is the shipped
SyncController over a scripted two-host transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from relaydeck.client.app import Root
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


def _snapshot(host_id: str, agent: str = "") -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
    doc["host"]["id"] = host_id
    doc["host"]["epoch"] = f"{host_id}-e1"
    for key in ("workspaces", "tabs", "panes"):
        for item in doc[key]:
            item["host_id"] = host_id
    template = dict(doc["agents"][0])
    template["host_id"] = host_id
    if agent:
        template["terminal_id"] = agent
        template["name"] = agent
        doc["agents"] = [template]
    else:
        doc["agents"] = []
    doc["cursor"] = f"c-{host_id}"
    return doc


def _host_entry(host_id: str) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "label": host_id,
        "epoch": f"{host_id}-e1",
        "reachable": True,
        "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
    }


class _TwoHostTransport(GatewayTransport):
    def __init__(self) -> None:
        self.runs: list[str] = []

    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        return {"hosts": [_host_entry("h1"), _host_entry("h2")]}

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        self.runs.append(host_id)
        return _snapshot(host_id, agent="a1" if host_id == "h1" else "")

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
        return snapshot_page(
            _snapshot(host_id, agent="a1" if host_id == "h1" else ""),
            limit,
            page_token,
            project,
            status,
            query,
        )

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        # An idle live stream: one that ended would reconnect and sync the host again.
        await asyncio.Event().wait()
        yield SseFrame("metadata", "{}")

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


class _SpyController(SyncController):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.subscribes = 0
        self.unsubscribes = 0

    def subscribe(self, listener: Any) -> None:
        self.subscribes += 1
        super().subscribe(listener)

    def unsubscribe(self, listener: Any) -> None:
        self.unsubscribes += 1
        super().unsubscribe(listener)


async def _render(
    route: str = "/", host_id: str = "h1"
) -> tuple[HostApp, _SpyController, _TwoHostTransport, RouteBridge]:
    transport = _TwoHostTransport()
    controller = _SpyController(transport=transport, backoff=Backoff.seeded(1))
    bridge = RouteBridge(route=route, width=1280)
    app = await render_component(
        Root,
        controller=controller,
        transport=transport,
        host_id=host_id,
        bridge=bridge,
        go=lambda route: None,
        session_factory=make_scripted_sessions().factory,
        account="tester",
    )
    await app.settle()
    return app, controller, transport, bridge


def test_selecting_second_host_resyncs_and_preserves_first() -> None:
    async def scenario() -> None:
        app, controller, transport, _bridge = await _render()
        assert transport.runs == ["h1"]
        assert controller.state.connection.host_id == "h1"
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        assert app.find("host-h2") is not None
        await app.set_props_and_fire("host-h2", {}, "click")
        await app.settle()
        assert transport.runs == ["h1", "h2"]
        assert controller.subscribes == 2
        assert controller.unsubscribes == 1
        assert controller.state.connection.host_id == "h2"
        assert scope_key("h1", "a1") in controller.state.inventory.agents

    run(scenario())


def test_initial_host_route_syncs_routed_host() -> None:
    """A deep link /hosts/h2 drives authoritative context from the
    first render: no transient sync of the configured default."""

    async def scenario() -> None:
        _app, controller, transport, _bridge = await _render(route="/hosts/h2", host_id="h1")
        assert transport.runs == ["h2"]
        assert controller.state.connection.host_id == "h2"

    run(scenario())


def _missing(app: HostApp, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return True
    return False


def test_narrowed_listing_survives_later_emissions() -> None:
    """Grant ownership: once /v1/hosts drops h1, no later controller
    emission resurrects it (the merge used to be view-local, so any
    _emit() brought revoked rows back)."""

    async def scenario() -> None:
        app, controller, transport, _bridge = await _render()
        await app.set_props_and_fire("console-rail-settings", {}, "click")
        assert app.find("host-h1") is not None

        async def narrowed() -> dict[str, Any]:
            return {"hosts": [_host_entry("h2")]}

        transport.hosts = narrowed  # type: ignore[method-assign]
        await app.set_props_and_fire("host-h2", {}, "click")
        await app.settle()
        assert _missing(app, "host-h1")
        assert set(controller.state.inventory.hosts) == {"h2"}
        assert controller.state.granted_hosts == frozenset({"h2"})
        controller._emit()
        await app.settle()
        assert _missing(app, "host-h1")
        assert set(controller.state.inventory.hosts) == {"h2"}

    run(scenario())


def test_history_navigation_switches_authoritative_host() -> None:
    """Browser back/forward (bridge.set_route) switches the sync host
    exactly like destination clicks do."""

    async def scenario() -> None:
        app, controller, transport, bridge = await _render()
        assert transport.runs == ["h1"]
        bridge.set_route("/hosts/h2")
        await app.settle()
        assert transport.runs == ["h1", "h2"]
        assert controller.state.connection.host_id == "h2"
        bridge.set_route("/hosts/h1")
        await app.settle()
        assert transport.runs == ["h1", "h2", "h1"]
        assert controller.state.connection.host_id == "h1"
        assert controller.subscribes == 3
        assert controller.unsubscribes == 2

    run(scenario())
