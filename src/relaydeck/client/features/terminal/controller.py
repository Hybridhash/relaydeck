"""Terminal stream controller. Flet-free.

Owns one `TerminalStreamClient` plus the ordered `TerminalByteStream`,
drives `TerminalViewState`, and gates resize to the control lease: size
reports from a view lease are recorded as rejected and never sent. The
screen layer forwards bytes to the extension control and reports viewer
overflow back through `on_viewer_overflow`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from relaydeck.client import open_timing
from relaydeck.client.features.sync.controller import TerminalStreamClient
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.keys import KeyInputError
from relaydeck.client.features.terminal.models import (
    TONE_WARN,
    NoticeModel,
    TerminalViewState,
    apply_bytes,
    apply_gap,
    apply_live,
    apply_reset,
)
from relaydeck.client.features.terminal.paste_policy import PasteError
from relaydeck.client.features.terminal.session.recovery_state import (
    GAP_STABLE_S,
    GapRecovery,
)
from relaydeck.client.features.terminal.session.recovery_state import (
    WATCH_RETRY_DELAYS_S as WATCH_RETRY_DELAYS_S,
)
from relaydeck.client.features.terminal.stream import (
    ACTION_REATTACH,
    ACTION_RESUME,
    EMULATOR_FRESH,
    EMULATOR_PRESERVED,
    EVENT_DATA,
    EVENT_GAP,
    EVENT_RESET,
    RecoveryDecision,
    ResumeRequest,
    StreamEvent,
    TerminalByteStream,
    decide_recovery,
)
from relaydeck.contracts.wire.terminal import OutputChunk
from relaydeck.lease_diagnostics import LeaseTrace

MAX_GEOMETRY_CELL = 1024


@dataclass
class TerminalController:
    socket_factory: Callable[[str], TerminalSocket]
    lease_id: str
    generation: int
    mode: str
    on_bytes: Callable[[bytes], None]
    on_state: Callable[[TerminalViewState], None] = field(default=lambda state: None)
    on_input_failure: Callable[[int, int], None] = field(default=lambda generation, seq: None)
    diagnostics: LeaseTrace | None = field(default=None, kw_only=True)
    on_history_reset: Callable[[], Awaitable[bool]] | None = field(default=None, kw_only=True)
    emulator: str = EMULATOR_FRESH
    gap_recovery: GapRecovery = field(default_factory=GapRecovery, kw_only=True)
    state: TerminalViewState = field(init=False)
    pending_resume: ResumeRequest | None = field(init=False, default=None)
    reattach_needed: bool = field(init=False, default=False)
    live_edge_pending: bool = field(init=False, default=False)
    _overflow_attempts: int = field(init=False, default=0)
    _overflow_stable_since: float | None = field(init=False, default=None)
    _live_edge_opening: bool = field(init=False, default=False)
    _history_notice: str | None = field(init=False, default=None)
    _writer_paused: bool = field(init=False, default=False)
    _writer_revoked: bool = field(init=False, default=False)
    _ui_input_fenced: bool = field(init=False, default=False)
    _revoke_reason: str = field(init=False, default="")
    _viewer_lost: bool = field(init=False, default=False)
    _detached: bool = field(init=False, default=False)
    rejected_resizes: list[tuple[int, int]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.state = TerminalViewState(lease_mode=self.mode)
        self._stream = TerminalByteStream()
        self._started = False
        self._client = TerminalStreamClient(
            socket_factory=self.socket_factory,
            diagnostics=self.diagnostics,
            on_output=self._handle_output,
            on_control=self._handle_control,
            on_gap=self._handle_gap,
            on_reset=self._handle_reset,
            on_input_failure=lambda generation, seq: self.on_input_failure(generation, seq),
            on_baseline=self._handle_baseline,
        )

    # -- lifecycle -------------------------------------------------------
    @property
    def gap_detail(self) -> str:
        return self.gap_recovery.detail

    def reset_gap_recovery(self) -> None:
        self.gap_recovery.reset()
        self._overflow_attempts = 0
        self._overflow_stable_since = None

    def cancel_gap_wait(self) -> None:
        self.gap_recovery.cancel_wait()

    def set_host_online(self, online: bool) -> None:
        self.gap_recovery.set_host_online(online)

    async def wait_gap_recovery(self, current: Callable[[], bool]) -> bool:
        identity = (self.lease_id, self.generation)

        def valid() -> bool:
            return (
                current()
                and identity == (self.lease_id, self.generation)
                and not self._viewer_lost
                and not self._detached
                and not self._writer_revoked
                and not self._writer_paused
            )

        if not valid():
            return False
        return self.mode != "control" or await self.gap_recovery.wait(valid)

    async def start(self, ticket: str, epoch: str = "", offset: int = 0) -> None:
        if self._viewer_lost:
            raise TransportError("viewer_reopened", "Explicit reattach required", False)
        self._detached = False
        # A reopened stream re-establishes liveness from its own first bytes; otherwise the
        # pre-recovery status and notice stay pinned while new bytes flow.
        self._started = False
        self._history_notice = None
        self._stream = TerminalByteStream(epoch, offset)
        self._client.reset_to_live = self.live_edge_pending
        await self._client.open(self.lease_id, self.generation, ticket, epoch, offset)

    def offered_stream_position(self) -> tuple[str, int | None]:
        """The stream client's current (epoch, offered offset) for reattach.

        The offered offset is set on every client-level gap (the server's
        retention base when it clamps a live-edge open); None means no
        usable resume position has been offered yet."""
        epoch = self._client.epoch or ""
        offered = self._client.offered_offset
        if isinstance(offered, bool):
            return (epoch, None)
        return (epoch, offered if isinstance(offered, int) else None)

    async def run(self) -> None:
        socket = self._client._socket
        try:
            await self._client.run()
        finally:
            if self._client._socket is socket:
                self._live_edge_opening = False

    async def close(self) -> None:
        await self._client.close()

    def resume_query(self) -> str:
        if self._viewer_lost:
            return ""
        return self._client.resume_query()

    @property
    def reopening(self) -> bool:
        return self._client.reopening

    def viewer_reopened(self) -> None:
        """A new native emulator owns no bytes from the previous viewer.

        Fence synchronously, before queued input/output or a recovery driver
        can reuse the old cursor. Only an explicit session reattach creates a
        replacement controller; this never requests takeover or replays input.
        """
        self._viewer_lost = True
        self.invalidate_writer("viewer_reopened")
        self.emulator = EMULATOR_FRESH
        self._started = False
        self._stream = TerminalByteStream()
        self.pending_resume = None
        self.reattach_needed = False
        self._set(
            TerminalViewState(
                status="disconnected",
                lease_mode="none",
                notice=NoticeModel(
                    TONE_WARN, "Viewer restarted; reattach explicitly. History may be incomplete."
                ),
            )
        )

    def disconnected(self) -> None:
        """Project local detachment immediately; retained history is unchanged."""
        self._detached = True
        self.invalidate_writer("disconnected")
        self._set(replace(self.state, status="disconnected", lease_mode="none"))

    # -- pause: writer invalidation -----------------------
    @property
    def writer_paused(self) -> bool:
        """True after `pause_writer()` until explicit `resume_writer()`."""
        return self._writer_paused

    def pause_writer(self) -> None:
        """Invalidate the control writer immediately. Probes report no live writer
        while paused, so every send path fails closed with `no_writer`
        and no byte is delivered. Idempotent."""
        self._writer_paused = True
        self.gap_recovery.cancel_wait()

    def resume_writer(self) -> None:
        """Explicitly re-enable the writer after the resume flow finished.

        Re-enables ONLY this controller's own probe: the generation and
        lease are unchanged, so this cannot silently steal control held
        elsewhere (a taken-over lease fails at ticket mint and the
        session maps the mint failure to disconnected -- "Could not
        stream" -- never to conflict). A revoked writer stays revoked."""
        self._writer_paused = False

    def invalidate_writer(self, reason: str = "") -> None:
        """Revoke the control writer immediately. Probes report
        no live writer from now on, so every send path fails closed with
        `no_writer` and no byte is delivered. One-way within this
        controller's life: only a replacement controller (takeover /
        reconnect) can produce a live writer again. Idempotent."""
        if not self._writer_revoked:
            self._revoke_reason = reason  # the first cause stays; later local fences add none
        self._writer_revoked = True
        self.gap_recovery.cancel_wait()

    @property
    def writer_revoked(self) -> bool:
        """True after `invalidate_writer()` (terminal renewal failure or
        explicit revoke). Unlike pause, this is never resumed."""
        return self._writer_revoked

    @property
    def revoke_reason(self) -> str:
        return self._revoke_reason

    # -- resize: controller only ------------------------------------------
    def wants_resize(self, cols: int, rows: int) -> tuple[int, int] | None:
        """Filter a Dart size report. Control leases may resize; view
        leases record a rejection and must never send."""
        if (
            self.mode != "control"
            or self._writer_revoked
            or self._writer_paused
            or self._ui_input_fenced
        ):
            self.rejected_resizes.append((cols, rows))
            return None
        if not (1 <= cols <= MAX_GEOMETRY_CELL and 1 <= rows <= MAX_GEOMETRY_CELL):
            return None
        return (cols, rows)

    async def request_resize(self, cols: int, rows: int) -> bool:
        """Send a resize frame when this controller holds control. False
        means rejected (view lease or bad geometry): nothing was sent."""
        wanted = self.wants_resize(cols, rows)
        if wanted is None:
            return False
        return await self._client.resize(*wanted)

    # -- stream events ------------------------------------------------------
    def _set(self, state: TerminalViewState) -> None:
        self.state = state
        self.on_state(state)

    @property
    def live_output_received(self) -> bool:
        """Preserved live metadata alone does not prove this reader has delivered a frame."""
        return self._started

    def _effective_emulator(self) -> str:
        if self.emulator == EMULATOR_PRESERVED or self._started:
            return EMULATOR_PRESERVED
        return EMULATOR_FRESH

    def _handle_output(self, offset: int, data: bytes) -> None:
        if self._viewer_lost or self._detached:
            return
        # The stream client already enforced epoch continuity, so its
        # epoch is the wire epoch of this chunk.
        event = self._stream.push(OutputChunk(self._client.epoch, offset, data))
        if event.kind == EVENT_DATA:
            if self.mode == "control":
                self.gap_recovery.output()
            self.on_bytes(event.data)
            if self.live_edge_pending or (self.ui_input_fenced and self._overflow_attempts >= 3):
                return
            now = time.monotonic()
            if self._overflow_stable_since is None:
                self._overflow_stable_since = now
            elif now - self._overflow_stable_since >= GAP_STABLE_S:
                self._overflow_attempts = 0
            if not self._started:
                self._started = True
                decision = decide_recovery(
                    event, self._stream, self.emulator, start_offset=event.offset
                )
                notice_text = (
                    self._history_notice if self._history_notice is not None else decision.notice
                )
                notice = NoticeModel(TONE_WARN, notice_text) if notice_text else None
                self._set(apply_live(self.state, self._stream.epoch, offset, notice))
            self._set(apply_bytes(self.state, len(event.data)))
        elif event.kind == EVENT_GAP:
            self._apply_gap(event)
        else:
            self._apply_reset(event)

    async def _handle_baseline(self, doc: dict[str, Any]) -> bool:
        socket = self._client._socket
        if self._viewer_lost or self._detached or self._writer_revoked or self._writer_paused:
            return False
        if doc["incomplete"]:
            self.fence_ui_input(True)
            if self.on_history_reset is None or not await self.on_history_reset():
                return False
        if (
            self._viewer_lost
            or self._detached
            or self._writer_revoked
            or self._writer_paused
            or self._client._socket is not socket
        ):
            return False
        self._stream = TerminalByteStream(doc["epoch"], doc["offset"])
        self.emulator = EMULATOR_FRESH
        self._history_notice = (
            "History incomplete: earlier output unavailable." if doc["incomplete"] else ""
        )
        self.live_edge_pending = False
        self._live_edge_opening = False
        self.pending_resume = None
        self.reattach_needed = False
        self._started = False
        if doc["incomplete"]:
            self.fence_ui_input(False)
        return True

    def _handle_control(self, doc: dict[str, Any]) -> None:
        # ack/bye flow through the stream client; nothing to project yet.
        _ = doc

    def _handle_gap(self, detail: str) -> None:
        self._overflow_stable_since = None
        if self._viewer_lost or self._detached:
            return
        if self.mode == "control":
            self.gap_recovery.record(detail)
        offered = self._client.offered_offset
        if (
            not self._started
            and self.emulator == EMULATOR_PRESERVED
            and offered is not None
            and offered > self._stream.next_offset
        ):
            # The first retained replay starts beyond the preserved cursor:
            # history was evicted. Resuming the same missing cursor repeats
            # the gap forever. Reset the viewer explicitly, then recover this
            # lease at the offered base; never acquire control or resize.
            self.pending_resume = None
            self.emulator = EMULATOR_FRESH
            self._apply_reset(StreamEvent(EVENT_RESET, self._stream.next_offset, b"", detail))
            return
        if not self._started and self.emulator == EMULATOR_FRESH and offered:
            # First-connect live edge: the server clamped offset 0 to its
            # retention base. Reopen exactly there; the first chunk above
            # byte 0 then raises the incomplete-history notice.
            self.pending_resume = ResumeRequest(self._client.epoch, offered)
            self._set(apply_gap(self.state, detail))
            return
        self._apply_gap(StreamEvent(EVENT_GAP, self._stream.next_offset, b"", detail))

    def _handle_reset(self, reason: str) -> None:
        self._overflow_stable_since = None
        if reason == "overflow":
            self.on_viewer_overflow()
            return
        if self._viewer_lost or self._detached:
            return
        self._apply_reset(StreamEvent(EVENT_RESET, self._stream.next_offset, b"", reason))

    def _apply_gap(self, event: StreamEvent) -> None:
        decision = decide_recovery(event, self._stream, self._effective_emulator())
        self._ingest_decision(decision, event.detail)

    def _apply_reset(self, event: StreamEvent) -> None:
        decision = decide_recovery(event, self._stream, self._effective_emulator())
        self._ingest_decision(decision, event.detail)

    def _ingest_decision(self, decision: RecoveryDecision, detail: str) -> None:
        if self._viewer_lost or self._detached:
            return
        if decision.action == ACTION_RESUME and decision.resume is not None:
            self.pending_resume = decision.resume
            self._set(apply_gap(self.state, detail))
        elif decision.action == ACTION_REATTACH:
            self.reattach_needed = True
            self._set(apply_reset(self.state, decision.notice or detail))
        else:  # pragma: no cover - continue carries no state change
            pass

    def on_viewer_overflow(self) -> None:
        """Fence once, then request an authenticated same-lease live-edge reset."""
        if self._viewer_lost or self._detached or self.live_edge_pending:
            return
        self._overflow_stable_since = None
        self.fence_ui_input(True)
        self.live_edge_pending = True  # Exhaustion must never fall through to lease reacquisition.
        if self._overflow_attempts >= 3:
            self.pending_resume = None
            self.reattach_needed = False
            self._set(
                replace(
                    apply_gap(self.state, "overflow"),
                    notice=NoticeModel(
                        TONE_WARN, "History incomplete: output recovery stopped; Retry explicitly"
                    ),
                )
            )
            return
        self.pending_resume = ResumeRequest(self._client.epoch, self._client.next_offset)
        self.reattach_needed = False
        self._set(
            replace(
                apply_gap(self.state, "overflow"),
                notice=NoticeModel(
                    TONE_WARN, "History incomplete: resuming live; earlier output unavailable."
                ),
            )
        )

    def begin_live_edge_recovery(self) -> bool:
        if not self.live_edge_pending or self._live_edge_opening or self._overflow_attempts >= 3:
            return False
        self._live_edge_opening = True
        self._overflow_attempts += 1
        return True

    # -- paste writer probe/sender ------------------------
    def paste_generation(self) -> int | None:
        """Current writer generation for paste routing, or None when this
        controller holds no live writer (view lease or closed socket).
        The review session binds this at open and revalidates at
        confirm; a mismatch fails closed with no input sent.

        Lifecycle caveat: this reflects close()-driven state only. After
        an unclean run() exit the probe may still report a generation
        while the socket is dead; `send_paste_chunk` then raises
        TransportError and no bytes are delivered (fail closed)."""
        if not self.writer_eligible or self._ui_input_fenced:
            return None
        return self.generation

    @property
    def writer_eligible(self) -> bool:
        """Underlying held writer eligibility, excluding the UI verification fence."""
        return (
            self.mode == "control"
            and not self._writer_paused
            and not self._writer_revoked
            and self._client._socket is not None
        )

    def fence_ui_input(self, fenced: bool) -> None:
        """UI transport verification never changes lease/pause/revocation ownership."""
        self._ui_input_fenced = fenced
        open_timing.mark_current("unfenced" if not fenced else "fenced")

    @property
    def ui_input_fenced(self) -> bool:
        return self._ui_input_fenced

    def next_input_identity(self) -> tuple[int, int]:
        """Identity reserved synchronously immediately before a writer send."""
        return self.generation, self._client._input_seq + 1

    async def send_paste_chunk(self, data: bytes) -> None:
        """Send one paste chunk through the sole current writer: the
        existing text input path (per-generation input_seq). Raises
        PasteError `no_writer` when no writer is live: an explicit
        failure with no hidden fallback."""
        if self.paste_generation() is None:
            raise PasteError("no_writer", "paste blocked: no live control writer")
        await self._client.send_input(data)

    # -- keys/direct writer probe/sender -------------------
    def key_generation(self) -> int | None:
        """Current writer generation for keys/direct-text routing: the same
        sole control-lease writer as paste (None when no writer is live)."""
        return self.paste_generation()

    async def send_key_bytes(self, data: bytes) -> None:
        """Send exact key/direct-text bytes through the sole current writer:
        the existing text input path (per-generation input_seq). Raises
        KeyInputError `no_writer` when no writer is live: an explicit
        failure with no hidden fallback."""
        if self.key_generation() is None:
            raise KeyInputError("no_writer", "key blocked: no live control writer")
        await self._client.send_input(data)
