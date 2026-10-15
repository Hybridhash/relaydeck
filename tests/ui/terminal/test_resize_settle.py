"""Post-open geometry settles without flooding or starving the writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft
import pytest

from relaydeck.client.features.terminal.surfaces import agent_hooks
from tests.support.host_harness import render_component, run

pytestmark = pytest.mark.open_hold


@dataclass
class _ResizeTimer:
    deadline: float
    delay: float
    callback: Callable[..., Any]
    args: tuple[Any, ...]
    cancelled: bool = False
    fired: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class _ResizeClock:
    """Drive only the hook's timers; leave Flet and the real writer task intact."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 0.0
        self.timers: list[_ResizeTimer] = []
        loop = asyncio.get_running_loop()
        original = loop.call_later

        def schedule(
            delay: float, callback: Callable[..., Any], *args: Any, context: Any = None
        ) -> Any:
            if getattr(callback, "__module__", None) != agent_hooks.__name__:
                return original(delay, callback, *args, context=context)
            timer = _ResizeTimer(self.now + delay, delay, callback, args)
            self.timers.append(timer)
            return timer

        monkeypatch.setattr(loop, "call_later", schedule)

    def deadline(self, delay: float) -> float:
        return min(
            timer.deadline
            for timer in self.timers
            if not timer.cancelled and not timer.fired and timer.delay == delay
        )

    def advance_to(self, target: float) -> None:
        assert target >= self.now
        while due := [
            timer
            for timer in self.timers
            if not timer.cancelled and not timer.fired and timer.deadline <= target
        ]:
            timer = min(due, key=lambda timer: timer.deadline)
            self.now = timer.deadline
            timer.fired = True
            timer.callback(*timer.args)
        self.now = target


def _cap_then_report(
    clock: _ResizeClock, report: Callable[[int, int], Any], row: int, cap_delay: float
) -> int:
    start = clock.now
    report(225, row)
    deadline = clock.deadline(cap_delay)
    # Keep quiet postponed until the real cap callback runs. Do not yield to
    # the newly spawned writer until another native report has arrived.
    for index in range(1, 5):
        clock.advance_to(start + (deadline - start) * index / 5)
        report(225, row + index)
    clock.advance_to(deadline)
    report(225, row + 5)
    return row + 5


