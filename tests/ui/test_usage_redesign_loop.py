"""Polling eligibility is separate from
display.
A failed refresh keeps the same context's last page with its error. An inactive view stops polling
and keeps its own page. A host change clears data even when its fetch fails. An obsolete response is
dropped. One manual refresh is in flight at a time. Real Flet hooks through the host harness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import Any

import flet as ft

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.usage import loop
from relaydeck.client.features.usage.api import UsageFetchError, UsageQuery
from relaydeck.client.features.usage.panel import UsagePanel
from tests.support.host_harness import render_component, run


def doc(host: str, name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host_id": host,
        "generated_ms": int(time.time() * 1000),
        "timezone": "UTC",
        "price_table_date": None,
        "next_cursor": None,
        "rows": [],
        "limits": [],
        "coverage": [],
        "snapshot_labels": [],
        "kinds": [
            {
                "kind": f"kind-{host}",
                "display_name": name,
                "tokens_reported": True,
                "limits_setup": None,
            }
        ],
        "workspaces": [],
    }


class Server:
    """Synthetic fetch: per-host answers, optional blocking or failure, and a call log."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}

    async def __call__(self, query: UsageQuery) -> Mapping[str, object]:
        self.calls.append(query.host_id)
        if query.host_id in self.gates:
            await self.gates[query.host_id].wait()
        if query.host_id in self.fail:
            raise UsageFetchError("malformed_page")  # a definitive error: unavailable at once
        return doc(query.host_id, "Alpha CLI" if query.host_id == "host-a" else "Beta CLI")


@ft.component
def Harness(fetch: Any) -> Any:
    host, set_host = ft.use_state("host-a")
    active, set_active = ft.use_state(True)
    return ft.Column(
        controls=[
            UsagePanel(
                fetch=fetch, identity="acct-a", host_id=host, active=active, dark=False, wide=False
            ),
            ft.Button("Host B", key="go-host-b", on_click=lambda _e: set_host("host-b")),
            ft.Button("Inactive", key="go-inactive", on_click=lambda _e: set_active(False)),
            ft.Button("Active", key="go-active", on_click=lambda _e: set_active(True)),
        ]
    )


def _texts(app: Any) -> list[str]:
    return [
        str(c.value) for c in iter_controls([app.page.views[0].controls]) if isinstance(c, ft.Text)
    ]


async def _until(app: Any, check: Callable[[], bool]) -> None:
    async with asyncio.timeout(4):
        while True:
            await app.settle()
            if check():
                return
            await asyncio.sleep(0.01)


def test_a_failed_refresh_keeps_the_same_context_page_marked_with_its_error() -> None:
    async def scenario() -> None:
        server = Server()
        app = await render_component(Harness, fetch=server)
        await _until(app, lambda: "Alpha CLI" in _texts(app))
        await app.set_props_and_fire("go-inactive", {}, "click")
        server.fail.add("host-a")  # the resume refresh fails
        await app.set_props_and_fire("go-active", {}, "click")
        await _until(app, lambda: "offline · last updated" in " ".join(_texts(app)))
        assert "Alpha CLI" in _texts(app)  # the same context's data is kept, marked with its error

    run(scenario())


def test_an_inactive_view_stops_polling_and_keeps_only_its_own_page() -> None:
    async def scenario() -> None:
        server = Server()
        app = await render_component(Harness, fetch=server)
        await _until(app, lambda: "Alpha CLI" in _texts(app))
        await app.set_props_and_fire("go-inactive", {}, "click")
        await app.settle()
        calls = len(server.calls)
        await asyncio.sleep(0.05)
        await app.settle()
        assert len(server.calls) == calls  # no fetch while inactive
        assert "Alpha CLI" in _texts(app)

    run(scenario())


def test_a_host_change_clears_the_page_even_when_its_fetch_fails() -> None:
    async def scenario() -> None:
        server = Server()
        app = await render_component(Harness, fetch=server)
        await _until(app, lambda: "Alpha CLI" in _texts(app))
        server.fail.add("host-b")
        await app.set_props_and_fire("go-host-b", {}, "click")
        await _until(app, lambda: "Usage is unavailable right now." in _texts(app))
        assert "Alpha CLI" not in _texts(app)

    run(scenario())


def test_an_obsolete_response_is_dropped_after_the_host_changes() -> None:
    async def scenario() -> None:
        server = Server()
        server.gates["host-a"] = asyncio.Event()  # host A answers late
        app = await render_component(Harness, fetch=server)
        await app.set_props_and_fire("go-host-b", {}, "click")
        await _until(app, lambda: "Beta CLI" in _texts(app))
        server.gates["host-a"].set()
        await app.settle()
        await app.settle()
        assert "Beta CLI" in _texts(app) and "Alpha CLI" not in _texts(app)

    run(scenario())


def test_one_manual_refresh_is_in_flight_and_a_press_while_running_is_refused() -> None:
    gate = loop.RefreshGate()
    assert gate.try_start() is True
    assert gate.running and gate.try_start() is False
    gate.finish()
    assert gate.try_start() is True
