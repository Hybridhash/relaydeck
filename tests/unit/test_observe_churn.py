"""stable bounded observers and coalesced monotonic recovery."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from relaydeck.connector import source as module
from relaydeck.connector.attach import AttachmentPool, AttachSession, ObserveSession
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource


class Clock:
    ms = 0

    def __call__(self) -> float:
        return self.ms / 1000


class FakeObserve(ObserveSession):
    instances: list[FakeObserve] = []
    fail_spawn = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.alive = False
        self.closes = 0
        self.output = b""
        self.instances.append(self)

    def spawn(self) -> None:
        if self.fail_spawn:
            raise OSError("untrusted private exception content")
        self.alive = True

    @property
    def running(self) -> bool:
        return self.alive

    def read(self, max_bytes: int = 65536, timeout: float = 0) -> bytes:
        data, self.output = self.output[:max_bytes], self.output[max_bytes:]
        return data

    def read_available(self, max_bytes: int = 65536) -> bytes:
        return self.read(max_bytes)

    def close(self, timeout: float = 5) -> int | None:
        self.closes += 1
        if self.alive:
            self.exit_status = -15  # Same intentional termination as the real pool.
        self.alive = False
        return self.exit_status


@pytest.fixture(autouse=True)
def fake_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeObserve.instances = []
    FakeObserve.fail_spawn = False
    monkeypatch.setattr(module, "ObserveSession", FakeObserve)


def routes(count: int, epoch: str = "e1") -> dict[str, dict[str, str]]:
    return {
        f"t{i}": {"epoch": epoch, "kind": "agent" if i % 2 else "shell", "mode": "observe"}
        for i in range(count)
    }


def test_six_observes_preserve_outputs_and_control_budget() -> None:
    source = HerdrSource(HerdrAdapter(), subscriptions=[])
    table = routes(6)
    try:
        for _ in range(100):
            assert source.note_epochs(table) == {}
            assert source.pump_live() == 0
        assert len(FakeObserve.instances) == 6
        assert all(s.running and s.closes == 0 for s in FakeObserve.instances)
        assert len(source._pool) == 0 and len(source._observer_pool) == 6
        assert source.drain_terminal_resets() == {}
        for _, (session, terminal, _) in source._attached.items():
            session.output = terminal.encode()
            assert source.resize_terminal(terminal, "e1", 40, 20) == "stale or unavailable terminal"
            assert (
                asyncio.run(source.submit_terminal_input(terminal, "e1", "text", b"no"))
                == "stale or unavailable terminal"
            )
        assert source.pump_live() == 12
        assert {row[0] for row in source.drain_outputs()} == set(table)
    finally:
        asyncio.run(source.close())


def test_six_real_child_observes_do_not_receive_eviction_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[LocalObserve] = []

    class LocalObserve(ObserveSession):
        closes = 0

        def spawn(self) -> None:
            target = self.argv[4]
            self.argv = [
                sys.executable,
                "-c",
                "import base64,json,sys,time; "
                "print(json.dumps({'type':'terminal.frame','encoding':'ansi',"
                "'bytes':base64.b64encode(sys.argv[1].encode()).decode()}),flush=True); "
                "time.sleep(30)",
                target,
            ]
            super().spawn()
            instances.append(self)

        def close(self, timeout: float = 5) -> int | None:
            self.closes += 1
            return super().close(timeout)

    monkeypatch.setattr(module, "ObserveSession", LocalObserve)

    async def scenario() -> None:
        source = HerdrSource(HerdrAdapter(), subscriptions=[])
        table = routes(6)
        output: dict[str, bytes] = {}
        try:
            assert source.note_epochs(table) == {}
            async with asyncio.timeout(5):
                while set(output) != set(table):
                    source.pump_live()
                    for terminal, epoch, data in source.drain_outputs():
                        assert epoch == "e1"
                        output[terminal] = output.get(terminal, b"") + data
                    if set(output) != set(table):
                        await source.wait_output_ready()
            for _ in range(100):
                assert source.note_epochs(table) == {}
                source.pump_live()
            assert output == {terminal: terminal.encode() for terminal in table}
            assert len(instances) == 6
            assert all(session.running and session.closes == 0 for session in instances)
            assert source.drain_terminal_resets() == {}
        finally:
            await source.close()  # Only these six disposable synthetic children.
        assert all(not session.running and session.closes == 1 for session in instances)

    asyncio.run(scenario())


@pytest.mark.parametrize("slots", [1, 3, 5])
def test_n_plus_one_observes_never_evict_and_capacity_warning_is_coalesced(slots: int) -> None:
    clock = Clock()
    pool = AttachmentPool(slots=slots)
    source = HerdrSource(HerdrAdapter(), observer_pool=pool, retry_clock=clock)
    table = routes(slots + 1)
    try:
        first = source.note_epochs(table)
        assert len(first) == 1 and "capacity deferred" in next(iter(first.values()))
        for _ in range(1000):
            clock.ms += 100  # Cross repeated deadlines, not just one frozen instant.
            assert source.note_epochs(table) == {}
            source.pump_live()
        assert len(FakeObserve.instances) == slots and len(source._attached) == slots
        assert all(s.closes == 0 and s.running for s in FakeObserve.instances)
        assert source.drain_terminal_resets() == {}
        assert len(source._observe_retries) == 1
        # A released slot admits the deferred observer without waiting for a
        # polling cycle or evicting a second healthy tap.
        assert source.note_epochs({k: v for k, v in table.items() if k != "t0"}) == {}
        assert len(FakeObserve.instances) == slots + 1
        assert len(source._attached) == slots
        assert sum(s.closes for s in FakeObserve.instances) == 1
    finally:
        asyncio.run(source.close())


def test_real_exit_backoff_survives_gateway_epoch_rotation_and_caps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    source = HerdrSource(HerdrAdapter(), retry_clock=clock)
    try:
        assert source.note_epochs(routes(1)) == {}
        for index, delay in enumerate([1000, 2000, 4000, 8000, 16000, 30000, 30000]):
            session = FakeObserve.instances[-1]
            session.alive, session.exit_status = False, 1
            source.pump_live()
            assert source.drain_terminal_resets() == {"t0": f"e{index + 1}"}
            state = source._observe_retries["t0"]
            assert state.attempt == index + 1 and state.deadline_ms == clock.ms + delay
            # Reset-required causes this epoch change. It cannot authorize an
            # immediate process respawn or reset the consecutive failure budget.
            assert source.note_epochs(routes(1, f"e{index + 2}")) == {}
            assert len(FakeObserve.instances) == index + 1
            clock.ms += delay - 1
            assert source.note_epochs(routes(1, f"e{index + 2}")) == {}
            assert len(FakeObserve.instances) == index + 1
            clock.ms += 1
            source._retry_due_observers()
            assert len(FakeObserve.instances) == index + 2
        assert caplog.text.count("attach session died") == 1
        assert "timed_observe_retry" in caplog.text
        # Initial frames cannot reset a repeatedly crashing observer's budget;
        # thirty seconds of genuine liveness can.
        source.pump_live()
        clock.ms += 30000
        source.note_epochs(routes(1, "e8"))
        assert "t0" not in source._observe_retries
        FakeObserve.instances[-1].alive = False
        source.pump_live()
        assert source._observe_retries["t0"].attempt == 1
        assert caplog.text.count("attach session died") == 2
    finally:
        asyncio.run(source.close())


def test_spawn_failures_are_rate_limited_and_never_log_exception_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    source = HerdrSource(HerdrAdapter(), retry_clock=clock)
    FakeObserve.fail_spawn = True
    try:
        assert "spawn failed" in source.note_epochs(routes(1))["t0"]
        for _ in range(1000):
            assert source.note_epochs(routes(1)) == {}
        assert len(FakeObserve.instances) == 1
        clock.ms = 1000
        source._retry_due_observers()
        assert len(FakeObserve.instances) == 2
        assert source._observe_retries["t0"].deadline_ms == 3000
        assert "untrusted private exception content" not in caplog.text
    finally:
        asyncio.run(source.close())


def test_deferred_epoch_rotation_does_not_reset_other_capacity_deadlines() -> None:
    clock = Clock()
    source = HerdrSource(HerdrAdapter(), observer_pool=AttachmentPool(slots=1), retry_clock=clock)
    try:
        table = routes(3)
        assert set(source.note_epochs(table)) == {"t1", "t2"}
        clock.ms = 100
        table["t1"]["epoch"] = "e2"
        assert source.note_epochs(table) == {}
        assert source._observe_retries["t1"].deadline_ms == 1000
        assert source._observe_retries["t2"].deadline_ms == 1000
        assert source._observe_retries["t2"].attempt == 1
        assert len(FakeObserve.instances) == 1
    finally:
        asyncio.run(source.close())


@pytest.mark.parametrize("wake", ["lifecycle", "buffered"])
def test_busy_observer_does_not_starve_due_retry(wake: str) -> None:
    clock = Clock()
    source = HerdrSource(HerdrAdapter(), retry_clock=clock)
    try:
        source.note_epochs(routes(2))
        FakeObserve.instances[0].alive = False
        source.pump_live()
        clock.ms = 1000
        if wake == "buffered":
            source._output_changed.clear()
            FakeObserve.instances[1]._decoded.extend(b"busy terminal")
        asyncio.run(source.wait_output_ready())
        assert len(FakeObserve.instances) == 3
        assert source._attached["shell:t0"][0].running
    finally:
        asyncio.run(source.close())


def test_cancelled_retry_wait_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_OBSERVE_RETRY_DELAYS_MS", (2000,))

    async def scenario() -> None:
        source = HerdrSource(HerdrAdapter())
        try:
            source.note_epochs(routes(1))
            FakeObserve.instances[-1].alive = False
            source.pump_live()
            await source.wait_output_ready()
            wait = asyncio.create_task(source.wait_output_ready())
            await asyncio.sleep(0)
            assert source._cancel_output_wait is not None
            wait.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wait
            assert source._cancel_output_wait is None
            assert len(FakeObserve.instances) == 1
        finally:
            await source.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["drop", "close", "takeover"])
def test_pending_retry_never_survives_removal_shutdown_or_explicit_control(
    action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    source = HerdrSource(HerdrAdapter(), retry_clock=clock)
    try:
        source.note_epochs(routes(1))
        FakeObserve.instances[-1].alive = False
        source.pump_live()
        if action == "drop":
            source.note_epochs({})
        elif action == "close":
            asyncio.run(source.close())
        else:

            class Control(AttachSession):
                @property
                def running(self) -> bool:
                    return True

            def attach(terminal: str) -> None:
                source._attached[f"shell:{terminal}"] = (Control([]), terminal, "")

            monkeypatch.setattr(source, "attach_shell", attach)
            assert (
                source.note_epochs({"t0": {"kind": "shell", "mode": "control", "epoch": "e2"}})
                == {}
            )
        clock.ms = 1000000
        source._retry_due_observers()
        assert len(FakeObserve.instances) == 1
        assert not source._observe_retries
    finally:
        asyncio.run(source.close())


def test_quiet_retry_timer_wakes_without_gateway_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_OBSERVE_RETRY_DELAYS_MS", (20,))

    async def scenario() -> None:
        source = HerdrSource(HerdrAdapter())
        try:
            source.note_epochs(routes(1))
            FakeObserve.instances[-1].alive = False
            source.pump_live()
            await source.wait_output_ready()  # consume lifecycle wake
            async with asyncio.timeout(1):
                await source.wait_output_ready()  # deadline alone wakes and retries
            assert len(FakeObserve.instances) == 2
        finally:
            await source.close()

    asyncio.run(scenario())
