"""Disposable real-PTY relay timing; no daemon, tunnel socket or service."""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import os
import pty
import selectors
import time
import tty
from pathlib import Path
from typing import Any

import pytest

from relaydeck.connector.attach import AttachSession
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.link import (
    OUTPUT_COALESCE_MAX_S,
    OUTPUT_COALESCE_S,
    ConnectorLink,
    LinkConfig,
)
from relaydeck.connector.source import (
    MAX_RELAY_BYTES,
    TUNNEL_MAX_OUTPUT,
    HerdrSource,
    TerminalRelay,
)
from tests.support.sync.scripted_transport import _ScriptedTransport


class _QuietSubscription:
    async def __anext__(self) -> Any:
        await asyncio.Future()

    async def aclose(self) -> None:
        pass


class _AliveProcess:
    def poll(self) -> None:
        return None


def _write_repaint(fd: int, data: bytes) -> None:
    """Fragmented producer wakes on writable, never on an idle sleep."""
    deadline = time.monotonic() + 3
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        at = 0
        while at < len(data):
            try:
                at += os.write(fd, data[at : at + 1024])
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AssertionError("PTY producer stalled") from None
                selector.select(max(0, deadline - time.monotonic()))


def _batches_close_only_at_quiet_or_cap(
    reads: list[tuple[float, int]], flushed: list[float], sizes: list[int]
) -> None:
    """A repaint ships as one batch unless its pieces fell silent for the quiet gap or kept
    arriving past the cap. Reads are stamped before they wake the forwarder and batches at its
    close decision; a piece read after that decision joins the batch late and is not part of
    the gap. The slack covers the loop's timer resolution only."""
    ends = list(itertools.accumulate(size for _, size in reads))
    end = 0
    for flush, size in zip(flushed[:-1], sizes[:-1], strict=True):
        start, end = end, end + size
        pieces = [at for (at, _), total in zip(reads, ends, strict=True) if start < total <= end]
        assert pieces, (reads, sizes)
        window = [at for at in pieces if at <= flush]
        if not window:
            continue
        quiet = flush - window[-1]
        held = flush - window[0]
        assert quiet >= OUTPUT_COALESCE_S - 0.001 or held >= OUTPUT_COALESCE_MAX_S - 0.001, (
            quiet,
            held,
        )


