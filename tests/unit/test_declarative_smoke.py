"""declarative lifecycle on the real 1.0.3 session.

Mount, state update, unrelated-rerender isolation, effect cleanup,
generation guards against late results, and per-session isolation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relaydeck.client.app import AppShell
from tests.support.host_harness import render_component, run


async def _immediate_fetch(query: str) -> tuple[str, ...]:
    await asyncio.sleep(0)
    titles = ("Exposed service", "Outdated dependency")
    return tuple(t for t in titles if query.casefold() in t.casefold())


def test_mount_and_state_update() -> None:
    async def scenario() -> None:
        app = await render_component(AppShell, fetch_inventory=_immediate_fetch)
        assert app.find("destination-title").value == "RelayDeck — Console"
        assert app.find("session-Exposed service").value == "Exposed service"
        assert app.find("session-Outdated dependency").value == "Outdated dependency"
        # Select the Usage destination: prop patch then event, like a real client.
        await app.set_props_and_fire("destinations", {"selected_index": 2}, "change")
        assert app.find("destination-title").value == "RelayDeck — Usage"

    run(scenario())


def test_query_refetch_and_error_state() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def fetch(query: str) -> tuple[str, ...]:
            calls.append(query)
            await asyncio.sleep(0)
            if query == "boom":
                raise RuntimeError("gateway down")
            return (f"item-{query}",) if query else ()

        app = await render_component(AppShell, fetch_inventory=fetch)
        assert calls == [""]
        await app.set_props_and_fire("inventory-query", {"value": "a"}, "change", "a")
        assert app.find("session-item-a").value == "item-a"
        await app.set_props_and_fire("inventory-query", {"value": "boom"}, "change", "boom")
        assert app.find("inventory-error").value == "Could not load sessions"
        assert calls == ["", "a", "boom"]

    run(scenario())


def test_late_result_cannot_overwrite_new_query() -> None:
    async def scenario() -> None:
        gates: dict[str, asyncio.Event] = {}

        async def fetch(query: str) -> tuple[str, ...]:
            gate = gates.setdefault(query, asyncio.Event())
            await gate.wait()
            return (f"item-{query}",)

        app = await render_component(AppShell, fetch_inventory=fetch)
        # First request for "" is pending; move to "a" then "ab".
        await app.set_props_and_fire("inventory-query", {"value": "a"}, "change", "a")
        await app.set_props_and_fire("inventory-query", {"value": "ab"}, "change", "ab")
        # Release out of order: stale results must be dropped.
        gates[""].set()
        gates["a"].set()
        await app.settle()
        with pytest.raises(KeyError):
            app.find("session-item-")
        with pytest.raises(KeyError):
            app.find("session-item-a")
        gates["ab"].set()
        await app.settle()
        assert app.find("session-item-ab").value == "item-ab"

    run(scenario())


def test_unmount_cancels_pending_fetch() -> None:
    async def scenario() -> None:
        import flet as ft

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch(query: str) -> tuple[str, ...]:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return ("never",)

        @ft.component
        def Root() -> Any:
            show, set_show = ft.use_state(True)
            return ft.Column(
                controls=[
                    ft.Button("toggle", key="toggle", on_click=lambda e: set_show(not show)),
                    AppShell(  # type: ignore[call-arg]
                        key="shell", fetch_inventory=fetch
                    )
                    if show
                    else ft.Text("gone", key="blank"),
                ]
            )

        app = await render_component(Root)
        await asyncio.wait_for(started.wait(), timeout=5)
        toggle = app.find("toggle")
        await app.session.dispatch_event(toggle._i, "click", "")
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert app.find("blank").value == "gone"

    run(scenario())


def test_sessions_are_isolated() -> None:
    async def scenario() -> None:
        app_a = await render_component(AppShell, fetch_inventory=_immediate_fetch)
        app_b = await render_component(AppShell, fetch_inventory=_immediate_fetch)
        await app_a.set_props_and_fire("destinations", {"selected_index": 3}, "change")
        assert app_a.find("destination-title").value == "RelayDeck — Settings"
        assert app_b.find("destination-title").value == "RelayDeck — Console"

    run(scenario())


def test_no_forbidden_core_imports() -> None:
    import flet as ft

    assert not hasattr(ft, "SecureStorage")
    assert not hasattr(ft, "Canvas")
