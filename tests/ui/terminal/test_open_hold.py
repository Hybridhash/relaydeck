"""A newly opened terminal holds its size reports until its layout settles.

The screen measures its own controls on the first frames, so the terminal reports a few sizes
before its final one. Reports coalesce until control settles and sizes stop changing;
matching the previous size must not release an intermediate layout.
"""

from __future__ import annotations

import asyncio
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.surfaces import agent_hooks
from relaydeck.client.features.terminal.surfaces.agent_hooks import use_open_hold
from tests.support.host_harness import render_component, run

pytestmark = pytest.mark.open_hold


def _probe(expected: tuple[int, int] | None, sent: list[tuple[int, int]], out: dict[str, Any]):
    @ft.component
    def Probe() -> ft.Control:
        report, held = use_open_hold(expected, lambda cols, rows: sent.append((cols, rows)))
        out["report"], out["held"] = report, held
        return ft.Text("held" if held else "shown", key="probe")

    return Probe


def test_the_expected_size_waits_for_layout_to_settle_and_only_latest_is_sent() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await render_component(_probe((59, 54), sent, out))
        out["report"](59, 46)
        out["report"](59, 51)
        assert sent == [] and out["held"] is True
        out["report"](59, 54)
        assert sent == []
        await asyncio.sleep(agent_hooks.OPEN_SETTLE_S + 0.05)
        await app.settle()
        assert sent == [(59, 54)]
        assert out["held"] is False and app.find("probe").value == "shown"
        out["report"](59, 24)  # Live geometry uses a short trailing settle.
        assert sent == [(59, 54)]
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert sent == [(59, 54), (59, 24)]
        out["report"](59, 24)  # Same-size reports still repaint.
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert sent[-2:] == [(59, 24), (59, 24)]

    run(scenario())


def test_without_an_expected_size_the_cap_ends_the_hold_with_the_latest_size(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.3)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await render_component(_probe(None, sent, out))
        out["report"](59, 46)
        out["report"](59, 54)
        assert sent == []
        await asyncio.sleep(0.4)
        await app.settle()
        assert sent == [(59, 54)] and out["held"] is False

    run(scenario())


def test_a_settled_size_that_never_matches_is_still_sent_after_the_cap(monkeypatch: Any) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.3)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await render_component(_probe((59, 54), sent, out))
        out["report"](40, 30)  # rotated since the last open: the expected size never comes
        assert sent == []
        await asyncio.sleep(0.4)
        await app.settle()
        assert sent == [(40, 30)] and out["held"] is False

    run(scenario())


def _toggle_probe(
    expected: tuple[int, int] | None, sent: list[tuple[int, int]], out: dict[str, Any]
):
    @ft.component
    def Probe() -> ft.Control:
        active, set_active = ft.use_state(True)
        report, held = use_open_hold(
            expected, lambda cols, rows: sent.append((cols, rows)), active=active
        )
        out["report"], out["held"], out["set_active"] = report, held, set_active
        return ft.Text("held" if held else "shown", key="probe")

    return Probe


def test_a_screen_brought_back_to_the_front_holds_its_reports_again() -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await render_component(_toggle_probe((59, 54), sent, out))
        out["report"](59, 54)
        await asyncio.sleep(agent_hooks.OPEN_SETTLE_S + 0.05)
        await app.settle()
        assert sent == [(59, 54)] and out["held"] is False
        out["set_active"](False)
        await app.settle()
        out["set_active"](True)
        await app.settle()
        assert out["held"] is False  # no report yet: nothing to conceal
        out["report"](59, 40)
        out["report"](59, 47)
        await app.settle()
        assert sent == [(59, 54)] and out["held"] is True
        out["report"](59, 54)
        await asyncio.sleep(agent_hooks.OPEN_SETTLE_S + 0.05)
        await app.settle()
        assert sent == [(59, 54), (59, 54)] and out["held"] is False
        out["report"](59, 24)  # A later change is forwarded after the short live settle.
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert sent[-1] == (59, 24)

    run(scenario())


def test_control_settlement_precedes_release_and_a_window_resize_while_held_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.15)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.05)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            settled, set_settled = ft.use_state(False)
            report, held = use_open_hold(
                (166, 37), lambda c, r: sent.append((c, r)), settled=settled
            )
            out.update(report=report, held=held, settle=set_settled)
            return ft.Text("held" if held else "shown", key="probe")

        app = await render_component(Probe)
        out["report"](166, 37)
        await asyncio.sleep(0.2)
        await app.settle()
        assert sent == [] and out["held"]
        out["settle"](True)
        await app.settle(rounds=1)
        out["report"](120, 28)  # Window changed while control was settling.
        await asyncio.sleep(0.03)
        out["report"](120, 28)
        await asyncio.sleep(0.03)
        assert sent == []
        await asyncio.sleep(0.04)
        await app.settle()
        assert sent == [(120, 28)] and not out["held"]

    run(scenario())