@pytest.mark.parametrize("phase", ["opening", "live"])
def test_cap_due_publication_survives_new_reports_before_the_writer_runs(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def scenario() -> None:
        clock = _ResizeClock(monkeypatch)
        sent: list[tuple[float, int]] = []
        expected: list[tuple[float, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            report, held = agent_hooks.use_open_hold(
                (225, 39), lambda _c, r: sent.append((clock.now, r))
            )
            out.update(report=report, held=held)
            return ft.Text("held" if held else "shown")

        app = await render_component(Probe)
        if phase == "live":
            clock.advance_to(agent_hooks.OPEN_HOLD_S)
            await app.settle(rounds=1)
            assert not out["held"] and not sent
        for boundary in range(3):
            cap = (
                agent_hooks.OPEN_HOLD_S
                if phase == "opening" and boundary == 0
                else agent_hooks.RESIZE_MAX_WAIT_S
            )
            start = clock.now
            newest = _cap_then_report(clock, out["report"], 40 + boundary * 10, cap)
            assert clock.now == start + cap
            assert sent == expected
            await asyncio.sleep(0)  # The serialized writer now gets its turn.
            expected.append((clock.now, newest))
            assert sent == expected

        # Once the due publication is consumed, a new burst still gets its
        # trailing repaint, even if its final grid equals the last one sent.
        out["report"](225, 30)
        clock.advance_to(clock.now + 0.05)
        out["report"](225, newest)
        clock.advance_to(clock.now + agent_hooks.RESIZE_SETTLE_S)
        await asyncio.sleep(0)
        assert sent == [*expected, (clock.now, newest)]

    run(scenario())


def test_cap_due_publication_waits_for_a_busy_writer_with_the_newest_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = _ResizeClock(monkeypatch)
        finish = asyncio.Event()
        sent: list[int] = []
        out: dict[str, Any] = {}
        busy = 0

        async def forward(_cols: int, rows: int) -> None:
            nonlocal busy
            busy += 1
            assert busy == 1
            try:
                sent.append(rows)
                if rows == 30:
                    await finish.wait()
            finally:
                busy -= 1

        @ft.component
        def Probe() -> ft.Control:
            report, _held = agent_hooks.use_open_hold((225, 39), forward)
            out.update(report=report)
            return ft.Text("probe")

        app = await render_component(Probe)
        clock.advance_to(agent_hooks.OPEN_HOLD_S)
        await app.settle(rounds=1)
        try:
            out["report"](225, 30)
            clock.advance_to(clock.now + agent_hooks.RESIZE_SETTLE_S)
            await asyncio.sleep(0)
            assert sent == [30] and busy == 1
            newest = _cap_then_report(clock, out["report"], 40, agent_hooks.RESIZE_MAX_WAIT_S)
            for row in range(50, 62):
                clock.advance_to(clock.now + 0.1)
                out["report"](225, row)
                newest = row
                assert sent == [30] and busy == 1
            finish.set()
            await asyncio.sleep(0)
            assert sent == [30, newest] and busy == 0
            out["report"](225, 39)
            clock.advance_to(clock.now + agent_hooks.RESIZE_SETTLE_S)
            await asyncio.sleep(0)
            assert sent == [30, newest, 39]
        finally:
            finish.set()

    run(scenario())


async def _opened(monkeypatch: pytest.MonkeyPatch, send: Any) -> tuple[Any, dict[str, Any]]:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.05)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.02)
    out: dict[str, Any] = {}

    @ft.component
    def Probe() -> ft.Control:
        report, held = agent_hooks.use_open_hold((225, 39), send)
        out.update(report=report, held=held)
        return ft.Text("held" if held else "shown", key="probe")

    app = await render_component(Probe)
    assert not out["held"]
    out["report"](225, 39)
    await asyncio.sleep(0.18)
    return app, out


@pytest.mark.parametrize("rows", [[37, 39, 39, 40, 41, 34, 39], [30, 35, 35, 35], [34, 32, 34]])
def test_live_burst_forwards_only_the_final_grid_including_return_to_start(
    monkeypatch: pytest.MonkeyPatch, rows: list[int]
) -> None:
    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        app, out = await _opened(monkeypatch, lambda c, r: sent.append((c, r)))
        assert sent == [(225, 39)]
        sent.clear()
        interval = 0.175 / 6 if len(rows) == 7 else 0.017 / 3
        for index, row in enumerate(rows):
            if index:
                await asyncio.sleep(interval)
            out["report"](225, row)
            assert not sent
        await asyncio.sleep(0.19)
        await app.settle(rounds=1)
        assert sent == [(225, rows[-1])]
        assert not out["held"]  # No connecting cover during normal resizing.
        out["report"](225, rows[-1])  # A later explicit same-size refresh still repaints.
        await asyncio.sleep(0.18)
        assert sent == [(225, rows[-1]), (225, rows[-1])]

    run(scenario())


def test_a_continuous_drag_makes_bounded_progress_and_sends_its_last_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        sent: list[tuple[float, tuple[int, int]]] = []
        latest = [(225, 39)]

        def forward(cols: int, rows: int) -> None:
            assert (cols, rows) == latest[0]
            sent.append((loop.time(), (cols, rows)))

        _, out = await _opened(monkeypatch, forward)
        sent.clear()
        start = loop.time()
        index = 0
        while loop.time() - start < 1.16:
            latest[0] = (225, 40 + index % 10)
            out["report"](*latest[0])
            if index == 0:
                assert not sent
            index += 1
            await asyncio.sleep(0.035)
        during_drag = list(sent)
        assert 2 <= len(during_drag) <= 3
        assert during_drag[0][0] - start <= 0.57
        assert all(b[0] - a[0] <= 0.57 for a, b in zip(during_drag, during_drag[1:], strict=False))
        await asyncio.sleep(0.19)
        assert sent[-1][1] == latest[0]
        assert len(sent) <= 4

    run(scenario())


@pytest.mark.parametrize("transition", ["hide", "unready", "replace", "unmount"])
def test_pending_geometry_and_stale_callbacks_never_cross_a_lifecycle_boundary(
    monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.05)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.02)

    async def scenario() -> None:
        sent: list[tuple[int, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe(active: bool, ready: bool, binding: int) -> ft.Control:
            report, held = agent_hooks.use_open_hold(
                (225, 39),
                lambda c, r: sent.append((c, r)),
                active=active,
                settled=ready,
                binding=binding,
            )
            out.update(report=report, held=held)
            return ft.Text("held" if held else "shown")

        @ft.component
        def Parent() -> ft.Control:
            active, set_active = ft.use_state(True)
            ready, set_ready = ft.use_state(True)
            binding, set_binding = ft.use_state(1)
            mounted, set_mounted = ft.use_state(True)
            out.update(active=set_active, ready=set_ready, binding=set_binding, mounted=set_mounted)
            return Probe(active, ready, binding) if mounted else ft.Text("closed")

        app = await render_component(Parent)
        assert not out["held"]
        old_report = out["report"]
        old_report(225, 30)
        change = {
            "hide": ("active", False),
            "unready": ("ready", False),
            "replace": ("binding", 2),
            "unmount": ("mounted", False),
        }
        field, value = change[transition]
        out[field](value)
        await app.settle(rounds=1)
        old_report(225, 20)
        await asyncio.sleep(0.2)
        assert not sent
        if transition != "unmount":
            out["active"](True)
            out["ready"](True)
            await app.settle(rounds=1)
            out["report"](200, 32)
            await asyncio.sleep(0.18)
            assert sent == [(200, 32)]

    run(scenario())


def test_a_slow_resize_writer_is_serial_and_only_the_latest_waiting_grid_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_hooks, "RESIZE_SETTLE_S", 0.02)

    async def scenario() -> None:
        entered, finish = asyncio.Event(), asyncio.Event()
        sent: list[tuple[int, int]] = []
        busy = [0]

        async def forward(cols: int, rows: int) -> None:
            busy[0] += 1
            assert busy[0] == 1
            try:
                sent.append((cols, rows))
                if rows == 30:
                    entered.set()
                    await finish.wait()
            finally:
                busy[0] -= 1

        _, out = await _opened(monkeypatch, forward)
        sent.clear()
        out["report"](225, 30)
        await asyncio.wait_for(entered.wait(), 0.3)
        out["report"](225, 31)
        await asyncio.sleep(0.04)  # This settled size is queued behind the blocked write.
        out["report"](225, 32)
        finish.set()  # Before this latest size's quiet timer: do not send stale 31.
        await asyncio.sleep(0.01)
        assert sent == [(225, 30)]
        await asyncio.sleep(0.04)
        assert sent == [(225, 30), (225, 32)] and busy[0] == 0

    run(scenario())


def test_a_retired_writer_cannot_drain_the_replacement_bindings_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_hooks, "OPEN_HOLD_S", 0.05)
    monkeypatch.setattr(agent_hooks, "OPEN_SETTLE_S", 0.02)
    monkeypatch.setattr(agent_hooks, "RESIZE_SETTLE_S", 0.02)

    async def scenario() -> None:
        entered_old, entered_new = asyncio.Event(), asyncio.Event()
        finish_old, finish_new = asyncio.Event(), asyncio.Event()
        retired = asyncio.Event()
        calls: list[tuple[int, int]] = []
        out: dict[str, Any] = {}

        @ft.component
        def Probe() -> ft.Control:
            binding, set_binding = ft.use_state(1)

            async def forward(_cols: int, rows: int) -> None:
                calls.append((binding, rows))
                if binding == 1:
                    entered_old.set()
                    try:
                        await finish_old.wait()
                    except asyncio.CancelledError:
                        retired.set()
                        await finish_old.wait()  # Model cancellation-delayed I/O cleanup.
                elif rows == 40:
                    entered_new.set()
                    await finish_new.wait()

            report, _held = agent_hooks.use_open_hold((225, 39), forward, binding=binding)
            out.update(report=report, binding=set_binding)
            return ft.Text("probe")

        app = await render_component(Probe)
        try:
            out["report"](225, 30)
            await asyncio.wait_for(entered_old.wait(), 0.3)
            out["binding"](2)
            await app.settle(rounds=1)
            await asyncio.wait_for(retired.wait(), 0.3)
            out["report"](225, 40)
            await asyncio.wait_for(entered_new.wait(), 0.3)
            out["report"](225, 41)
            await asyncio.sleep(0.04)
            finish_old.set()
            await asyncio.sleep(0.01)
            assert calls == [(1, 30), (2, 40)]
            finish_new.set()
            await asyncio.sleep(0.03)
            assert calls == [(1, 30), (2, 40), (2, 41)]
        finally:
            finish_old.set()
            finish_new.set()

    run(scenario())
