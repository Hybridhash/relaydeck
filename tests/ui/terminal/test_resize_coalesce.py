"""A return to the app settles geometry once; same-size refreshes still repaint."""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.surfaces import agent_hooks
from relaydeck.client.features.terminal.surfaces.agent_hooks import use_open_hold
from tests.support.host_harness import render_component, run

pytestmark = pytest.mark.open_hold


def _probe(sent: list[tuple[int, int]], out: dict[str, Any]) -> Any:
    @ft.component
    def Probe() -> ft.Control:
        activation, set_activation = ft.use_state(0)
        out["bump"] = lambda: set_activation(activation + 1)
        report, held = use_open_hold(
            None, lambda cols, rows: sent.append((cols, rows)), activation=activation
        )
        out["report"], out["held"] = report, held
        return ft.Text("held" if held else "shown", key="probe")

    return Probe


async def _opened(sent: list[tuple[int, int]], out: dict[str, Any]) -> Any:
    app = await render_component(_probe(sent, out))
    out["report"](80, 22)
    await asyncio.sleep(agent_hooks.OPEN_HOLD_S + 0.1)
    await app.settle()
    assert sent == [(80, 22)] and out["held"] is False
    return app


def test_a_return_burst_publishes_one_resize_with_the_final_size() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await _opened(sent, out)
        sent.clear()
        out["bump"]()
        await app.settle()
        for cols, rows in ((80, 42), (80, 12), (80, 42), (80, 46), (80, 52)):
            out["report"](cols, rows)
            await asyncio.sleep(0.08)
        assert sent == [] and out["held"] is False  # a return never covers the surface
        await asyncio.sleep(agent_hooks.ACTIVATION_SETTLE_S + 0.1)
        await app.settle()
        assert sent == [(80, 52)]

    run(scenario())


def test_a_same_size_report_after_a_return_still_repaints_once() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await _opened(sent, out)
        sent.clear()
        out["bump"]()
        await app.settle()
        out["report"](80, 22)  # the size did not change; the repaint is still due
        await asyncio.sleep(agent_hooks.ACTIVATION_SETTLE_S + 0.1)
        await app.settle()
        assert sent == [(80, 22)]

    run(scenario())


def test_the_cap_bounds_a_return_that_never_goes_quiet() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await _opened(sent, out)
        sent.clear()
        out["bump"]()
        await app.settle()
        for rows in range(30, 40):
            out["report"](80, rows)
            await asyncio.sleep(0.1)
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.1)
        assert sent and sent[-1] == (80, 39)

    run(scenario())


def test_without_a_return_keyboard_reports_publish_normally_and_never_cover() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await _opened(sent, out)
        sent.clear()
        out["report"](80, 12)
        assert out["held"] is False
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.1)
        await app.settle()
        assert sent == [(80, 12)] and out["held"] is False

    run(scenario())