@pytest.mark.parametrize("expected", [None, (166, 37)], ids=["cold", "warm"])
@pytest.mark.parametrize("report_while_unready", [False, True])
def test_opening_readiness_loss_requires_a_fresh_report_after_restoration(
    monkeypatch: pytest.MonkeyPatch,
    expected: tuple[int, int] | None,
    report_while_unready: bool,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.12)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.05)
    monkeypatch.setattr(agent_hooks, "RESIZE_SETTLE_S", 0.02)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            ready, set_ready = ft.use_state(True)
            report, held = use_open_hold(expected, lambda c, r: sent.append((c, r)), settled=ready)
            out.update(report=report, held=held, ready=set_ready)
            return ft.Text("held" if held else "shown", key="probe")

        app = await render_component(Probe)
        opening_report = out["report"]
        opening_report(166, 35)
        assert out["held"] and not sent
        out["ready"](False)
        await app.settle(rounds=1)
        assert out["held"]  # Losing readiness does not change the visual cover.
        unready_report = out["report"]
        if report_while_unready:
            unready_report(120, 28)
        await asyncio.sleep(0.15)
        assert not sent and out["held"]
        out["ready"](True)
        await app.settle(rounds=1)
        opening_report(100, 20)
        unready_report(110, 22)
        await asyncio.sleep(0.15)  # Both quiet and cap must discard old geometry.
        await app.settle(rounds=1)
        assert not sent
        assert not out["held"]  # A visual timeout does not authorize an old resize.
        out["report"](140, 32)
        await asyncio.sleep(0.04)
        await app.settle(rounds=1)
        assert sent == [(140, 32)] and not out["held"]

    run(scenario())


@pytest.mark.parametrize("expected", [None, (59, 54)], ids=["cold", "warm"])
@pytest.mark.parametrize("changing", [False, True], ids=["identical", "changing"])
def test_continuous_reports_cannot_postpone_the_settled_hold_cap(
    monkeypatch: pytest.MonkeyPatch,
    expected: tuple[int, int] | None,
    changing: bool,
) -> None:
    cap = 0.15
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", cap)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.08)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        sent: list[tuple[float, tuple[int, int]]] = []
        out: dict[str, Any] = {}
        latest = [(59, 54)]

        def forward(cols: int, rows: int) -> None:
            assert (cols, rows) == latest[0]
            sent.append((loop.time(), (cols, rows)))

        @ft.component
        def Probe() -> ft.Control:
            settled, set_settled = ft.use_state(False)
            report, held = use_open_hold(expected, forward, settled=settled)
            out.update(report=report, held=held, settle=set_settled)
            return ft.Text("held" if held else "shown", key="probe")

        async def stream(duration: float) -> None:
            end = loop.time() + duration
            index = 0
            while loop.time() < end:
                latest[0] = (59, 54 + index % 3) if changing else (59, 54)
                out["report"](*latest[0])
                index += 1
                await asyncio.sleep(0.02)

        app = await render_component(Probe)
        await stream(cap + 0.08)
        assert sent == [] and out["held"]  # No fallback can bypass control/native readiness.
        start = loop.time()
        out["settle"](True)
        await app.settle(rounds=1)
        await stream(cap * 2)
        # Reports never paused for the quiet interval, including after the cap.
        assert sent and not out["held"] and app.find("probe").value == "shown"
        assert sent[0][0] - start < cap + 0.07
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert len(sent) > 1  # After release the last live report is still published.
        latest[0] = (80, 24)
        before = len(sent)
        out["report"](*latest[0])
        out["report"](*latest[0])
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert [size for _, size in sent[before:]] == [(80, 24)]

    run(scenario())


def test_a_re_shown_screen_releases_the_latest_size_at_the_cap(monkeypatch: Any) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.3)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}
        app = await render_component(_toggle_probe(None, sent, out))
        await asyncio.sleep(0.4)
        out["set_active"](False)
        await app.settle()
        out["set_active"](True)
        await app.settle()
        out["report"](59, 40)
        out["report"](59, 47)
        assert sent == []
        await asyncio.sleep(0.4)
        await app.settle()
        assert sent == [(59, 47)] and out["held"] is False

    run(scenario())


def test_input_phase_changes_restart_quiet_without_reopening_a_finished_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.22)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.08)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            phase, set_phase = ft.use_state((True, False))
            report, held = use_open_hold(
                (166, 37), lambda c, r: sent.append((c, r)), input_phase=phase
            )
            out.update(report=report, held=held, phase=set_phase)
            return ft.Text("held" if held else "shown", key="probe")

        app = await render_component(Probe)
        out["report"](166, 35)
        await asyncio.sleep(0.05)
        out["phase"]((True, True))
        await app.settle(rounds=1)
        await asyncio.sleep(0.04)  # Beyond the original quiet timer.
        assert not sent
        out["report"](166, 37)
        await asyncio.sleep(0.1)
        await app.settle(rounds=1)
        assert sent == [(166, 37)] and not out["held"]
        out["phase"]((False, False))
        await app.settle(rounds=1)
        out["report"](120, 25)
        out["report"](120, 25)
        await asyncio.sleep(agent_hooks.RESIZE_SETTLE_S + 0.03)
        assert sent == [(166, 37), (120, 25)] and not out["held"]

    run(scenario())


def test_continuous_input_phase_changes_cannot_postpone_the_opening_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.15)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.08)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        sent: list[float] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            phase, set_phase = ft.use_state((True, False))
            settled, set_settled = ft.use_state(False)
            report, held = use_open_hold(
                (166, 37),
                lambda *_: sent.append(loop.time()),
                input_phase=phase,
                settled=settled,
            )
            out.update(report=report, held=held, phase=set_phase, settle=set_settled)
            return ft.Text("held" if held else "shown", key="probe")

        app = await render_component(Probe)
        # A report stream and input changes both restart quiet, never the cap.
        out["report"](166, 37)
        start = loop.time()
        out["settle"](True)
        await app.settle(rounds=1)
        count = 0
        while loop.time() - start < 0.25:
            out["phase"]((True, bool(count % 2)))
            await app.settle(rounds=1)
            out["report"](166, 37)
            await asyncio.sleep(0.01)
            count += 1
        assert sent and not out["held"]
        assert 0.12 <= sent[0] - start < 0.2

    run(scenario())
