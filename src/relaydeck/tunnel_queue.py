"""Connection-local bounded admission and a single ordered tunnel writer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.contracts.tunnel import SEND_TIMEOUT_S, SnapshotIntake

MAX_FRAMES = 4096  # More than one second of 14 terminals at the 5 ms output cadence.
MAX_BYTES = 2 * 1024 * 1024
CONTROL_RESERVE = 4


class TunnelCapacityError(ConnectionError):
    """A bounded connection lane cannot accept more work; fail the connection."""


class BoundedInbox[T]:
    """Count the active item too; capacity never increases merely by dequeueing."""

    def __init__(
        self,
        frames: int = MAX_FRAMES,
        byte_limit: int = MAX_BYTES,
        control_reserve: int = CONTROL_RESERVE,
    ) -> None:
        self.queue: asyncio.Queue[tuple[T, int, float, bool, int]] = asyncio.Queue()
        self.control_reserve = control_reserve
        self.frames = frames
        self.byte_limit = byte_limit
        self.count = self.bytes = self.controls = 0
        self.oldest_at = 0.0
        self._stamps: dict[int, float] = {}

    def put(self, value: T, size: int, *, control: bool = False, frames: int = 1) -> None:
        if frames < 1 or size < 0 or self.bytes + size > self.byte_limit:
            raise TunnelCapacityError("tunnel_queue_full")
        if self.count - self.controls + frames > self.frames and not control:
            raise TunnelCapacityError("tunnel_queue_full")
        if control and (size > 4096 or self.controls + frames > self.control_reserve):
            raise TunnelCapacityError("tunnel_queue_full")
        stamp = time.monotonic()
        was_empty = self.count == 0
        entry = (value, size, stamp, control, frames)
        self.queue.put_nowait(entry)
        self._stamps[id(entry)] = stamp
        self.count += frames
        self.bytes += size
        self.controls += frames if control else 0
        if was_empty:
            self.oldest_at = stamp

    async def get(self) -> tuple[T, int, float, bool, int]:
        return await self.queue.get()

    @property
    def oldest_age(self) -> float:
        return max(0, time.monotonic() - self.oldest_at) if self.oldest_at else 0

    def finish(self, entry: tuple[T, int, float, bool, int]) -> None:
        self.count -= entry[4]
        self.bytes -= entry[1]
        self.controls -= entry[4] if entry[3] else 0
        self.queue.task_done()
        self._stamps.pop(id(entry), None)
        self.oldest_at = next(iter(self._stamps.values()), 0)


@dataclass
class _Write:
    texts: tuple[str, ...]
    done: asyncio.Future[bool]
    before: Callable[[], Awaitable[bool]] | None


class TunnelWriter:
    """Actual-send receipts, bounded batches and no retry of uncertain writes.

    `before` runs in the sole writer immediately before its wire attempt, for
    durable dispatch markers/deadlines. A snapshot batch stays contiguous.
    """

    def __init__(self, send: Callable[[str], Awaitable[None]], current: Callable[[], bool]) -> None:
        self.inbox: BoundedInbox[_Write] = BoundedInbox()
        self._send, self._current = send, current
        self._closed = False
        self.phase = "idle"

    async def send(
        self,
        text: str,
        *,
        before: Callable[[], Awaitable[bool]] | None = None,
        control: bool = False,
    ) -> bool:
        return await self.batch((text,), before=before, control=control)

    async def batch(
        self,
        texts: tuple[str, ...],
        *,
        before: Callable[[], Awaitable[bool]] | None = None,
        control: bool = False,
    ) -> bool:
        future = self.post(texts, before=before, control=control)
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                return await asyncio.shield(future)
        except BaseException:
            # Consume late failures without abandoning an in-flight durable marker.
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            raise

    def post(
        self,
        texts: tuple[str, ...],
        *,
        before: Callable[[], Awaitable[bool]] | None = None,
        control: bool = False,
    ) -> asyncio.Future[bool]:
        """Immediate bounded admission for the reader; its writer task owns failures."""
        if self._closed or not self._current():
            raise ConnectionError("tunnel_closed")
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        try:
            self.inbox.put(
                _Write(texts, future, before),
                sum(len(t.encode()) for t in texts),
                control=control,
                frames=1,  # An atomic snapshot batch is one admission; bytes still count fully.
            )
        except TunnelCapacityError:
            self.close()
            raise
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        return future

    async def run(self) -> None:
        try:
            while True:
                try:
                    entry = await self.inbox.get()
                except asyncio.QueueShutDown:
                    raise ConnectionError("tunnel_closed") from None
                work = entry[0]
                try:
                    self.phase = "prepare"
                    if not self._current():
                        raise ConnectionError("tunnel_superseded")
                    ready = work.before is None or await work.before()
                    if not self._current():
                        raise ConnectionError("tunnel_superseded")
                    if ready:
                        self.phase = "send"
                        for text in work.texts:
                            if self._closed or not self._current():
                                raise ConnectionError("tunnel_superseded")
                            async with asyncio.timeout(SEND_TIMEOUT_S):
                                await self._send(text)
                    work.done.set_result(ready)
                except BaseException as exc:
                    work.done.set_exception(ConnectionError("tunnel_write_failed"))
                    raise exc
                finally:
                    self.inbox.finish(entry)
                    self.phase = "idle"
        finally:
            self.close()

    def close(self) -> None:
        self._closed = True
        while not self.inbox.queue.empty():
            entry = self.inbox.queue.get_nowait()
            if not entry[0].done.done():
                entry[0].done.set_exception(ConnectionError("tunnel_closed"))
            self.inbox.finish(entry)
        self.inbox.queue.shutdown()


async def cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """Join every connection-owned task; no detached workers survive a tunnel."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class TunnelIntake:
    """One bounded snapshot assembly, admitted atomically alongside ordered documents."""

    def __init__(self, inbox: BoundedInbox[Any], snapshots: SnapshotIntake) -> None:
        self.inbox, self.snapshots = inbox, snapshots
        self.batch_bytes = 0

    def put(self, doc: dict[str, Any], size: int, host: str, now: float) -> None:
        self.snapshots.check_deadline(now)
        kind = doc.get("kind")
        if kind in ("snapshot.begin", "snapshot.chunk", "snapshot.end"):
            self.batch_bytes += size
            if (
                self.inbox.bytes + self.batch_bytes > self.inbox.byte_limit
                or self.inbox.count >= self.inbox.frames
            ):
                raise TunnelCapacityError("tunnel_queue_full")
            snapshot = self.snapshots.accept(doc, host, now)
            if snapshot is not None:
                self.inbox.put(
                    {"kind": "snapshot", "host_id": host, "snapshot": snapshot}, self.batch_bytes
                )
                self.batch_bytes = 0
        else:
            if self.snapshots.active:
                raise ValueError("snapshot_interleaved")
            self.inbox.put(doc, size)
