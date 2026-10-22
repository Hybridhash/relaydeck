"""Python side of the RelayDeck terminal extension control.

Transport only: bytes cross here, policy lives in
`relaydeck.domain.terminal` (ownership/generations) and the client input sessions.
Follows the `RawImage` DataChannel pattern:
Dart opens the channel on mount, Python captures it in
`on_data_channel_open`, output is acked cumulatively for backpressure.

No `from __future__ import annotations` in this module: Flet 1.0.3's
`get_event_field_type` reads raw `__annotations__` and only unwraps
`ForwardRef`, so a PEP 563 string annotation on `on_data_channel_open`
crashes live rendering with `[resolve error] ... tuple index out of
range`. Every annotation here must evaluate at def time (py>=3.13).
"""

import asyncio
import logging
import struct
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft
from flet.controls.layout_control import LayoutControl
from flet.data_channel import DataChannel, DataChannelOpenEvent

from relaydeck_terminal import protocol
from relaydeck_terminal.diagnostics import trace_lifecycle

_DIAG = logging.getLogger("relaydeck.client.typing_split")


class TerminalOverflowError(RuntimeError):
    """Raised when unacked output exceeds the viewer queue bound."""


# A pending resume probe: the arming epoch plus a future resolving to
# (native instance, dart resets, applied bytes), or None when a fresh
# viewer, reset, or newer resume supersedes the wait.
_ResumeWaiter = tuple[int, asyncio.Future[tuple[int, int, int] | None]]

_KEYBOARD_TRACE_EVENTS = {
    1: "button_down",
    2: "button_tap",
    3: "button_cancel",
    4: "focus_gained",
    5: "focus_lost",
    6: "request_keyboard",
    7: "release_keyboard",
    8: "input_enabled",
    9: "delayed_sample",
}


@dataclass(frozen=True)
class ResumeOutcome:
    """Result of rebinding a retained terminal to a replacement UI
    transport.

    status "resumed": the same native engine was verified (instance +
    reset identity) and the missing output suffix was redelivered
    exactly once; no viewer-generation bump, no controller stop, no
    resize, no input replay. reason "ok", or "ok_after_overflow" when
    the viewer had already overflowed during the outage (the session
    was already notified through the overflow path).
    status "incomplete": continuity is NOT proven (timeout, identity
    or offset mismatch, evicted journal, pre-existing protocol
    error); the caller must drive the honest reset-as-watcher
    recovery, never silent continuation. reason names the cause.
    status "noop": the terminal never attached; nothing to rebind.
    """

    status: str
    reason: str
    resent_bytes: int = 0


class _SnapshotWait:
    """Reassembly state for one in-flight snapshot request.

    Chunks are keyed by request id and placed by offset, so concurrent
    requests and out-of-order delivery both reassemble exactly; a dead
    request id (timeout, reset) drops its late chunks on the floor.
    """

    def __init__(self, fut: asyncio.Future[str]) -> None:
        self.fut = fut
        self.total: int | None = None
        self.declared = 0
        self.merged: bytearray | None = None
        self.filled: bytearray | None = None
        self.covered = 0

    def feed(self, total: int, offset: int, data: bytes) -> str | None:
        # Structural guards BEFORE allocation (the dispatcher validates
        # first; these are defense in depth and must never allocate on
        # violation either).
        if self.total is not None and self.total != total:
            return None
        if offset > total or offset + len(data) > total:
            return None
        if self.total is None:
            self.total = total
            self.declared = total
            self.merged = bytearray(total)
            self.filled = bytearray(total)
        assert self.merged is not None and self.filled is not None
        end = offset
        while end < offset + len(data):
            if not self.filled[end]:
                self.filled[end] = 1
                self.covered += 1
            end += 1
        self.merged[offset : offset + len(data)] = data
        if self.covered < self.total:
            return None
        return bytes(self.merged).decode("utf-8", errors="replace")


