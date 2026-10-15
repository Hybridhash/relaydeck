"""Real Root preference loading/saving must retain the latest space choice."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import flet as ft
import pytest

from relaydeck.client.app import Root
from relaydeck.client.features.inventory.routing import RouteBridge
from relaydeck.client.features.settings.prefs import (
    PREFS_KEY,
    TerminalPrefs,
    clean,
    encode,
    load,
    save,
)
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.transport import HttpxGatewayTransport
from tests.support.host_harness import render_component, run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.find_optional import chips_with
from tests.support.ui.navigation import _StubController, _StubTransport
from tests.support.ui.spaces_filter import _spaces_state


class _MemoryPrefs:
    def __init__(self, stored: TerminalPrefs | None = None) -> None:
        self.data = {PREFS_KEY: encode(stored)} if stored is not None else {}
        self.read_started = asyncio.Event()
        self.read_gate: asyncio.Event | None = None
        self.write_started = asyncio.Event()
        self.write_gate: asyncio.Event | None = None
        self.writes = 0

    async def get(self, key: str) -> str | None:
        captured = self.data.get(key)
        self.read_started.set()
        if self.read_gate is not None:
            await self.read_gate.wait()
        return captured

    async def set(self, key: str, value: str) -> None:
        self.writes += 1
        self.write_started.set()
        if self.writes == 1 and self.write_gate is not None:
            await self.write_gate.wait()
        self.data[key] = value


async def _root(store: _MemoryPrefs, width: int = 390) -> Any:
    controller = _StubController()
    sessions = make_scripted_sessions()

    @ft.component
    def Parent() -> Any:
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            controls=[
                Root(
                    controller=cast(SyncController, controller),
                    transport=cast(HttpxGatewayTransport, _StubTransport()),
                    host_id="home-mac",
                    bridge=RouteBridge(width=width, height=844),
                    go=lambda route: None,
                    session_factory=sessions.factory,
                    account="synthetic",
                    preferences=store,
                )
                if mounted
                else ft.Text("Closed"),
                ft.Button("Unmount", key="test-unmount", on_click=lambda e: set_mounted(False)),
            ]
        )

    app = await render_component(Parent)
    controller.push(_spaces_state())
    await app.settle()
    return app


@pytest.mark.parametrize("width", [390, 1280])
def test_device_space_persistence_restores_actual_root_rows_counts_and_dropdown(width: int) -> None:
    async def scenario() -> None:
        store = _MemoryPrefs()
        app = await _root(store, width)
        try:
            await app.set_props_and_fire("host-chip-space-home-mac:w1", {}, "click")
            assert json.loads(store.data[PREFS_KEY])["space_key"] == "home-mac:w1"
        finally:
            await app.set_props_and_fire("test-unmount", {}, "click")
        restored = await _root(store, width)
        try:
            annotations = [
                item.content
                for item in chips_with(restored.find("console-filter-chips"), "host-chip-space-")
            ]
            selected = [item for item in annotations if item.selected is True]
            assert len(selected) == 1
            assert selected[0].content.key == "host-chip-space-home-mac:w1"
            assert selected[0].label == "Atlas, 2 agents, selected"
            assert restored.find("inventory-project").value == "home-mac:w1"
            assert restored.find("agents-count").value == "2 agents"
            assert [card.key for card in restored.find("agents-tiles").controls] == [
                "agent-home-mac:b1",
                "agent-home-mac:w1",
            ]
            await restored.set_props_and_fire("host-chip-lab-mac", {}, "click")
            assert json.loads(store.data[PREFS_KEY])["space_key"] == ""
            selected = [
                item.content
                for item in chips_with(restored.find("console-filter-chips"), "host-chip-space-")
                if item.content.selected is True
            ]
            assert len(selected) == 1 and selected[0].label == "All spaces, selected"
        finally:
            await restored.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_delayed_preference_load_cannot_replace_explicit_all_spaces_choice() -> None:
    async def scenario() -> None:
        store = _MemoryPrefs(TerminalPrefs(space_key="home-mac:w2"))
        store.read_gate = asyncio.Event()
        app = await _root(store)
        try:
            await asyncio.wait_for(store.read_started.wait(), 1)
            await app.set_props_and_fire("host-chip-space-home-mac:w1", {}, "click")
            await app.set_props_and_fire("host-chip-space-all", {}, "click")
            store.read_gate.set()
            await app.settle()
            assert app.find("inventory-project").value == ""
            assert app.find("agents-count").value == "5 agents"
            assert json.loads(store.data[PREFS_KEY])["space_key"] == ""
        finally:
            store.read_gate.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_delayed_old_preference_write_cannot_overwrite_latest_space_choice() -> None:
    async def scenario() -> None:
        store = _MemoryPrefs()
        store.write_gate = asyncio.Event()
        app = await _root(store)
        try:
            await app.set_props_and_fire("host-chip-space-home-mac:w1", {}, "click")
            await asyncio.wait_for(store.write_started.wait(), 1)
            await app.set_props_and_fire("host-chip-space-all", {}, "click")
            store.write_gate.set()
            await app.settle()
            assert json.loads(store.data[PREFS_KEY])["space_key"] == ""
            assert app.find("inventory-project").value == ""
        finally:
            store.write_gate.set()
            await app.set_props_and_fire("test-unmount", {}, "click")

    run(scenario())


def test_space_pref_schema_is_backward_compatible_and_rejects_corrupt_fields() -> None:
    async def scenario() -> None:
        store = _MemoryPrefs()
        chosen = TerminalPrefs(font_size=15, show_key_row=False, space_key="home-mac:w1")
        assert await save(store.set, chosen)
        assert await load(store.get) == chosen

    run(scenario())
    assert clean({"font_size": 15, "show_key_row": False}) == TerminalPrefs(
        font_size=15, show_key_row=False
    )
    for bad in (None, False, [], 12, "unscoped", "a:b\n", "a:" + "b" * 512):
        assert clean({"space_key": bad}).space_key == ""
