"""Rename publication through source, gateway storage/followers and mounted Roots."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx

from relaydeck.client.app import Root
from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.sync.controller import SyncController, snapshot_from_wire
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from relaydeck.connector.fixture import FixtureAdapter
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.link import ConnectorLink, LinkConfig
from relaydeck.connector.source import HerdrSource
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, ConnectionState, scope_key
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.production import JournalBackend
from relaydeck.journal import CommandJournal
from tests.support.creation.command_journal import NOW, _dispatch_msg
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.sync.scripted_transport import _ScriptedTransport
from tests.support.ui.navigation import _StubController


def test_rename_publishes_fresh_names_and_updates_two_subscribed_roots(tmp_path: Path) -> None:
    class Adapter(FixtureAdapter):
        snapshot_reads = 0

        async def session_snapshot(self) -> dict[str, Any]:
            self.snapshot_reads += 1
            return {
                "snapshot": {
                    "protocol": 22,
                    "workspaces": [{"workspace_id": "w1", "label": "Workspace"}],
                    "tabs": [{"tab_id": key, **row} for key, row in self._tabs.items()],
                    "panes": [{"pane_id": key, **row} for key, row in self._panes.items()],
                }
            }

        async def agent_rename(self, target: str, name: str) -> dict[str, Any]:
            agent = next(row for row in self._agents.values() if row["pane_id"] == target)
            return await super().agent_rename(agent["terminal_id"], name)

    class Source(HerdrSource):
        async def collect_live(self, timeout: float) -> list[dict[str, Any]]:
            event = self._consume_live_line(
                "tab.renamed",
                {"tab": {"tab_id": tab_id, "label": "new-name", "workspace_id": "w1"}},
            )
            assert event is not None
            return [event]

    async def scenario() -> None:
        nonlocal tab_id
        adapter = Adapter()
        tab = await adapter.tab_create("w1", label="8")
        tab_id = tab["tab"]["tab_id"]
        started = await adapter.agent_start("8", "tool-a", tab["root_pane"]["pane_id"])
        await adapter.agent_rename(tab["root_pane"]["pane_id"], None)
        terminal_id = started["agent"]["terminal_id"]
        key = scope_key("home-mac", terminal_id)
        source = Source(cast(HerdrAdapter, adapter))
        source._snapshot = {}
        await source.refresh()
        backend = JournalBackend(CommandJournal(tmp_path / "gateway.db", clock=lambda: NOW))
        gateway = GatewayState(backend=backend, cursor_secret=b"x" * 32, now_ms=lambda: NOW)
        backend.ingest_snapshot("home-mac", source.snapshot_document("home-mac"), NOW, b"x" * 32)
        message = _dispatch_msg(
            "rename-subscribed", "agent.rename", {"agent_id": terminal_id}, {"name": "new-name"}
        )
        message["host_epoch"] = source.epoch
        changed = asyncio.Event()

        class Tunnel(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                await super().send_text(text)
                doc = json.loads(text)
                if doc["kind"] == "snapshot":
                    assert backend.ingest_snapshot("home-mac", doc["snapshot"], NOW, b"x" * 32) == {
                        "ok": True
                    }
                elif doc["kind"] == "event":
                    assert "ok" in backend.ingest_event("home-mac", doc["event"], NOW, b"x" * 32)

        tunnel = Tunnel([json.dumps(message)])
        link = ConnectorLink(
            LinkConfig(
                gateway_url="https://gateway.invalid",
                host_id="home-mac",
                host_token="synthetic",
                journal_path=str(tmp_path / "connector.db"),
                ping_interval_s=0,
            ),
            source,
        )
        followers = []
        clients = []
        roots = []
        sessions = []
        try:
            for width in (390, 1280):
                access, _ = gateway.sessions.create(
                    "synthetic",
                    frozenset({"sessions.read"}),
                    frozenset({"home-mac"}),
                    NOW,
                    f"device-{width}",
                )
                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(gateway)))
                clients.append(client)

                async def token(access: str = access) -> str:
                    return access

                transport = HttpxGatewayTransport("https://gateway.invalid", token, client)
                state = reducers.apply_snapshot(
                    AppState(), snapshot_from_wire(backend.snapshot("home-mac"))
                )
                state = replace(
                    state, connection=replace(state.connection, state=ConnectionState.LIVE)
                )
                sync = SyncController(transport, state=state)
                controller = _StubController()
                sync.subscribe(controller.push)
                factory = make_scripted_sessions()
                sessions.append(factory)
                app = await render_component(
                    Root,
                    controller=controller,
                    transport=transport,
                    host_id="home-mac",
                    bridge=RouteBridge(route="/agents", width=width),
                    go=lambda route: None,
                    session_factory=factory.factory,
                    account="synthetic",
                )
                controller.push(state)
                await app.settle()
                tile = app.find(f"agent-{key}")
                assert any(getattr(node, "value", None) == "8" for node in iter_controls([tile]))
                roots.append((app, tile._i))

                async def follow(sync: SyncController = sync) -> None:
                    async for event in backend.follow("home-mac", 0):
                        if event is not None:
                            sync._apply_frame(SseFrame("metadata", json.dumps(event)))
                            changed.set()

                followers.append(asyncio.create_task(follow()))
            await asyncio.sleep(0)
            assert len(backend._followers["home-mac"]) == 2
            assert await link._drain_once(tunnel, asyncio.Event()) == "once-done"
            assert adapter.snapshot_reads == 2
            snapshot = backend.snapshot("home-mac")
            assert snapshot["tabs"][0]["label"] == "new-name"
            assert not snapshot["agents"][0]["name"]
            assert sum(json.loads(text)["kind"] == "snapshot" for text in tunnel.sent) == 1
            await link._push_live(tunnel, outputs=False)
            await asyncio.wait_for(changed.wait(), 1)
            for app, identifier in roots:
                await app.settle()
                tile = app.find(f"agent-{key}")
                assert tile._i == identifier
                assert any(
                    getattr(node, "value", None) == "new-name" for node in iter_controls([tile])
                )
            assert all(not item.factory.created and not item.streams.opened for item in sessions)
        finally:
            for task in followers:
                task.cancel()
            await asyncio.gather(*followers, return_exceptions=True)
            for client in clients:
                await client.aclose()
            await link.close()
            backend.close()

    tab_id = ""
    run(scenario())
