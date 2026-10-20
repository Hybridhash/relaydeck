"""Actual Flet host tree + typed HTTP adapter (no renderer/device claim)."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any, cast

import flet as ft
import httpx
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.inventory import paging
from relaydeck.client.features.inventory.paging import InventoryFilters, InventoryPager
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.inventory.selectors import metadata_rows
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.domain.models import ConnectionState
from relaydeck.gateway.app import create_app
from tests.security.helpers import make_state, mint_token
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.sync.inventory_metadata import _metadata
from tests.support.ui.navigation import _StubController
from tests.support.ui.spaces_filter import _counter_values


def _populate(backend: Any) -> None:
    agents = backend.snapshots["home-mac"]["agents"]
    agents[0]["name"], agents[0]["status"] = "demo", "blocked"
    agents[0]["focused"] = False
    for n in range(1, 63):
        agents.append({**agents[0], "terminal_id": f"term-{n + 1:03}", "name": f"demo-{n:03}"})
    twin = copy.deepcopy(backend.snapshots["home-mac"])
    twin["host"]["id"] = "other-mac"
    for kind in ("agents", "workspaces", "tabs", "panes"):
        for item in twin[kind]:
            item["host_id"] = "other-mac"
    backend.snapshots["other-mac"] = twin


def _mixed_inventory(backend: Any) -> None:
    _populate(backend)
    for host, snapshot in backend.snapshots.items():
        base = snapshot["agents"][0]
        statuses = (["blocked"] * 61 if host == "home-mac" else []) + [
            "working",
            "done",
            "idle",
            "unknown",
        ]
        snapshot["agents"] = [
            {
                **base,
                "terminal_id": f"term-{n:03}",
                "name": f"agent-{n:03}",
                "status": status,
                "focused": status == "idle",
            }
            for n, status in enumerate(statuses)
        ]


def test_console_global_status_groups_across_unseen_host_pages() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _mixed_inventory(backend)
        value = mint_token(state, hosts=frozenset(backend.snapshots))

        async def token() -> str:
            return value

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            pager = InventoryPager(HttpxGatewayTransport("http://gw", token, http), lambda: None)
            hosts = {host: "epoch-7" for host in backend.snapshots}
            await pager.refresh("acct-1", hosts, InventoryFilters())
            assert pager.total == 69 and len(pager.rows) == 54
            assert len(pager.visible_rows) == 50
            assert {row["status"] for row in pager.visible_rows} == {"blocked"}
            # Other-host Working cannot precede Blocked on home-mac's next page.
            await pager.load_more()
            assert not pager.more and len(pager.visible_rows) == 69
            rows = metadata_rows(pager.visible_rows, 0)
            # Shown by name, so a status change never moves a card; paging stays status-grouped.
            assert [(row.title, row.key) for row in rows] == sorted(
                (row.title, row.key) for row in rows
            )
            assert len({row.key for row in rows}) == 69  # same bare IDs, two hosts
            await pager.refresh("acct-1", hosts, InventoryFilters(host_id="other-mac"))
            assert pager.total == 4 and len(pager.visible_rows) == 4
            assert {row["host_id"] for row in pager.visible_rows} == {"other-mac"}
        state.push.close()

    run(scenario())


def test_actual_root_console_withholds_lower_status_until_continuation() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _mixed_inventory(backend)
        controller = _StubController()
        for host, snapshot in backend.snapshots.items():
            controller.accept_snapshot(host, snapshot)
        value = mint_token(state, hosts=frozenset(backend.snapshots))

        async def token() -> str:
            return value

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = HttpxGatewayTransport("http://gw", token, http)
            sessions = make_scripted_sessions()

            @ft.component
            def App() -> Any:
                mounted, set_mounted = ft.use_state(True)
                return ft.Column(
                    controls=[
                        Root(
                            controller=cast(SyncController, controller),
                            transport=transport,
                            host_id="home-mac",
                            bridge=RouteBridge(route="/", width=360),
                            go=lambda route: None,
                            session_factory=sessions.factory,
                            account="acct-1",
                        )
                        if mounted
                        else ft.Text("Closed"),
                        ft.Button(
                            "Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)
                        ),
                    ]
                )

            app = await render_component(App)
            try:
                await app.settle(40)
                assert app.find("agents-count").value == "50 of 69 agents"
                with pytest.raises(KeyError):
                    app.find("agent-other-mac:term-000")
                await app.set_props_and_fire("inventory-more", {}, "click")
                await app.settle(30)
                assert app.find("agents-count").value == "69 of 69 agents"
                assert app.find("agent-other-mac:term-000")
            finally:
                await app.set_props_and_fire("test-unmount", {}, "click")
        state.push.close()

    run(scenario())


def test_console_capacity_keeps_earliest_status_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paging, "MAX_ROWS", 60)

    async def scenario() -> None:
        state, backend = make_state()
        _mixed_inventory(backend)
        value = mint_token(state, hosts=frozenset(backend.snapshots))

        async def token() -> str:
            return value

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            pager = InventoryPager(HttpxGatewayTransport("http://gw", token, http), lambda: None)
            await pager.refresh(
                "acct-1", {h: "epoch-7" for h in backend.snapshots}, InventoryFilters()
            )
            await pager.load_more()
            assert pager.total == 69 and len(pager.visible_rows) == 60
            assert not pager.more and "Narrow the filters" in pager.error
            assert {row["status"] for row in pager.visible_rows} == {"blocked"}
        state.push.close()

    run(scenario())


def test_pager_continuation_survives_read_mark_and_drops_other_account_cache() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _populate(backend)
        current = mint_token(state, hosts=frozenset(backend.snapshots))

        async def token() -> str:
            return current

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            pager = InventoryPager(HttpxGatewayTransport("http://gw", token, http), lambda: None)
            _metadata(backend)
            filters = InventoryFilters(host_id="home-mac")
            await pager.refresh("acct-1", {"home-mac": "epoch-7"}, filters)
            assert len(pager.rows) == 50 and pager.total == 63 and pager.more
            assert pager.rows["home-mac:term-1"]["unread"]
            await pager.mark_read("home-mac:term-1")
            await pager.load_more()
            assert len(pager.rows) == 63 and not pager.more
            assert not pager.rows["home-mac:term-1"]["unread"]
            current = mint_token(state, account="acct-2", hosts=frozenset(backend.snapshots))
            await pager.refresh("acct-2", {"home-mac": "epoch-7"}, filters)
            assert len(pager.rows) == 50 and pager.rows["home-mac:term-1"]["unread"]
            pager.clear()
            assert not pager.rows and not pager.pages and not pager.account
        state.push.close()

    run(scenario())


def test_pager_late_filter_epoch_read_and_offline_results_are_fenced() -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _populate(backend)
        value = mint_token(state, hosts=frozenset(backend.snapshots))
        entered, release, read_entered, read_release = (asyncio.Event() for _ in range(4))

        async def token() -> str:
            return value

        class Delayed(HttpxGatewayTransport):
            offline = False
            wrong_epoch = False

            async def agents(
                self, host_id: str, limit: int = 100, page_token: str = "", **kwargs: Any
            ) -> dict[str, Any]:
                if self.offline:
                    raise TransportError("host_offline", "synthetic offline", True)
                doc = await super().agents(host_id, limit, page_token, **kwargs)
                if kwargs.get("query") == "demo-001":
                    entered.set()
                    await release.wait()
                if self.wrong_epoch:
                    doc["host_epoch"] = "epoch-obsolete"
                return doc

            async def mark_agent_read(self, *args: Any) -> dict[str, Any]:
                doc = await super().mark_agent_read(*args)
                read_entered.set()
                await read_release.wait()
                return doc

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = Delayed("http://gw", token, http)
            pager = InventoryPager(transport, lambda: None)
            hosts = {"home-mac": "epoch-7"}
            old = asyncio.create_task(
                pager.refresh("acct-1", hosts, InventoryFilters(query="demo-001"))
            )
            await asyncio.wait_for(entered.wait(), 1)
            current = InventoryFilters(query="demo-002")
            await pager.refresh("acct-1", hosts, current)
            release.set()
            await old
            assert [row["name"] for row in pager.rows.values()] == ["demo-002"]
            transport.offline = True
            await pager.refresh("acct-1", hosts, current)
            assert len(pager.rows) == 1 and "host_offline" in pager.error
            transport.offline = False
            transport.wrong_epoch = True
            await pager.refresh("acct-1", hosts, InventoryFilters(query="demo-003"))
            assert not pager.rows and "stale_epoch" in pager.error
            transport.wrong_epoch = False
            _metadata(backend)
            await pager.refresh("acct-1", hosts, InventoryFilters())
            pending = asyncio.create_task(pager.mark_read("home-mac:term-1"))
            await asyncio.wait_for(read_entered.wait(), 1)
            _metadata(backend, event_id="ev-activity-new")
            await pager.refresh("acct-1", hosts, InventoryFilters())
            read_release.set()
            await pending
            assert pager.rows["home-mac:term-1"]["unread"]
        state.push.close()

    run(scenario())


@pytest.mark.parametrize("width", [390, 1280])
def test_root_pages_real_adapter_filters_reconnect_and_revoked_row_callback(width: int) -> None:
    async def scenario() -> None:
        state, backend = make_state()
        _populate(backend)
        value = mint_token(state, hosts=frozenset(backend.snapshots))
        controller = _StubController()
        sessions = make_scripted_sessions()
        routes: list[str] = []
        entered: list[dict[str, Any]] = []

        async def token() -> str:
            return value

        class Counting(HttpxGatewayTransport):
            async def agents(
                self, host_id: str, limit: int = 100, page_token: str = "", **kwargs: Any
            ) -> dict[str, Any]:
                entered.append({"host": host_id, **kwargs})
                return await super().agents(host_id, limit, page_token, **kwargs)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(state)), base_url="http://gw"
        ) as http:
            transport = Counting("http://gw", token, http)

            @ft.component
            def Parent() -> Any:
                mounted, set_mounted = ft.use_state(True)
                return ft.Column(
                    controls=[
                        Root(
                            controller=cast(SyncController, controller),
                            transport=transport,
                            host_id="home-mac",
                            bridge=RouteBridge(route="/", width=width),
                            go=routes.append,
                            session_factory=sessions.factory,
                            account="acct-1",
                        )
                        if mounted
                        else ft.Text("Closed"),
                        ft.Button(
                            "Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)
                        ),
                    ]
                )

            app = await render_component(Parent)
            try:
                for host, snapshot in backend.snapshots.items():
                    controller.accept_snapshot(host, snapshot)
                await app.settle(40)
                assert app.find("agents-count").value == "100 of 126 agents"
                assert app.find("agent-home-mac:term-1") and app.find("agent-other-mac:term-1")
                for key in (
                    "inventory-host",
                    "inventory-project",
                    "inventory-status",
                    "inventory-more",
                ):
                    assert app.find(key).height == 48
                await app.set_props_and_fire("inventory-more", {}, "click")
                await app.settle(30)
                assert app.find("agents-count").value == "126 of 126 agents"
                await app.set_props_and_fire(
                    "inventory-host", {"value": "other-mac"}, "select", "other-mac"
                )
                await app.set_props_and_fire(
                    "inventory-status", {"value": "blocked"}, "select", "blocked"
                )
                await app.set_props_and_fire("host-chip-space-other-mac:w1", {}, "click")
                await app.set_props_and_fire(
                    "inventory-query", {"value": "demo-002"}, "change", "demo-002"
                )
                await app.settle(30)
                assert app.find("agents-count").value == "1 of 1 agents"
                assert _counter_values(app) == [1, 0, 0]
                assert entered[-1] == {
                    "host": "other-mac",
                    "project": "w1",
                    "status": "blocked",
                    "query": "demo-002",
                }
                selected = app.find("agent-other-mac:term-003").on_click
                connection = controller.state.connection
                controller.push(
                    replace(
                        controller.state,
                        connection=replace(connection, state=ConnectionState.RECONNECTING),
                    )
                )
                await app.settle()
                controller.push(
                    replace(
                        controller.state, connection=replace(connection, state=ConnectionState.LIVE)
                    )
                )
                await app.settle(30)
                assert app.find("inventory-query").value == "demo-002"
                assert app.find("inventory-project").value == "other-mac:w1"
                await selected(None)
                await app.settle()
                assert controller.runs[-1] == "other-mac" and routes[-1].startswith("/agents/")
                assert sessions.ops.attaches == [] and sessions.submitted == []
                before = list(routes)
                controller.push(replace(controller.state, granted_hosts=frozenset({"home-mac"})))
                await selected(None)
                await app.settle()
                assert routes == before
                assert app.find("agents-count").value == "0 of 0 agents"
                controller.push(replace(controller.state, revoked=True))
                await app.settle()
                assert app.find("agents-count").value == "0 of 0 agents"
                refresh = app.find("inventory-refresh").on_click
            finally:
                await app.set_props_and_fire("test-unmount", {}, "click")
            count = len(entered)
            await selected(None)
            refresh(None)
            await app.settle()
            assert len(entered) == count and routes == before
        state.push.close()

    run(scenario())
