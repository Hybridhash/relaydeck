"""Bounded terminal output rings and per-viewer queues.

One append-only byte ring per upstream attachment. Offsets are absolute
within the current epoch and survive appends; retention is bounded by
bytes (2 MiB) AND age (5 minutes), whichever evicts first. Reads are
offset-addressed for reconnect resume and epoch-bound: a rotated epoch
(takeover) forces reset-required instead of serving a forged
tail from the new stream.

Viewer queues are per downstream connection, each bounded at 1 MiB. A
slow viewer overflows explicitly (flag + detail, never silent byte loss
presented as valid) while other viewers are unaffected.

Pure logic with injected millisecond clocks. No I/O, no sockets.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

RING_MAX_BYTES = 2 * 1024 * 1024
RING_MAX_AGE_MS = 5 * 60 * 1000
VIEWER_QUEUE_MAX_BYTES = 1024 * 1024
FIRST_REPLAY_MAX_BYTES = VIEWER_QUEUE_MAX_BYTES // 2
READ_CHUNK_MAX = 64 * 1024

STATUS_OK = "ok"
STATUS_STALE_OFFSET = "stale_offset"
STATUS_STALE_EPOCH = "stale_epoch"


@dataclass(frozen=True)
class RingRead:
    """One offset-addressed read. `chunks` is non-empty only when status
    is ok; stale reads carry no bytes, so a caller can never mistake a
    partial tail for a complete screen."""

    status: str
    chunks: tuple[tuple[int, bytes], ...]
    next_offset: int
    epoch: str


class TerminalRing:
    """Append-only bytes with absolute offsets and dual retention bounds."""

    def __init__(self, epoch: str, now_ms: int) -> None:
        self._epoch = epoch
        self._segments: deque[tuple[int, bytes, int]] = deque()
        self._retained = 0
        self._base = 0
        self._end = 0
        self._born_ms = now_ms

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def base_offset(self) -> int:
        return self._base

    @property
    def end_offset(self) -> int:
        return self._end

    @property
    def retained_bytes(self) -> int:
        return self._retained

    def append(self, data: bytes, now_ms: int) -> tuple[int, int]:
        """Append bytes; returns (start_offset, end_offset). Empty appends
        are a no-op returning the live edge twice."""
        if not data:
            return (self._end, self._end)
        start = self._end
        self._segments.append((start, data, now_ms))
        self._end += len(data)
        self._retained += len(data)
        self._evict(now_ms)
        return (start, self._end)

    def _evict(self, now_ms: int) -> None:
        # Eviction is lazy on append: an idle ring holds its bytes (exact
        # for a preserved emulator) until the next publish reaps them.
        while len(self._segments) > 1:
            oldest_ms = self._segments[0][2]
            if now_ms - oldest_ms < RING_MAX_AGE_MS and self._retained <= RING_MAX_BYTES:
                break
            _, data, _ = self._segments.popleft()
            self._retained -= len(data)
            self._base += len(data)
        if not self._segments:
            return
        offset, data, stamp = self._segments[0]
        if now_ms - stamp >= RING_MAX_AGE_MS:
            self._segments.popleft()
            self._retained -= len(data)
            self._base += len(data)
        elif self._retained > RING_MAX_BYTES:
            # A single append larger than the whole ring keeps only its tail.
            over = self._retained - RING_MAX_BYTES
            self._segments[0] = (offset + over, data[over:], stamp)
            self._retained -= over
            self._base += over

    def read(self, epoch: str, after_offset: int) -> RingRead:
        """Read retained bytes at/after `after_offset`, chunked for the
        wire. Stale reads carry no bytes: the caller must drive an
        explicit reset/reattach, never render a partial tail."""
        if epoch != self._epoch:
            return RingRead(STATUS_STALE_EPOCH, (), self._end, self._epoch)
        if after_offset < self._base or after_offset > self._end:
            return RingRead(STATUS_STALE_OFFSET, (), self._end, self._epoch)
        chunks: list[tuple[int, bytes]] = []
        for offset, data, _ in self._segments:
            seg_end = offset + len(data)
            if seg_end <= after_offset:
                continue
            skip = max(0, after_offset - offset)
            piece = data[skip:]
            pos = offset + skip
            while piece:
                head, piece = piece[:READ_CHUNK_MAX], piece[READ_CHUNK_MAX:]
                chunks.append((pos, head))
                pos += len(head)
        return RingRead(STATUS_OK, tuple(chunks), self._end, self._epoch)

    def rotate(self, new_epoch: str) -> None:
        """Bind a new epoch after takeover: history is dropped and offsets
        restart, so old offsets can never address new bytes."""
        self._epoch = new_epoch
        self._segments.clear()
        self._retained = 0
        self._base = 0
        self._end = 0


@dataclass
class ViewerQueue:
    """Bounded FIFO for one downstream connection. Overflow is explicit:
    `overflowed` latches with the first dropped offset and every further
    push is refused until `reset` (reattach)."""

    viewer_id: str
    epoch: str
    _pending: deque[tuple[int, bytes]] = field(default_factory=deque)
    _pending_bytes: int = 0
    overflowed: bool = False
    overflow_at_offset: int = 0
    reset_pending: bool = False

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def push(self, offset: int, data: bytes) -> bool:
        if self.overflowed or self.reset_pending:
            return False
        if self._pending_bytes + len(data) > VIEWER_QUEUE_MAX_BYTES:
            self.overflowed = True
            self.overflow_at_offset = offset
            return False
        self._pending.append((offset, data))
        self._pending_bytes += len(data)
        return True

    def pop_all(self) -> tuple[tuple[int, bytes], ...]:
        chunks = tuple(self._pending)
        self._pending.clear()
        self._pending_bytes = 0
        return chunks

    def mark_reset(self, epoch: str) -> None:
        """Epoch rotated upstream: queued bytes are stale, drop them and
        require an explicit reattach (never deliver a mixed stream)."""
        self.epoch = epoch
        self._pending.clear()
        self._pending_bytes = 0
        self.overflowed = False
        self.reset_pending = True

    def reset(self, epoch: str) -> None:
        """Reattach complete: accept the new live stream from here."""
        self.epoch = epoch
        self._pending.clear()
        self._pending_bytes = 0
        self.overflowed = False
        self.reset_pending = False


class StreamFanout:
    """One ring plus its live viewer queues. Publishing appends to the
    ring once and offers the same bytes to every viewer; a slow viewer
    overflows alone while the ring and other viewers are unaffected."""

    def __init__(self, epoch: str, now_ms: int) -> None:
        self.ring = TerminalRing(epoch, now_ms)
        self._viewers: dict[str, ViewerQueue] = {}

    def subscribe(self, viewer_id: str) -> ViewerQueue:
        queue = ViewerQueue(viewer_id, self.ring.epoch)
        self._viewers[viewer_id] = queue
        return queue

    def unsubscribe(self, viewer_id: str) -> None:
        self._viewers.pop(viewer_id, None)

    def viewer(self, viewer_id: str) -> ViewerQueue | None:
        return self._viewers.get(viewer_id)

    @property
    def viewer_count(self) -> int:
        """Live subscriptions (must return to zero)."""
        return len(self._viewers)

    def publish(self, data: bytes, now_ms: int) -> tuple[int, int]:
        start, end = self.ring.append(data, now_ms)
        if data:
            for queue in self._viewers.values():
                queue.push(start, data)
        return (start, end)

    def rotate(self, new_epoch: str) -> None:
        self.ring.rotate(new_epoch)
        for queue in self._viewers.values():
            queue.mark_reset(new_epoch)


class RingStore:
    """Fanouts keyed by attachment id. `rotate` creates-or-rotates so the
    first attach opens a ring and every takeover rebinds it."""

    def __init__(self) -> None:
        self._fanouts: dict[str, StreamFanout] = {}

    def __len__(self) -> int:
        return len(self._fanouts)

    def rotate(self, attachment_id: str, epoch: str, now_ms: int) -> StreamFanout:
        fanout = self._fanouts.get(attachment_id)
        if fanout is None:
            fanout = StreamFanout(epoch, now_ms)
            self._fanouts[attachment_id] = fanout
        else:
            fanout.rotate(epoch)
        return fanout

    def get(self, attachment_id: str) -> StreamFanout | None:
        return self._fanouts.get(attachment_id)

    def drop(self, attachment_id: str) -> None:
        self._fanouts.pop(attachment_id, None)

    def viewer_count(self, attachment_id: str) -> int:
        """Live subscriptions on one fanout (0 when no fanout exists)."""
        fanout = self._fanouts.get(attachment_id)
        return fanout.viewer_count if fanout is not None else 0
