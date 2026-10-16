"""Leaving the pairing page must not void an attempt the Mac may still approve.

The held result is observed in the web session store, not the harness callback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flet as ft

from relaydeck.client.features.pairing.components import PairingPanel
from relaydeck.client.serve import WebSessionStore, web_pairing_flow
from relaydeck.gateway.app import create_app
from tests.support.companion.pairing_harness import Harness
from tests.support.host_harness import render_component, run
from tests.support.pairing import ORIGIN

PAGE = "page-1"


async def _press(app: Any, key: str) -> None:
    control = app.find(key)
    handler = getattr(control, "on_click", None) or control.on_press
    arity = handler.__code__.co_argcount
    result = handler(SimpleNamespace(control=control)) if arity else handler()
    if asyncio.iscoroutine(result):
        await result


async def _start(h: Harness, gate: asyncio.Event) -> tuple[WebSessionStore, Any, dict[str, Any]]:
    store = WebSessionStore(ORIGIN, b"s" * 32, now_ms=lambda: h.clock[0])
    store.browsers["browser"] = h.clock[0] + 600_000
    store.browser_pages[PAGE] = "browser"
    flow = web_pairing_flow(store, PAGE, create_app(h.state))
    assert flow is not None
    entered = asyncio.Event()

    async def sleep(interval: float) -> None:
        entered.set()
        await gate.wait()
        h.clock[0] += 2000

    flow.sleep = sleep
    setters: dict[str, Any] = {}

    @ft.component
    def Host() -> Any:
        visible, set_visible = ft.use_state(True)
        setters["visible"] = set_visible
        return ft.Column(
            controls=[PairingPanel(flow, endpoint=ORIGIN, host_id="home-mac")]
            if visible
            else [ft.Text("elsewhere")]
        )

    app = await render_component(Host)
    setters["app"] = app
    await _press(app, "connect-begin")
    await asyncio.wait_for(entered.wait(), 2)  # the approval poll is suspended
    return store, flow, setters


async def _set_visible(setters: dict[str, Any], visible: bool) -> None:
    setters["visible"](visible)
    await setters["app"].settle()


async def _wait_ready(store: WebSessionStore) -> None:
    for _ in range(300):
        if PAGE in store.pairing_ready:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no held result")


def test_approval_after_unmount_leaves_a_held_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state", approve=False)
        gate = asyncio.Event()
        try:
            store, flow, setters = await _start(h, gate)
            await _set_visible(setters, False)
            h.decide(True)
            gate.set()
            await _wait_ready(store)
            assert store.pairing_ready[PAGE][0] == flow.generation
        finally:
            await h.http.aclose()

    run(scenario())


def test_cancel_after_remount_prevents_the_held_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state", approve=False)
        gate = asyncio.Event()
        try:
            store, _flow, setters = await _start(h, gate)
            await _set_visible(setters, False)
            await _set_visible(setters, True)  # remount
            await _press(setters["app"], "connect-cancel")
            h.decide(True)
            gate.set()
            await asyncio.sleep(0.2)
            assert PAGE not in store.pairing_ready
        finally:
            await h.http.aclose()

    run(scenario())


def test_expiry_after_unmount_prevents_the_held_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path / "state", approve=False)
        gate = asyncio.Event()
        try:
            store, _flow, setters = await _start(h, gate)
            await _set_visible(setters, False)
            h.clock[0] += 400_000  # beyond the request lifetime
            gate.set()
            await asyncio.sleep(0.2)
            assert PAGE not in store.pairing_ready
        finally:
            await h.http.aclose()

    run(scenario())