@ft.control("RelayDeckTerminal")  # type: ignore[arg-type]
class RelayDeckTerminal(LayoutControl):
    """Isolated imperative native bridge (terminal exception).

    The app holds ONE instance per terminal surface in a ref and returns
    the same instance on every render, so the DataChannel binding and
    byte counters survive re-renders. Output/input flow through methods,
    never through per-render prop patches.
    """

    input_enabled: bool = False
    """Mount-time input mode. Runtime switches use set_input_enabled(),
    which sends an INPUT_ENABLED packet immediately (no prop patch)."""

    max_lines: int = 5000
    """Bounded scrollback lines held by the Dart emulator."""

    font_size: float = 14.0

    on_data_channel_open: ft.EventHandler[DataChannelOpenEvent] | None = None

    def init(self) -> None:
        super().init()  # type: ignore[no-untyped-call]
        self._channel: DataChannel | None = None
        self._ready = asyncio.Event()
        self._inbox = bytearray()
        self._sent_bytes = 0
        self._acked_bytes = 0
        self._overflowed = False
        self._protocol_error: str | None = None
        self._selection_waiters: deque[asyncio.Future[str]] = deque()
        self._snapshot_next_id = 0
        self._snapshot_waiters: dict[int, _SnapshotWait] = {}
        self._snapshot_inflight_bytes = 0
        self._size_listeners: list[Callable[[int, int], None]] = []
        self._input_listeners: list[Callable[[int, bytes], None]] = []
        self._mode_listeners: list[Callable[[bool], None]] = []
        self._view_listeners: list[Callable[[protocol.ViewState], None]] = []
        self._keyboard_state_listeners: list[Callable[[bool], None]] = []
        self._pending_writes: deque[bytes] = deque()
        self._pending_bytes = 0
        self._viewer_generation = 0
        self._channel_id: int | None = None
        self._session_ref: weakref.ReferenceType[Any] | None = None
        self._logged_session_route: tuple[int, int] | None = None
        self._reset_count = 0
        self._reopen_listeners: list[Callable[[], None]] = []
        # Native instance id from the mount hello (req 0). Reset continuity is checked per report
        # against _reset_count, so the Dart count needs no stored baseline.
        self._native_instance: int | None = None
        self._resume_next_id = 0
        self._resume_epoch = 0
        self._resume_waiters: dict[int, _ResumeWaiter] = {}
        # Resend journal keyed by cumulative _sent_bytes, so a resume can redeliver the exact
        # missing suffix. Capped at the viewer queue bound: anything evicted already overflowed.
        self._journal: deque[bytes] = deque()
        self._journal_base: int = 0
        self._journal_bytes: int = 0
        # While a resume probe is in flight, new writes are held (still counted in _sent_bytes) and
        # flushed after the redelivered suffix, so a byte is either redelivered or flushed, never
        # both. Each holds its start offset (None for control). Capture/reset retire the batch.
        self._resume_hold = False
        self._resume_hold_epoch: int | None = None
        self._resume_held: deque[tuple[int | None, bytes]] = deque()
        self._resume_held_bytes = 0
        # Verification fence: armed while the binding is unverified and held after any failure,
        # until verified success, reset or fresh capture. Armed, effects-bearing inbound is dropped,
        # not queued.
        self._fence = False
        self._fence_drops = 0
        # Wheel input stays blocked after cancel_scroll() until the ack for the latest sequence, so
        # wheel reports in flight when a key lands drop as stale. The u32 wire token wraps
        # separately and maps back through _cancel_outstanding.
        self._cancel_sent_seq = 0
        self._cancel_acked_seq = 0
        self._cancel_outstanding: dict[int, int] = {}
        self._blocked_drops = 0
        # Set on the first SCROLL_CANCELLED ack (the viewer sends one
        # unsolicited at mount): only then is CANCEL_SCROLL answered.
        # Write-once per term; the bundle cannot downgrade mid-session.
        self._scroll_cancel_supported = False
        self._transport_generation = 0
        self._bound_transport_generation = 0
        self._verified_transport_generation = 0
        self._remount: Callable[[], None] | None = None
        self._retired = False
        self._view_report_count = 0
        self._last_view_size = (0, 0)
        if self.on_data_channel_open is None:
            self.on_data_channel_open = self._capture_channel

    # -- app wiring ----------------------------------------------------
    @property
    def viewer_generation(self) -> int:
        return self._viewer_generation

    def invalidate_transport(self, generation: int) -> None:
        """Fence a retained viewer even while paused or without a PTY lease."""
        if generation != self._transport_generation:
            self._transport_generation = generation
            self._verified_transport_generation = -1
            self._resume_epoch += 1
            self._abort_resume_waiters()
            self._cancel_acked_seq = self._cancel_sent_seq
            self._cancel_outstanding.clear()
        self._resume_fence = True

    def verify_transport(self, generation: int) -> bool:
        if generation != self._transport_generation or not self.channel_ready:
            return False
        self._verified_transport_generation = generation
        return True

    @property
    def transport_verified(self) -> bool:
        return (
            self.channel_ready
            and not self.resume_unverified
            and self._verified_transport_generation == self._transport_generation
        )

    def set_remount_handler(self, handler: Callable[[], None] | None) -> None:
        self._remount = handler

    def request_remount(self) -> bool:
        if self._remount is None:
            return False
        self._remount()
        return True

    def retire_transport(self) -> None:
        """A deliberate fresh mount retires all callbacks on its old channel."""
        if self._channel is not None:
            self._channel.on_bytes(None)
            self._channel.close()
        self._channel = None
        self._ready.clear()
        self._resume_epoch += 1
        self._abort_resume_waiters()
        self._abort_viewer_waiters("viewer retired")
        self._retire_hold()
        self._resume_fence = True
        self._verified_transport_generation = -1
        self._retired = True

    @property
    def retired(self) -> bool:
        return self._retired

    def _channel_current(self) -> bool:
        if self._channel is None:
            return False
        if getattr(self._channel, "_closed", False):
            return False
        # The host-only capturing seam has no Flet channel identity.
        if self._channel_id is None:
            return self._session_ref is None
        try:
            return (
                self._bound_transport_generation == self._transport_generation
                and self._resolve_channel(self._channel_id) is self._channel
            )
        except (RuntimeError, AttributeError):
            return False

    def add_channel_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Notify on confirmed channel opens, including the first native mount."""
        self._reopen_listeners.append(listener)

        def remove() -> None:
            if listener in self._reopen_listeners:
                self._reopen_listeners.remove(listener)

        return remove

    def add_input_listener(self, listener: Callable[[int, bytes], None]) -> None:
        """Listener receives (origin, bytes); see protocol.ORIGIN_*."""
        self._input_listeners.append(listener)

    def add_size_listener(self, listener: Callable[[int, int], None]) -> None:
        self._size_listeners.append(listener)

    def add_mode_listener(self, listener: Callable[[bool], None]) -> None:
        """Listener receives the live engine `bracketedPasteMode` after
        every output write and reset (protocol.MODE_REPORT). Paste
        framing follows this engine truth; Python never re-parses the
        output bytes for 2004."""
        self._mode_listeners.append(listener)

    def add_view_state_listener(self, listener: Callable[[protocol.ViewState], None]) -> None:
        """Listener receives the live view state (alt buffer, at bottom,
        mouse reporting) whenever the Dart side reports a change
        (protocol.VIEW_REPORT). The scroll buttons use this to show
        position and to no-op in the alternate buffer."""
        self._view_listeners.append(listener)

    def add_keyboard_state_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Listener receives KEYBOARD_STATE: True when the native keyboard button armed
        the soft keyboard, False when it was hidden or focus was lost. Returns the
        unsubscribe callable."""
        self._keyboard_state_listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._keyboard_state_listeners:
                self._keyboard_state_listeners.remove(listener)

        return unsubscribe

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def protocol_error(self) -> str | None:
        return self._protocol_error

    # -- channel plumbing ----------------------------------------------
    def _capture_channel(self, e: DataChannelOpenEvent) -> None:
        if self._retired:
            return
        channel = self.get_data_channel(e.channel_id)
        try:
            self._session_ref = weakref.ref(self.page.session)
        except RuntimeError:
            pass
        _DIAG.info(
            "term_channel_open term=%s id=%s prev_id=%s same_obj=%s gen=%s",
            id(self) % 100000,
            e.channel_id,
            self._channel_id,
            self._channel is channel,
            self._viewer_generation,
        )
        if self._channel is channel and self._channel_id == e.channel_id:
            trace_lifecycle("channel_retained", viewer=id(self), generation=self._viewer_generation)
            return
        reopened = self._viewer_generation > 0
        if self._channel is not None:
            self._channel.on_bytes(None)
            self._channel.close()
        self._viewer_generation += 1
        generation = self._viewer_generation
        self._cancel_acked_seq = self._cancel_sent_seq
        self._cancel_outstanding.clear()
        self._channel_id = e.channel_id
        self._channel = channel
        self._bound_transport_generation = self._transport_generation
        if reopened:
            self.fence_fresh_viewer()
        self._install_channel_delivery(channel, asyncio.get_running_loop())
        self._ready.set()
        for listener in tuple(self._reopen_listeners):
            listener()
        trace_lifecycle(
            "channel_open",
            viewer=id(self),
            control=self._i,
            generation=generation,
            reopens=generation - 1,
        )
        self._flush_pending()

    def _install_channel_delivery(
        self, channel: DataChannel, loop: asyncio.AbstractEventLoop
    ) -> None:
        generation = self._viewer_generation

        def deliver(payload: bytes, resets: int) -> None:
            # Binding currency, not attempt currency: a callback queued
            # on a superseded channel or sync generation retires
            # instead of dispatching into its successor. Capture sync generation at
            # arrival; the binding and viewer generation per install.
            if (
                channel is self._channel
                and self._channel_current()
                and generation == self._viewer_generation
                and resets == self._reset_count
            ):
                self._on_dart_message(payload)

        def schedule(payload: bytes) -> None:
            loop.call_soon_threadsafe(deliver, payload, self._reset_count)

        channel.on_bytes(schedule)

    def _on_dart_message(self, payload: bytes) -> None:
        self._inbox += payload
        try:
            packets = protocol.decode_packets(self._inbox)
        except ValueError as exc:
            self._protocol_error = str(exc)
            self._inbox.clear()
            return
        for packet in packets:
            self._dispatch(packet)

    @property
    def _resume_fence(self) -> bool:
        return getattr(self, "_fence", False)

    @_resume_fence.setter
    def _resume_fence(self, armed: bool) -> None:
        # Content-free diagnostics: fence transitions and dropped inbound while armed.
        if armed != getattr(self, "_fence", False):
            _DIAG.info(
                "term_fence armed=%s dropped=%s sent=%s acked=%s",
                armed,
                getattr(self, "_fence_drops", 0),
                getattr(self, "_sent_bytes", 0),
                getattr(self, "_acked_bytes", 0),
            )
            self._fence_drops = 0
        self._fence = armed

    def _dispatch(self, packet: protocol.DecodedPacket) -> None:
        if self._resume_fence and packet.kind in (
            protocol.INPUT,
            protocol.SIZE_REPORT,
            protocol.ACK,
        ):
            if packet.kind == protocol.SIZE_REPORT:
                cols, rows = struct.unpack(">HH", packet.payload)
                _DIAG.info("term_size cols=%s rows=%s applied=False", cols, rows)
            # Unverified binding: drop effects-bearing inbound (PTY input, resize, applied
            # accounting). The verification report, read-only MODE/VIEW reflections and waiter
            # replies still flow.
            self._fence_drops = getattr(self, "_fence_drops", 0) + 1
            if self._fence_drops == 1:
                _DIAG.info("term_fence_drop first kind=0x%02x", packet.kind)
            return
        if packet.kind == protocol.INPUT:
            origin, data = protocol.decode_input(packet.payload)
            if (
                origin == protocol.ORIGIN_MOUSE
                and self.wheel_blocked
                and protocol.is_wheel_report(data)
            ):
                # Produced before the viewer handled the cancel: stale.
                # Dropped synchronously so no deferred callback can see
                # it after the ack releases the block.
                self._blocked_drops += 1
                if self._blocked_drops == 1:
                    _DIAG.info("term_blocked_wheel_drop first count=1")
                return
            # Stamp the cancel epoch for work spawned by listeners, so
            # async handling can re-check it inside the writer.
            token = protocol.input_cancel_epoch.set(self._cancel_sent_seq)
            try:
                for listener in list(self._input_listeners):
                    listener(origin, data)
            finally:
                protocol.input_cancel_epoch.reset(token)
        elif packet.kind == protocol.SIZE_REPORT:
            cols, rows = struct.unpack(">HH", packet.payload)
            self._last_view_size = (cols, rows)
            _DIAG.info("term_size cols=%s rows=%s applied=True", cols, rows)
            for size_listener in list(self._size_listeners):
                size_listener(cols, rows)
        elif packet.kind == protocol.MODE_REPORT:
            bracketed = bool(packet.payload) and packet.payload[0] == 0x01
            for mode_listener in list(self._mode_listeners):
                mode_listener(bracketed)
        elif packet.kind == protocol.VIEW_REPORT:
            # Dart emits this only in its post-frame callback. This is a
            # viewport diagnostic, not an ACK that pixels were painted.
            self._view_report_count += 1
            state = protocol.decode_view_report(packet.payload)
            for view_listener in list(self._view_listeners):
                view_listener(state)
        elif packet.kind == protocol.SCROLL_CANCELLED:
            try:
                token = protocol.decode_scroll_cancelled(packet.payload)
            except ValueError:
                return
            self._scroll_cancel_supported = True
            epoch = self._cancel_outstanding.get(token)
            if epoch is None:
                return  # mount advertisement (token 0) or a stale binding
            if epoch > self._cancel_acked_seq:
                self._cancel_acked_seq = epoch
            self._cancel_outstanding = {
                tok: ep
                for tok, ep in self._cancel_outstanding.items()
                if ep > self._cancel_acked_seq
            }
        elif packet.kind == protocol.KEYBOARD_STATE:
            shown = bool(packet.payload) and packet.payload[0] == 0x01
            for keyboard_listener in list(self._keyboard_state_listeners):
                keyboard_listener(shown)
        elif packet.kind == protocol.KEYBOARD_TRACE:
            try:
                event, flags, viewport = protocol.decode_keyboard_trace(packet.payload)
            except ValueError:
                return
            logging.getLogger("relaydeck.client.typing_split").info(
                "keyboard_trace event=%s armed=%s focus=%s conn=%s input=%s down_shown=%s vv=%s",
                _KEYBOARD_TRACE_EVENTS[event],
                bool(flags & 1),
                bool(flags & 2),
                bool(flags & 4),
                bool(flags & 8),
                bool(flags & 16),
                viewport,
            )
        elif packet.kind == protocol.PAGE_METRICS:
            try:
                reset, scroll, offset, height, window = protocol.decode_page_metrics(packet.payload)
            except ValueError:
                return
            logging.getLogger("relaydeck.client.typing_split").info(
                "page_metrics reset=%s scroll=%s vv_offset=%s vv=%s window=%s",
                bool(reset),
                scroll,
                offset,
                height,
                window,
            )
        elif packet.kind == protocol.SELECTION_TEXT:
            text = packet.payload.decode("utf-8", errors="replace")
            if self._selection_waiters:
                fut = self._selection_waiters.popleft()
                if not fut.done():
                    fut.set_result(text)
        elif packet.kind == protocol.SNAPSHOT_CHUNK:
            try:
                req_id, total, offset, data = protocol.decode_snapshot_chunk(packet.payload)
            except ValueError:
                return
            wait = self._snapshot_waiters.get(req_id)
            if wait is None or wait.fut.done():
                return
            violation = self._check_snapshot_chunk(wait, total, offset, len(data))
            if violation is not None:
                self._drop_snapshot_waiter(req_id)
                wait.fut.set_exception(RuntimeError(f"snapshot rejected: {violation}"))
                return
            if wait.total is None:
                self._snapshot_inflight_bytes += total
            snapshot = wait.feed(total, offset, data)
            if snapshot is not None:
                self._drop_snapshot_waiter(req_id)
                wait.fut.set_result(snapshot)
        elif packet.kind == protocol.ACK:
            (applied,) = struct.unpack(">Q", packet.payload)
            if applied > self._acked_bytes:
                self._acked_bytes = applied
                self._flush_pending()
        elif packet.kind == protocol.RESUME_REPORT:
            try:
                req_id, instance, resets, applied = protocol.decode_resume_report(packet.payload)
            except ValueError:
                return
            if req_id == 0:
                # Mount hello: records the baseline native identity.
                # Same-FIFO ordering guarantees it reflects the engine
                # that just opened this channel.
                _DIAG.info(
                    "term_hello term=%s instance=%s prev=%s",
                    id(self) % 100000,
                    instance % 100000,
                    None if self._native_instance is None else self._native_instance % 100000,
                )
                previous = self._native_instance
                if previous is not None and previous != instance:
                    self.fence_fresh_viewer()
                    self._resume_fence = True
                    # Resets are per native engine. Carrying A's count into B
                    # would make every later verified resume fail reset_mismatch.
                    self._reset_count = resets
                    self._viewer_generation += 1
                    if self._channel is not None:
                        self._install_channel_delivery(self._channel, asyncio.get_running_loop())
                    _DIAG.info(
                        "term_native_replaced term=%s prev=%s new=%s",
                        id(self) % 100000,
                        previous % 100000,
                        instance % 100000,
                    )
                    for reopen_listener in tuple(self._reopen_listeners):
                        reopen_listener()
                    # RendererBinding also fences the fresh viewer. Keep effects
                    # blocked until its existing explicit reset/recovery, and record
                    # B's baseline after those callbacks have retired A's state.
                    self._resume_fence = True
                self._native_instance = instance
                return
            entry = self._resume_waiters.pop(req_id, None)
            if entry is None:
                return
            epoch, resume_fut = entry
            if epoch != self._resume_epoch or resume_fut.done():
                return
            resume_fut.set_result((instance, resets, applied))

    def _check_snapshot_chunk(
        self, wait: _SnapshotWait, total: int, offset: int, length: int
    ) -> str | None:
        """Validate declared ints BEFORE any reassembly allocation.

        Pure integer checks — no buffers touched — so a hostile total
        (e.g. 0xFFFFFFFF) or overrun offset is rejected without
        allocating. Returns the violation reason, or None when safe.
        """
        if total > protocol.MAX_SNAPSHOT_TOTAL:
            return f"declared total {total} exceeds {protocol.MAX_SNAPSHOT_TOTAL}"
        if wait.total is not None and wait.total != total:
            return f"total changed mid-stream {wait.total} -> {total}"
        if offset > total:
            return f"offset {offset} past total {total}"
        if offset + length > total:
            return f"chunk [{offset}:{offset + length}] overruns total {total}"
        if wait.total is None:
            declared = self._snapshot_inflight_bytes + total
            if declared > protocol.MAX_SNAPSHOT_INFLIGHT_BYTES:
                return f"in-flight {declared} exceeds {protocol.MAX_SNAPSHOT_INFLIGHT_BYTES}"
        return None

    def _drop_snapshot_waiter(self, req_id: int) -> None:
        wait = self._snapshot_waiters.pop(req_id, None)
        if wait is not None:
            self._snapshot_inflight_bytes -= wait.declared
            wait.declared = 0

    def _send(self, packet: bytes) -> None:
        if self._retired:
            if packet[0] == protocol.WRITE:
                raise TerminalOverflowError("terminal viewer is retired")
            return
        channel = self._channel
        if channel is not None and not self._channel_current():
            self._resume_fence = True
            self._verified_transport_generation = -1
            if packet[0] == protocol.WRITE:
                # Do not count/journal output a disposed Flet queue drops.
                raise TerminalOverflowError("terminal transport is stale")
            channel = None  # bounded, idempotent control state awaits rebind
        if self._resume_hold and channel is not None and self._ready.is_set():
            # Resume probe in flight: hold for ordered post-suffix
            # flush (still counted by the caller). The probe itself
            # bypasses _send so it can never deadlock behind the hold.
            write_start: int | None = None
            if packet[0] == protocol.WRITE:
                # Tagged pre-increment: the caller counts the chunk
                # after this returns, so _sent_bytes is its start.
                write_start = self._sent_bytes
            elif packet[0] == protocol.INPUT_ENABLED:
                # Idempotent state: only the latest input truth
                # matters, so older copies coalesce away instead of
                # queueing without bound.
                self._drop_held_kind(protocol.INPUT_ENABLED)
            if self._resume_held_bytes + len(packet) > protocol.VIEWER_QUEUE_LIMIT:
                self._overflowed = True
                raise TerminalOverflowError("resume hold queue full")
            self._resume_held.append((write_start, packet))
            self._resume_held_bytes += len(packet)
            return
        if channel is None or not self._ready.is_set():
            self._pending_writes.append(packet)
            self._pending_bytes += len(packet)
            if self._pending_bytes > protocol.VIEWER_QUEUE_LIMIT:
                self._overflowed = True
                raise TerminalOverflowError("terminal channel not ready; queue full")
            return
        channel.send(packet)

    def _flush_pending(self) -> None:
        channel = self._channel
        if channel is None or not self._channel_current():
            return
        while self._pending_writes:
            packet = self._pending_writes.popleft()
            self._pending_bytes -= len(packet)
            channel.send(packet)

    # -- output path ----------------------------------------------------
    def write(self, data: bytes) -> None:
        """Queue terminal output; raises TerminalOverflowError when the
        viewer is too slow instead of silently dropping bytes."""
        if self._overflowed:
            raise TerminalOverflowError("terminal viewer overflowed")
        unacked = self._sent_bytes - self._acked_bytes
        if unacked + len(data) > protocol.VIEWER_QUEUE_LIMIT:
            self._overflowed = True
            _DIAG.info("term_overflow unacked=%s fence=%s", unacked, self._fence)
            raise TerminalOverflowError(f"unacked {unacked} + {len(data)} exceeds viewer bound")
        for chunk in protocol.split_for_transport(data):
            if not chunk:
                continue
            self._send(protocol.encode_write(chunk))
            self._sent_bytes += len(chunk)
            self._journal_append(chunk)

    def _journal_append(self, chunk: bytes) -> None:
        self._journal.append(chunk)
        self._journal_bytes += len(chunk)
        overflow = self._journal_bytes - protocol.VIEWER_QUEUE_LIMIT
        while overflow > 0 and self._journal:
            oldest = self._journal.popleft()
            if len(oldest) > overflow:
                self._journal.appendleft(oldest[overflow:])
                self._journal_base += overflow
                self._journal_bytes -= overflow
                overflow = 0
            else:
                self._journal_base += len(oldest)
                self._journal_bytes -= len(oldest)
                overflow -= len(oldest)

    def _journal_suffix(self, applied: int, end: int) -> bytes | None:
        """Bytes (applied, end] from the journal, or None when the range
        is not fully retained. Pure read; no counters move. The caller
        passes the sent count snapshotted at probe time so bytes held
        during the probe await are never both redelivered and flushed."""
        if applied < self._journal_base or end > self._sent_bytes or applied > end:
            return None
        start = applied - self._journal_base
        stop = end - self._journal_base
        if start > self._journal_bytes or stop > self._journal_bytes:
            return None
        out = bytearray()
        skipped = 0
        for chunk in self._journal:
            if skipped + len(chunk) <= start:
                skipped += len(chunk)
                continue
            if skipped >= stop:
                break
            out += chunk[max(0, start - skipped) : max(0, stop - skipped)]
            skipped += len(chunk)
        suffix = bytes(out)
        if len(suffix) != end - applied:
            return None
        return suffix

    def _journal_clear(self) -> None:
        self._journal.clear()
        self._journal_base = 0
        self._journal_bytes = 0

    # -- input path (policy decided by the caller) -----------------------
    def fence_fresh_viewer(self) -> None:
        """Only after native channel evidence: discard old queued display state."""
        self._inbox.clear()
        self._sent_bytes = self._acked_bytes = 0
        self._pending_writes.clear()
        self._pending_bytes = 0
        self._overflowed = False
        self._protocol_error = None
        self._abort_viewer_waiters("viewer reopened")
        # A fresh viewer supersedes any in-flight resume: its probe
        # reply (if any) belongs to a dead binding. The new mount's
        # hello records the fresh baseline; until then no resume may
        # adopt an identity.
        self._resume_epoch += 1
        self._abort_resume_waiters()
        self._native_instance = None
        self._journal_clear()
        self._retire_hold()
        self.set_input_enabled(False)

    def set_input_enabled(self, enabled: bool) -> None:
        """Switch Composer display-only vs Direct Input at runtime.

        The value crosses to Dart as an INPUT_ENABLED packet immediately
        (never through a per-render prop patch); the local prop mirror is
        written directly to `_values` because the rendered control is
        frozen and the `Prop` descriptor rejects post-mount writes.
        """
        self._values["input_enabled"] = enabled
        self._send(protocol.encode_input_enabled(enabled))

    def set_keyboard_button(self, enabled: bool) -> None:
        """Show or hide the native keyboard button. Sent as a
        packet because the rendered control is frozen after mount, like input_enabled."""
        self._values["keyboard_button"] = enabled
        self._send(protocol.encode_keyboard_button(enabled))

    def clear_selection(self) -> None:
        self._send(protocol.encode_clear_selection())

    def scroll_viewport(self, pages: int | None) -> None:
        """Scroll the local view: negative pages up, positive down (1..10),
        None jumps to bottom. Never writes to the PTY; a no-op in the
        alternate buffer. Raises ValueError outside +-1..10."""
        self._send(protocol.encode_scroll_viewport(pages))

    def scroll_to_bottom(self) -> None:
        """Jump the local view to the live edge (scroll_viewport(None))."""
        self.scroll_viewport(None)

    @property
    def wheel_blocked(self) -> bool:
        """True while a cancel's ack is still outstanding."""
        return self._cancel_acked_seq < self._cancel_sent_seq

    @property
    def cancel_epoch(self) -> int:
        """Latest issued cancel epoch (unbounded; 0 before the first cancel).

        The wire token wraps at u32 and never equals 0 for a real cancel;
        this epoch never wraps, so stamps and the block stay monotonic.
        """
        return self._cancel_sent_seq

    def cancel_scroll(self) -> None:
        """Retire widget scroll motion before a server-side key/paste write.

        Sent only to a viewer that proved it acks (mount ack or a prior
        cancel): an older bundle would ignore the packet and the block
        would have no ack coming. The sequence advances only once the
        packet is accepted: on a dead or full channel there is no
        widget to ack, and reset/rebind/generation changes retire any
        outstanding cancel, so the block can never stick. Never raises.
        """
        if not self._scroll_cancel_supported:
            return
        # Only a live, unheld viewer can have wheel rows in flight; queueing the
        # cancel behind a rebind or resume would only add to a bounded queue.
        if self._resume_hold or not self._ready.is_set() or not self._channel_current():
            return
        epoch = self._cancel_sent_seq + 1
        token = (epoch - 1) % 0xFFFFFFFF + 1
        try:
            self._send(protocol.encode_cancel_scroll(token))
        except TerminalOverflowError:
            return
        self._cancel_sent_seq = epoch
        # Only the newest cancel can release the block; an older ack is
        # superseded, so the map never grows while acks are late.
        self._cancel_outstanding = {token: epoch}

    def request_keyboard(self) -> None:
        """Ask the viewer to open the soft keyboard (Direct Input icon).

        Dart honours it only while input is enabled, so send it after
        `set_input_enabled(True)`; it never writes to the PTY.
        """
        self._send(protocol.encode_request_keyboard())

    def release_keyboard(self) -> None:
        """Ask the viewer to hide the soft keyboard and disarm touch input.

        Never writes to the PTY; the viewer ignores it when input is disabled.
        """
        self._send(protocol.encode_release_keyboard())

    def _abort_viewer_waiters(self, reason: str) -> None:
        while self._selection_waiters:
            future = self._selection_waiters.popleft()
            if not future.done():
                future.set_exception(RuntimeError(reason))
        for req_id in list(self._snapshot_waiters):
            wait = self._snapshot_waiters[req_id]
            self._drop_snapshot_waiter(req_id)
            if not wait.fut.done():
                wait.fut.set_exception(RuntimeError(reason))

    def reset(self, *, reason: str = "explicit") -> None:
        self._reset_count += 1
        reasons = {"explicit": 0, "stream_reset": 1, "controller_replaced": 2}
        trace_lifecycle(
            "reset",
            viewer=id(self),
            generation=self._viewer_generation,
            count=self._reset_count,
            reason=reasons.get(reason, 3),
        )
        self._sent_bytes = 0
        self._acked_bytes = 0
        self._overflowed = False
        self._protocol_error = None
        self._cancel_acked_seq = self._cancel_sent_seq
        self._cancel_outstanding.clear()
        self._abort_viewer_waiters("terminal reset; snapshot aborted")
        # Counters just zeroed: an in-flight resume's offset base is
        # gone, so it must fail fast rather than validate against
        # reset state. The native identity survives (same engine).
        self._resume_epoch += 1
        self._abort_resume_waiters()
        self._retire_hold()
        self._journal_clear()
        self._send(protocol.encode_reset())

    def _abort_resume_waiters(self) -> None:
        for req_id in list(self._resume_waiters):
            _, fut = self._resume_waiters.pop(req_id)
            if not fut.done():
                fut.set_result(None)

    def _drop_held_kind(self, kind: int) -> None:
        """Remove every held packet of one kind, keeping byte counts exact."""
        kept: deque[tuple[int | None, bytes]] = deque()
        kept_bytes = 0
        for start, pkt in self._resume_held:
            if pkt[0] == kind:
                continue
            kept.append((start, pkt))
            kept_bytes += len(pkt)
        self._resume_held = kept
        self._resume_held_bytes = kept_bytes

    def _flush_held(self, *, after: int | None) -> None:
        # after=None: nothing was resent, so the whole batch flushes in order. after=S: the suffix
        # covered (applied, S], so held writes starting below S must not flush again; control
        # packets and later writes flush in order.
        while self._resume_held:
            start, packet = self._resume_held.popleft()
            self._resume_held_bytes -= len(packet)
            if after is not None and start is not None and start < after:
                continue
            self._send(packet)

    def _retire_hold(self) -> None:
        # Capture/reset start a new sync generation: the held offsets belong to dead counters, so
        # the batch is discarded and the fence releases for the explicit resync.
        self._resume_hold = False
        self._resume_hold_epoch = None
        self._resume_held.clear()
        self._resume_held_bytes = 0
        self._resume_fence = False

    def _resolve_channel(self, channel_id: int) -> Any:
        try:
            return self.get_data_channel(channel_id)
        except (RuntimeError, AttributeError):
            session = self._session_ref() if self._session_ref else None
            connection = getattr(session, "connection", None)
            if connection is None:
                raise
            # This hot-path diagnostic only needs to describe a changed route.
            route = (id(connection), channel_id)
            if route != self._logged_session_route:
                self._logged_session_route = route
                _DIAG.info(
                    "term_channel_resolve via=session term=%s id=%s", id(self) % 100000, channel_id
                )
            return connection.data_channel_for(channel_id)

    def _adopt_current_channel(self, loop: asyncio.AbstractEventLoop) -> Any:
        """Bind to the channel the transport resolves for this id now.

        A different previous binding is unhooked and closed; the partial inbound frame
        from the dead connection and queued packets are dropped (complete packets were
        already dispatched). Installs the binding-validity delivery for the new channel.
        """
        channel_id = self._channel_id
        if channel_id is None:
            raise RuntimeError("terminal data channel was never opened")
        channel = self._resolve_channel(channel_id)
        old = self._channel
        if old is not None and old is not channel:
            old.on_bytes(None)
            old.close()
        self._channel = channel
        self._bound_transport_generation = self._transport_generation
        # A partial Dart->Python frame tail from the dead connection
        # can never complete; complete packets were already dispatched
        # synchronously, so only the tail is dropped here.
        self._inbox.clear()
        self._pending_writes.clear()
        self._pending_bytes = 0

        self._install_channel_delivery(channel, loop)
        return channel

    def rebind_transport(self) -> bool:
        """Point this terminal at the transport that exists now, before any resume runs.

        A resume attempt cancelled before its task starts
        must not leave output on the dead channel. Inbound stays fenced until a verified
        resume or an explicit `reset()` clears it. No-op (False) before a channel was
        attached; idempotent when the channel object is unchanged.
        """
        if self._channel_id is None:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        try:
            current = self._resolve_channel(self._channel_id)
        except RuntimeError:
            # Not (re)attached to the page yet: the resume attempt re-resolves the
            # channel once the control is mounted. Never break the reconnect path.
            _DIAG.warning(
                "term_rebind_failed term=%s id=%s",
                id(self) % 100000,
                self._channel_id,
                exc_info=True,
            )
            return False
        changed = current is not self._channel
        _DIAG.info(
            "term_rebind term=%s id=%s changed=%s", id(self) % 100000, self._channel_id, changed
        )
        if changed:
            self._adopt_current_channel(loop)
        self._bound_transport_generation = self._transport_generation
        self._resume_fence = True
        return True

    @property
    def resume_unverified(self) -> bool:
        """True while inbound effects are fenced for a transport not yet verified."""
        return self._resume_fence

    async def rebind_resumed(
        self,
        *,
        timeout: float = 5.0,
        current: Callable[[], bool] | None = None,
    ) -> ResumeOutcome:
        """Rebind this retained terminal to the replacement UI transport.

        Called once per terminal after authenticated session reuse (same
        verified identity, new FletApp connection). Probes the native
        engine over the freshly resolved mux binding and, only when the
        engine proves it is the same instance with matching reset
        identity and a journal-covered applied offset, redelivers the
        missing output suffix exactly once.

        `current` is an optional synchronous ownership guard supplied
        by the caller (UI resume trigger): it runs after all bridge checks
        pass, immediately before any success output delivery. A False
        return (or a raise) yields `incomplete/superseded` with the
        fence still armed, counters/journal untouched, no reset, and
        no VERIFIED — the caller no longer owns this boundary. None
        (default) keeps the unguarded behavior.

        Guarantees on "resumed": viewer_generation untouched (the
        session's reopen path never fires, the controller keeps
        running), no resize, no takeover, no input replay — queued
        pre-attach packets are dropped (subsumed by the journal
        suffix) and only the live input-enabled truth is re-asserted
        as idempotent state sync. Dart→Python input sent during the
        outage was already dropped by the dead socket (Flet queues no
        raw channel bytes); both sides additionally fence input until
        this call verifies continuity.
        Anything uncertain (timeout, identity/reset/offset mismatch,
        evicted journal, pre-existing protocol error) returns
        "incomplete": the caller must drive the honest
        reset-as-watcher recovery, never silent continuation. The
        owning attempt still flushes its held batch so write() never
        silently loses bytes; only the gap stays unknown. A
        superseded attempt flushes nothing (its successor owns the
        batch now); capture/reset retire the batch outright.
        """
        if self._channel_id is None:
            return ResumeOutcome("noop", "never_attached")
        if self._protocol_error is not None:
            # Fail closed and fenced: the stream state is untrusted. Retiring the binding needs a
            # replacement channel, so the fence covers queued callbacks on the current one.
            self._resume_epoch += 1
            self._abort_resume_waiters()
            self._resume_fence = True
            return ResumeOutcome("incomplete", "protocol_error")
        # A concurrent resume loses to this one; a stale reply can
        # never validate against a newer binding.
        self._resume_epoch += 1
        self._abort_resume_waiters()
        epoch = self._resume_epoch
        # The replacement binding is unverified until this attempt
        # positively completes: inbound effects stay fenced.
        self._resume_fence = True
        loop = asyncio.get_running_loop()
        channel = self._adopt_current_channel(loop)
        had_overflow = self._overflowed
        self._resume_next_id += 1
        req_id = self._resume_next_id
        fut: asyncio.Future[tuple[int, int, int] | None] = loop.create_future()
        self._resume_waiters[req_id] = (epoch, fut)
        self._resume_hold = True
        self._resume_hold_epoch = epoch
        try:
            # Direct send: the probe bypasses _send so it can never
            # deadlock behind the hold it just armed.
            channel.send(protocol.encode_resume_probe(req_id))
            sent_at_probe = self._sent_bytes
            try:
                report = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except TimeoutError:
                self._resume_waiters.pop(req_id, None)
                return ResumeOutcome("incomplete", "probe_timeout")
            if report is None or epoch != self._resume_epoch:
                return ResumeOutcome("incomplete", "superseded")
            if self._protocol_error is not None:
                # Stream framing failed during the probe: a matching
                # report must not override the failed parser state.
                # The fence stays armed; no VERIFIED leaves here.
                return ResumeOutcome("incomplete", "protocol_error")
            instance, resets, applied = report
            adopt_native: int | None = None
            if self._native_instance is None:
                # Without a mount hello, validate reset identity independently (zero output does not
                # prove equal reset counts) and commit the adoption only after every later check. A
                # reloaded document can reuse a channel id for a different engine.
                if resets != self._reset_count:
                    return ResumeOutcome("incomplete", "reset_mismatch")
                if applied == 0 and sent_at_probe == 0:
                    adopt_native = instance
                else:
                    return ResumeOutcome("incomplete", "unknown_native")
            elif instance != self._native_instance:
                return ResumeOutcome("incomplete", "instance_changed")
            elif resets != self._reset_count:
                return ResumeOutcome("incomplete", "reset_mismatch")
            if applied > sent_at_probe:
                return ResumeOutcome("incomplete", "offset_future")
            if applied < self._acked_bytes:
                return ResumeOutcome("incomplete", "offset_regressed")
            suffix = self._journal_suffix(applied, sent_at_probe)
            if suffix is None:
                return ResumeOutcome("incomplete", "offset_evicted")
            if current is not None:
                try:
                    owned = current()
                except Exception:
                    owned = False
                if not owned:
                    return ResumeOutcome("incomplete", "superseded")
            if adopt_native is not None:
                self._native_instance = adopt_native
            # Everything at or below applied was displayed; the resend covers only (applied,
            # sent_at_probe]. Counters do not move: the suffix was counted when first written.
            # Direct sends bypass the hold like the probe does.
            self._acked_bytes = applied
            for chunk in protocol.split_for_transport(suffix):
                if not chunk:
                    continue
                channel.send(protocol.encode_write(chunk))
            # The re-assert subsumes any coalesced held copy (both are
            # the latest-called truth), so the held copy drops and the
            # wire carries input truth exactly once, before VERIFIED.
            self._drop_held_kind(protocol.INPUT_ENABLED)
            self._resume_hold = False
            self._resume_hold_epoch = None
            self._flush_held(after=sent_at_probe)
            channel.send(protocol.encode_input_enabled(self.input_enabled))
            channel.send(protocol.encode_resume_verified())
            self._resume_fence = False
            reason = "ok_after_overflow" if had_overflow else "ok"
            return ResumeOutcome("resumed", reason, len(suffix))
        finally:
            if self._resume_hold_epoch == epoch:
                # Only non-success exits reach here still owning the
                # batch (success disarms inline above): nothing was
                # resent, so the whole batch flushes in order.
                self._resume_hold = False
                self._resume_hold_epoch = None
                self._flush_held(after=None)

    async def get_selection_text(self, timeout: float = 5.0) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._selection_waiters.append(fut)
        self._send(protocol.encode_request_selection_text())
        return await asyncio.wait_for(fut, timeout)

    @property
    def channel_ready(self) -> bool:
        return self._ready.is_set() and self._channel_current()

    async def get_snapshot_text(self, timeout: float = 5.0) -> str:
        """Fetch the live emulator buffer text (scrollback + screen).

        Emulator truth: cursor overwrites applied, escape sequences
        consumed — the Select Text sheet source. Raises RuntimeError
        immediately when the channel never attached (caller falls back
        to raw output instead of hanging); TimeoutError cleans up the
        waiter so late chunks cannot contaminate the next request.
        """
        if not self.channel_ready:
            raise RuntimeError("terminal channel not ready; no snapshot source")
        if len(self._snapshot_waiters) >= protocol.MAX_SNAPSHOT_INFLIGHT:
            raise RuntimeError(
                f"too many in-flight snapshot requests ({protocol.MAX_SNAPSHOT_INFLIGHT})"
            )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._snapshot_next_id += 1
        req_id = self._snapshot_next_id
        self._snapshot_waiters[req_id] = _SnapshotWait(fut)
        self._send(protocol.encode_request_snapshot(req_id))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._drop_snapshot_waiter(req_id)

    # -- test seam -------------------------------------------------------
    def _test_inject_dart_message(self, payload: bytes) -> None:
        """Drive the Dart->Python path without a Flutter client."""
        self._on_dart_message(payload)

    def _test_mark_ready(self, sent: list[bytes]) -> None:
        """Bind a capturing fake channel for host-mode tests."""

        class _FakeChannel:
            def send(self, payload: bytes) -> None:
                sent.append(payload)

            def on_bytes(self, handler: Any) -> None:
                pass

            def close(self) -> None:
                pass

        self._channel = _FakeChannel()  # type: ignore[assignment]
        self._ready.set()
        self._flush_pending()
