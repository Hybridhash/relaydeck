"""Settings › Server health: tiles from a snapshot, hidden without the scope, polling that stops
when hidden or unmounted, an error that keeps the last data, and the phone layout."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.a11y.focus import iter_controls
from relaydeck.client.features.settings import server_health
from relaydeck.client.features.settings.components import SettingsPage
from relaydeck.client.features.settings.prefs import TerminalPrefs
from relaydeck.client.features.settings.server_health import HealthPanel
from relaydeck.client.features.settings.server_health_model import poll_loop
from relaydeck.client.features.sync.transport import TransportError
from tests.support.host_harness import render_component, run

SNAPSHOT: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/gateway/server-metrics.json").read_text()
)


class FakeTransport:
    def __init__(self, *, error: str = "", delay: float = 0.0) -> None:
        self.calls = 0
        self.in_flight = 0
        self.peak = 0
        self.error = error
        self.delay = delay
        self.failing_after: int | None = None

    async def metrics(self) -> dict[str, Any]:
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.error or (self.failing_after is not None and self.calls > self.failing_after):
                raise TransportError(self.error or "host_offline", "x", False)
            return SNAPSHOT
        finally:
            self.in_flight -= 1


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_health, "poll_loop", functools.partial(poll_loop, interval=0.02))


def _texts(app: Any) -> list[str]:
    return [
        c.value
        for c in iter_controls([app.page.views[0].controls])
        if isinstance(c, ft.Text) and c.value
    ]


def _has(app: Any, key: str) -> bool:
    try:
        app.find(key)
    except KeyError:
        return False
    return True


def test_tiles_table_and_lists_render_from_a_snapshot() -> None:
    async def scenario() -> None:
        app = await render_component(HealthPanel, FakeTransport(), active=True, phone=False)
        await app.wait_for("health-tile-requests")
        for name in ("requests", "errors", "loop", "sockets"):
            assert _has(app, f"health-tile-{name}")
            assert _has(app, f"health-spark-{name}")
        assert app.find("health-tile-requests-value").value == "33"
        assert app.find("health-tile-sockets-note").value == "1 controlling · 1 viewing"
        assert app.find("health-dot-semantics").label == "Server status: Healthy"
        assert app.find("health-spark-loop-semantics").label.startswith("Event loop delay")
        assert app.find("health-route-0") is not None
        assert {"Live connections".upper(), "SIGN-INS", "USAGE REFRESH"} <= set(_texts(app))
        assert any("up " in t and "Healthy" in t for t in _texts(app))
        assert _has(app, "health-footer")

    run(scenario())


def test_the_panel_is_hidden_without_the_capability() -> None:
    async def scenario() -> None:
        transport = FakeTransport(error="unsupported_capability")
        app = await render_component(HealthPanel, transport, active=True, phone=False)
        await app.settle()
        assert not _has(app, "settings-server-health")
        assert not _has(app, "health-tile-requests")
        calls = transport.calls
        await app.settle(5)
        assert transport.calls == calls == 1  # a refused scope is not asked again

    run(scenario())


def test_the_settings_page_places_it_after_devices_and_before_diagnostics() -> None:
    async def scenario() -> None:
        marker = ft.Text("panel", key="settings-server-health")
        app = await render_component(
            SettingsPage,
            TerminalPrefs(),
            lambda value: None,
            phone=False,
            account="me",
            version="0",
            on_logout=lambda: None,
            management=ft.Text("management"),
            server_health=marker,
            diagnostics=(("a", "b"),),
        )
        keys = [
            getattr(c, "key", None)
            for c in iter_controls([app.page.views[0].controls])
            if getattr(c, "key", None)
            in ("settings-management", "settings-server-health", "diagnostics-pane")
        ]
        assert keys == ["settings-management", "settings-server-health", "diagnostics-pane"]

    run(scenario())


class Harness:
    """Mount/active switches around the panel, driven by real clicks."""

    @staticmethod
    @ft.component
    def view(transport: Any) -> Any:
        active, set_active = ft.use_state(True)
        mounted, set_mounted = ft.use_state(True)
        return ft.Column(
            [
                ft.TextButton("a", key="toggle-active", on_click=lambda e: set_active(not active)),
                ft.TextButton("m", key="toggle-mount", on_click=lambda e: set_mounted(False)),
                *([HealthPanel(transport, active=active, phone=False)] if mounted else []),
            ]
        )


def test_polling_stops_when_hidden_and_when_unmounted_and_never_overlaps() -> None:
    async def scenario() -> None:
        transport = FakeTransport(delay=0.03)
        app = await render_component(Harness.view, transport)
        await app.wait_for("health-tile-requests")
        await app.settle(5)
        assert transport.calls >= 2 and transport.peak == 1
        await app.set_props_and_fire("toggle-active", {}, "click")
        await app.settle(3)
        frozen = transport.calls
        await app.settle(8)
        assert transport.calls == frozen  # hidden: no more requests
        await app.set_props_and_fire("toggle-active", {}, "click")
        await app.settle(8)
        assert transport.calls > frozen and transport.peak == 1
        await app.set_props_and_fire("toggle-mount", {}, "click")
        await app.settle(3)
        gone = transport.calls
        await app.settle(8)
        assert transport.calls == gone  # unmounted: the task was cancelled

    run(scenario())


def test_an_error_keeps_the_last_numbers_and_says_it_is_retrying() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = await render_component(HealthPanel, transport, active=True, phone=False)
        await app.wait_for("health-tile-requests")
        transport.failing_after = transport.calls
        await app.wait_for("health-error")
        assert app.find("health-error").value == "Couldn't update · retrying"
        assert app.find("health-tile-requests-value").value == "33"  # data stays
        transport.failing_after = None
        for _ in range(100):
            await app.settle(1)
            if not _has(app, "health-error"):
                break
        assert not _has(app, "health-error")

    run(scenario())


def test_phone_layout_stacks() -> None:
    async def scenario() -> None:
        wide = await render_component(HealthPanel, FakeTransport(), active=True, phone=False)
        await wide.wait_for("health-split")
        assert isinstance(wide.find("health-split"), ft.Row)
        assert isinstance(wide.find("health-tiles"), ft.Row)
        phone = await render_component(HealthPanel, FakeTransport(), active=True, phone=True)
        await phone.wait_for("health-split")
        assert isinstance(phone.find("health-split"), ft.Column)
        assert isinstance(phone.find("health-tiles"), ft.Column)  # 2 x 2
        assert _has(phone, "health-route-0")
        assert any("calls · p50" in t for t in _texts(phone))  # two-line route rows

    run(scenario())