@pytest.mark.parametrize("slow_metadata", [False, True])
@pytest.mark.parametrize("repeat", range(3))
def test_real_pty_repaint_forwarded_without_idle_staircase(
    tmp_path: Path, slow_metadata: bool, repeat: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector_sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep(delay: float, result: Any = None) -> Any:
        task = asyncio.current_task()
        owner = getattr(task.get_coro(), "__qualname__", "") if task is not None else ""
        if owner.endswith("_collect_pty"):
            collector_sleeps.append(delay)
        return await real_sleep(delay, result)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def scenario() -> None:
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        os.set_blocking(slave, False)
        session = AttachSession(["disposable-pty"])
        session._fd = master
        session._proc = _AliveProcess()  # type: ignore[assignment]
        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        source._attached["shell:t1"] = (session, "t1", "")
        source._epochs = {"t1": "e1"}
        source._epoch_kinds = {"t1": "shell"}
        source._snapshot = {"panes": []}
        source._subscription = _QuietSubscription()  # type: ignore[assignment]
        source._pane_subscription = _QuietSubscription()  # type: ignore[assignment]
        stop = asyncio.Event()
        repaint = (b"\x1b[H\x1b[2Jsynthetic repaint\r\n" * 400)[:10240]
        assert len(repaint) == 10240
        frames: list[bytes] = []
        timings: list[float] = []
        flushed: list[float] = []
        decided: list[float] = []
        reads: list[tuple[float, int]] = []
        done = asyncio.Event()
        start = time.monotonic()
        pump = source.pump_live

        def pump_live() -> int:
            stamp = time.monotonic()
            collected = pump()
            if collected:
                reads.append((stamp, collected))
            return collected

        source.pump_live = pump_live  # type: ignore[method-assign]

        class Transport(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                await super().send_text(text)
                doc = json.loads(text)
                if doc["kind"] == "terminal.output":
                    assert (doc["terminal_id"], doc["epoch"]) == ("t1", "e1")
                    frames.append(base64.b64decode(doc["data_b64"]))
                    flushed.append(decided[-1])
                    timings.append((time.monotonic() - start) * 1000)
                    if sum(map(len, frames)) >= len(repaint):
                        done.set()
                        stop.set()

        transport = Transport([])
        link = ConnectorLink(
            LinkConfig(
                gateway_url="http://127.0.0.1:9",
                host_id="h1",
                host_token="synthetic",
                journal_path=str(tmp_path / "connector.db"),
                ping_interval_s=0,
            ),
            source,
        )
        link._last_epochs = {"t1": "e1"}
        push_updates = link._push_terminal_updates

        async def push_decided(port: Any) -> None:
            decided.append(time.monotonic())
            await push_updates(port)

        link._push_terminal_updates = push_decided
        metadata_started = asyncio.Event()
        metadata_release = asyncio.Event()
        if slow_metadata:

            async def collect(_timeout: float) -> list[dict[str, Any]]:
                return [{"event_id": "metadata-probe"}]

            async def refresh() -> None:
                metadata_started.set()
                await metadata_release.wait()

            async def snapshot(port: Any) -> None:
                await port.send_text(json.dumps({"kind": "snapshot"}))

            source.collect_live = collect  # type: ignore[method-assign]
            source.refresh = refresh  # type: ignore[method-assign]
            link._push_snapshot = snapshot  # type: ignore[method-assign]
        task = asyncio.create_task(link._serve(transport, stop))
        if slow_metadata:
            await asyncio.wait_for(metadata_started.wait(), 1)
            start = time.monotonic()
        producer = asyncio.create_task(asyncio.to_thread(_write_repaint, slave, repaint))
        ready = asyncio.create_task(done.wait())
        try:
            completed, _ = await asyncio.wait(
                {ready, task, producer}, timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            if task in completed:
                task.result()
            if producer in completed:
                producer.result()
            await asyncio.wait_for(ready, 3)
            await producer
            metadata_release.set()
            assert await asyncio.wait_for(task, 1) == "stop"
            print(
                json.dumps(
                    {
                        "bytes": len(repaint),
                        "slow_metadata": slow_metadata,
                        "repeat": repeat,
                        "chunks": len(frames),
                        "sizes": list(map(len, frames)),
                        "forward_ms": [round(t, 3) for t in timings],
                    }
                )
            )
            assert b"".join(frames) == repaint
            # Wall time is the scheduler's; a starved runner forwards late without being wrong.
            # The staircase is caught structurally: the collector yields, never sleeps on a
            # timer, and a batch boundary needs a quiet gap or the cap.
            assert timings[-1] < 1000
            assert set(collector_sleeps) == {0}
            _batches_close_only_at_quiet_or_cap(reads, flushed, list(map(len, frames)))
            if slow_metadata:
                kinds = [json.loads(text)["kind"] for text in transport.sent]
                assert (
                    kinds.index("terminal.output") < kinds.index("snapshot") < kinds.index("event")
                )
        finally:
            stop.set()
            task.cancel()
            ready.cancel()
            await asyncio.gather(task, producer, ready, return_exceptions=True)
            session._proc = None
            session.close()
            os.close(slave)
            await source.close()
            link._journal.close()

    asyncio.run(scenario())


def test_upstream_eof_cancels_readiness_and_output_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        class ClosedSubscription(_QuietSubscription):
            async def __anext__(self) -> Any:
                raise StopAsyncIteration

        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        source._snapshot = {"panes": []}
        source._subscription = ClosedSubscription()  # type: ignore[assignment]
        source._pane_subscription = _QuietSubscription()  # type: ignore[assignment]
        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        try:
            assert (
                await asyncio.wait_for(link._serve(_ScriptedTransport([]), asyncio.Event()), 1)
                == "drop"
            )
            assert link._source_stale
            assert not any(
                t.get_coro().__qualname__.endswith(
                    ("_collect_pty", "_forward_pty", "_serve_messages", "wait_changed")
                )
                for t in asyncio.all_tasks()
            )
        finally:
            await source.close()
            link._journal.close()

    asyncio.run(scenario())


def test_relay_reservation_keeps_inflight_and_queued_within_original_bound() -> None:
    relay = TerminalRelay()
    relay.script("t1", "e1", b"a" * TUNNEL_MAX_OUTPUT)
    held = relay.drain(reserve=True)
    relay.script("t2", "e2", b"b" * (MAX_RELAY_BYTES - TUNNEL_MAX_OUTPUT))
    # Over the bound: nothing is staged and nothing raises. The terminal is marked for resync.
    assert relay.script("t1", "e1", b"x") == 0
    assert relay.overflowed == {"t1": "e1"} and relay.overflow_count == 1
    assert relay.load <= MAX_RELAY_BYTES
    relay.release(sum(len(frame.data) for frame in held))
    assert relay.script("t1", "e1", b"x") == 0  # still waiting for its resync
    relay.clear_overflow(["t1"])
    assert relay.script("t1", "e1", b"x") == 1


def test_nonblocking_drain_coalesces_until_eagain_and_yields_at_64k(tmp_path: Path) -> None:
    class Session(AttachSession):
        def __init__(self) -> None:
            super().__init__(["synthetic"])
            self.pieces = [b"a" * 1024, b"b" * 4096, b""]
            self.calls: list[int] = []

        def read(self, max_bytes: int = 65536, timeout: float = 5) -> bytes:
            return self.read_available(max_bytes)

        def read_available(self, max_bytes: int = 65536) -> bytes:
            self.calls.append(max_bytes)
            return self.pieces.pop(0)

    source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
    session = Session()
    source._attached["shell:t1"] = (session, "t1", "")
    assert source.pump_attached(lambda _id: "e1") == 5120
    assert source.drain_outputs() == [("t1", "e1", b"a" * 1024 + b"b" * 4096)]
    assert session.calls == [65536, 64512, 60416]
    session.pieces = [b"z" * TUNNEL_MAX_OUTPUT, b"next turn"]
    session.calls.clear()
    assert source.pump_attached(lambda _id: "e1") == TUNNEL_MAX_OUTPUT
    assert session.calls == [65536] and session.pieces == [b"next turn"]


def test_real_pty_readiness_cleans_up_and_never_reads_an_unmapped_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        session = AttachSession(["disposable"])
        session._fd = master
        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        source._attached["shell:t1"] = (session, "t1", "")
        loop = asyncio.get_running_loop()
        added: list[int] = []
        removed: list[int] = []
        add, remove = loop.add_reader, loop.remove_reader

        def add_reader(fd: int, callback: Any, *args: Any) -> None:
            added.append(fd)
            add(fd, callback, *args)

        def remove_reader(fd: int) -> bool:
            removed.append(fd)
            return remove(fd)

        monkeypatch.setattr(loop, "add_reader", add_reader)
        monkeypatch.setattr(loop, "remove_reader", remove_reader)
        waiter = asyncio.create_task(source.wait_output_ready())
        try:
            await asyncio.sleep(0)
            os.write(slave, b"unmapped")
            await asyncio.sleep(0)
            assert not waiter.done() and not added
            assert source.pump_live() == 0
            source._epochs = {"t1": "e1"}
            source._output_changed.set()
            await asyncio.wait_for(waiter, 1)
            await source.wait_output_ready()  # Consume the table-change notification.
            await asyncio.wait_for(source.wait_output_ready(), 1)
            assert added == removed == [master]
            assert session.read(timeout=0) == b"unmapped"
            waiter = asyncio.create_task(source.wait_output_ready())
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert added == removed == [master, master]
            assert not any("wait_changed" in t.get_coro().__qualname__ for t in asyncio.all_tasks())
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            session.close()
            os.close(slave)

    asyncio.run(scenario())


def test_closed_pty_wait_cannot_remove_replacement_descriptor_reader(tmp_path: Path) -> None:
    async def scenario() -> None:
        master, slave = pty.openpty()
        session = AttachSession(["disposable"])
        session._fd = master
        os.set_blocking(master, False)
        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        source._pool.acquire("shell:t1", lambda: session, 0)
        source._attached["shell:t1"] = (session, "t1", "")
        source._epochs = {"t1": "e1"}
        loop = asyncio.get_running_loop()
        waiter = asyncio.create_task(source.wait_output_ready())
        replacement = -1
        write = -1
        try:
            await asyncio.sleep(0)
            source.drop_terminal("t1")
            assert session._fd is None
            replacement, write = os.pipe()
            if replacement != master:
                os.dup2(replacement, master)
                os.close(replacement)
                replacement = master
            received: asyncio.Future[bytes] = loop.create_future()

            def read_replacement() -> None:
                data = os.read(replacement, 1)
                if not received.done():
                    received.set_result(data)

            loop.add_reader(replacement, read_replacement)
            await asyncio.wait_for(waiter, 1)
            os.write(write, b"x")
            assert await asyncio.wait_for(received, 1) == b"x"
            assert source._cancel_output_wait is None
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            if replacement >= 0:
                loop.remove_reader(replacement)
                os.close(replacement)
            if write >= 0:
                os.close(write)
            session.close()
            os.close(slave)
            await source.close()

    asyncio.run(scenario())


def test_output_consumer_preserves_order_and_drops_replaced_epoch_mid_send(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        link._last_epochs = {"t1": "e1", "t2": "e2"}
        source.script_output("t1", "e1", b"a" * (64 * 1024))
        source.script_output("t2", "e2", b"utf8\xe2\x86")
        source.script_output("t2", "e2", b"\x91\x1b[31m")

        class Transport(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                await super().send_text(text)
                if len(self.sent) == 1:
                    # The accepted first frame cannot be recalled. The rest
                    # of the old batch must not cross a changed route.
                    link._last_epochs["t1"] = "replacement"
                    await asyncio.sleep(0)

        transport = Transport([])
        try:
            await link._push_outputs(transport)
            docs = [json.loads(text) for text in transport.sent]
            assert [(d["terminal_id"], d["epoch"]) for d in docs] == [("t1", "e1"), ("t2", "e2")]
            assert base64.b64decode(docs[0]["data_b64"]) == b"a" * (32 * 1024)
            assert base64.b64decode(docs[1]["data_b64"]) == b"utf8\xe2\x86\x91\x1b[31m"
            assert source.relay._reserved_bytes == 0
            assert all(len(text.encode()) <= 65536 for text in transport.sent)
        finally:
            link._journal.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="PTY backpressure was removed: it could spin the event loop")
def test_blocked_send_does_not_block_producer_and_overflow_fails_supervised(tmp_path: Path) -> None:
    async def scenario() -> None:
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        class Session(AttachSession):
            def __init__(self) -> None:
                super().__init__(["synthetic"])
                self.first = True
                self.collected = 0

            def read(self, max_bytes: int = 65536, timeout: float = 5) -> bytes:
                return self.read_available(max_bytes)

            def read_available(self, max_bytes: int = 65536) -> bytes:
                if self.first:
                    self.first = False
                    self.collected += 1
                    return b"a"
                if not blocked.is_set():
                    return b""
                self.collected += max_bytes
                return b"b" * max_bytes

        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        session = Session()
        source._attached["shell:t1"] = (session, "t1", "")
        source._epochs = {"t1": "e1"}

        async def ready() -> None:
            await blocked.wait()
            await asyncio.sleep(0)

        source.wait_output_ready = ready  # type: ignore[method-assign]

        class Transport(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                assert json.loads(text)["kind"] == "terminal.output"
                blocked.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        link._last_epochs = {"t1": "e1"}

        async def idle(*_args: Any, **_kwargs: Any) -> str:
            await asyncio.Future()
            return "stop"

        link._serve_messages = idle  # type: ignore[method-assign]
        try:
            # A blocked tunnel is backpressure, not a failure: the pump stops reading the PTY
            # (the bytes stay there), the link keeps running and nothing overflows.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(link._serve(Transport([]), asyncio.Event()), 1)
            assert session.collected <= MAX_RELAY_BYTES + 64 * 1024
            assert source.relay.overflow_count == 0 and not source.relay.overflowed
            assert sum(len(f.data) for f in source.relay._frames) <= MAX_RELAY_BYTES
            assert source.relay._reserved_bytes == 0 and cancelled.is_set()
            assert not any(
                t.get_coro().__qualname__.endswith(
                    ("_collect_pty", "_forward_pty", "_serve_messages")
                )
                for t in asyncio.all_tasks()
            )
        finally:
            link._journal.close()

    asyncio.run(scenario())


def test_bootstrap_refresh_and_live_consumer_cannot_interleave_terminal_bytes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = HerdrSource(HerdrAdapter(socket_path=str(tmp_path / "unused.sock")))
        source.script_output("t1", "e1", b"a" * 65536)
        link = ConnectorLink(
            LinkConfig("http://127.0.0.1:9", "h1", "synthetic", str(tmp_path / "c.db")), source
        )
        link._last_epochs = {"t1": "e1"}
        entered, release = asyncio.Event(), asyncio.Event()

        class Transport(_ScriptedTransport):
            async def send_text(self, text: str) -> None:
                await super().send_text(text)
                if len(self.sent) == 1:
                    entered.set()
                    await release.wait()

        transport = Transport([])
        first = asyncio.create_task(link._push_outputs(transport))
        second: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            source.script_output("t1", "e1", b"later")
            second = asyncio.create_task(link._push_outputs(transport))
            await asyncio.sleep(0)
            assert len(transport.sent) == 1
            release.set()
            await asyncio.gather(first, second)
            data = [base64.b64decode(json.loads(t)["data_b64"]) for t in transport.sent]
            assert data == [b"a" * 32768, b"a" * 32768, b"later"]
            assert source.relay._reserved_bytes == 0
            source._terminal_resets = {"removed": "old", "t1": "e1"}
            await link._push_terminal_updates(transport)
            resets = [json.loads(t) for t in transport.sent if "terminal.reset_required" in t]
            assert [(d["terminal_id"], d["epoch"]) for d in resets] == [("t1", "e1")]
        finally:
            first.cancel()
            if second is not None:
                second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
            else:
                await asyncio.gather(first, return_exceptions=True)
            link._journal.close()

    asyncio.run(scenario())
