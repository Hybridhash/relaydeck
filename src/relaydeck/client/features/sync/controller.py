"""Sync controller and terminal stream client.

The controller owns one AppState and drives it from the gateway: initial
snapshot, SSE metadata stream, reconnect with backoff and cursor resume,
explicit resync on gaps/epoch changes, and revocation on auth failure.
The terminal client tracks output sequences, acks, generations, and
reconnect resume for one lease.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import (
    GatewayTransport,
    TerminalSocket,
    TransportError,
)
from relaydeck.client.open_timing import mark_current
from relaydeck.contracts.catalog import catalog_from_wire
from relaydeck.contracts.schemas import SchemaError, validate, validate_metadata_event
from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.domain import reducers
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    Connection,
    ConnectionState,
    EventType,
    MetadataEvent,
    Pane,
    Snapshot,
    Tab,
    Workspace,
)
from relaydeck.lease_diagnostics import LeaseTrace

MAX_IDENTICAL_GAP_FAILURES = 3


def snapshot_from_wire(doc: dict[str, Any]) -> Snapshot:
    host = doc.get("host", {})
    return Snapshot(
        host_id=str(host.get("id", "")),
        host_epoch=str(host.get("epoch", "")),
        cursor=str(doc.get("cursor", "")),
        host_protocol=int(host.get("protocol", 0)),
        host_version=str(host.get("version", "")),
        host_capabilities=frozenset(str(c) for c in host.get("capabilities", [])),
        agent_catalog=catalog_from_wire(host.get("agent_catalog")),
        workspaces=tuple(
            Workspace(
                id=str(w.get("id", "")),
                host_id=str(w.get("host_id", "")),
                number=int(w.get("number", 0)),
                label=str(w.get("label", "")),
                focused=bool(w.get("focused", False)),
                pane_count=int(w.get("pane_count", 0)),
                tab_count=int(w.get("tab_count", 0)),
                active_tab_id=str(w.get("active_tab_id", "")),
                agent_status=AgentStatus.coerce(str(w.get("agent_status", "unknown"))),
            )
            for w in doc.get("workspaces", [])
        ),
        tabs=tuple(
            Tab(
                id=str(t.get("id", "")),
                host_id=str(t.get("host_id", "")),
                workspace_id=str(t.get("workspace_id", "")),
                number=int(t.get("number", 0)),
                label=str(t.get("label", "")),
                focused=bool(t.get("focused", False)),
                pane_count=int(t.get("pane_count", 0)),
                agent_status=AgentStatus.coerce(str(t.get("agent_status", "unknown"))),
            )
            for t in doc.get("tabs", [])
        ),
        panes=tuple(
            Pane(
                id=str(p.get("id", "")),
                host_id=str(p.get("host_id", "")),
                workspace_id=str(p.get("workspace_id", "")),
                tab_id=str(p.get("tab_id", "")),
                focused=bool(p.get("focused", False)),
                agent_status=AgentStatus.coerce(str(p.get("agent_status", "unknown"))),
            )
            for p in doc.get("panes", [])
        ),
        agents=tuple(
            Agent(
                terminal_id=str(a.get("terminal_id", "")),
                host_id=str(a.get("host_id", "")),
                workspace_id=str(a.get("workspace_id", "")),
                tab_id=str(a.get("tab_id", "")),
                pane_id=str(a.get("pane_id", "")),
                name=str(a.get("name", "")),
                agent=str(a.get("agent", "")),
                title=str(a.get("title", "")),
                status=AgentStatus.coerce(str(a.get("status", "unknown"))),
                focused=bool(a.get("focused", False)),
                launch_pending=bool(a.get("launch_pending", False)),
                interactive_ready=bool(a.get("interactive_ready", False)),
                state_change_seq=int(a.get("state_change_seq", 0)),
                revision=int(a.get("revision", 0)),
            )
            for a in doc.get("agents", [])
        ),
    )


def _observed_ms(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            moment = datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return int(moment.timestamp() * 1000)
    return 0


def event_from_wire(doc: dict[str, Any]) -> MetadataEvent:
    return MetadataEvent(
        event_id=str(doc.get("event_id", "")),
        host_id=str(doc.get("host_id", "")),
        host_epoch=str(doc.get("host_epoch", "")),
        sequence=int(doc.get("sequence", 0)),
        cursor=str(doc.get("cursor", "")),
        type=EventType(str(doc.get("type", "resync.required"))),
        resource_id=str(doc.get("resource_id", "")),
        resource_revision=int(doc.get("resource_revision", 0)),
        observed_ms=_observed_ms(doc.get("observed_at")),
        payload=dict(doc.get("payload", {})),
    )


@dataclass(frozen=True)
class SyncConnection(Connection):
    """Immutable retry presentation data; reducers preserve these fields.

    The domain Connection remains unchanged. An outage spans unsuccessful
    snapshot/stream retries; only a stable open stream resets its clock.
    """

    retry_attempt: int = 0
    not_live_since: float | None = None


@dataclass
class SyncController:
    transport: GatewayTransport
    backoff: Backoff = field(default_factory=Backoff)
    sleep: Callable[[float], Any] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    state: AppState = field(default_factory=AppState)
    recover_auth: Callable[[], Awaitable[bool]] | None = None
    _listeners: list[Callable[[AppState], None]] = field(default_factory=list)
    _resync_event: asyncio.Event = field(default_factory=asyncio.Event)
    _resume_snapshot: tuple[str, dict[str, Any]] | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _stream_started: float | None = None
    _retry_attempt: int = 0
    _outage_since: float | None = None
    _resume_listeners: list[Callable[[], None]] = field(default_factory=list)
    last_run_exit: str = "not_started"
    _gap_boundary: tuple[str, str, str] | None = None
    _gap_failures: int = 0
    _gap_sequence: int = 0

    def _reset_gap_failures(self) -> None:
        self._gap_boundary = None
        self._gap_failures = 0
        self._gap_sequence = 0

    def gap_stalled_at(self, host_id: str) -> bool:
        """Keep lifecycle resubscription stopped at the failed host boundary."""
        host = self.state.inventory.hosts.get(host_id)
        return self.state.connection.detail == "gap_stalled" and self._gap_boundary == (
            host_id,
            host.epoch if host else "",
            self.state.connection.cursor,
        )

    def _gap_stalled(self, host_id: str, code: str) -> bool:
        if code != "gap":
            self._reset_gap_failures()
            return False
        host = self.state.inventory.hosts.get(host_id)
        boundary = (host_id, host.epoch if host else "", self.state.connection.cursor)
        self._gap_failures = self._gap_failures + 1 if boundary == self._gap_boundary else 1
        self._gap_boundary = boundary
        self._gap_sequence = self.state.connection.last_sequence
        return self._gap_failures >= MAX_IDENTICAL_GAP_FAILURES

    def subscribe_resume(self, listener: Callable[[], None]) -> None:
        self._resume_listeners.append(listener)

    def unsubscribe_resume(self, listener: Callable[[], None]) -> None:
        if listener in self._resume_listeners:
            self._resume_listeners.remove(listener)

    def resume_ui(self) -> None:
        """Same verified identity: refresh subscribers, revive only a dead task.

        No Root rebuild, terminal attach, command retry or credential replay.
        """
        self._emit()
        for listener in list(self._resume_listeners):
            listener()

    def task_failed(self, error: BaseException) -> None:
        # Exception text can contain network/credential data. Record only
        # its type and a fixed actionable state; consume it at the owner.
        self.last_run_exit = f"error:{type(error).__name__}"
        if not self.state.revoked:
            self._set(
                reducers.set_connection(
                    self.state, ConnectionState.DISCONNECTED, "sync_task_failed"
                )
            )

    def _reconnecting(self, reason: str) -> None:
        if self.state.revoked:
            return
        self._retry_attempt += 1
        if self._outage_since is None:
            self._outage_since = self.clock()
        connection = self.state.connection
        connection = SyncConnection(
            state=ConnectionState.RECONNECTING,
            host_id=connection.host_id,
            cursor=connection.cursor,
            last_sequence=connection.last_sequence,
            detail=reason,
            retry_attempt=self._retry_attempt,
            not_live_since=self._outage_since,
        )
        self._set(replace(self.state, connection=connection, stale=True))

    def _stream_opened(self) -> None:
        if self.state.revoked:
            return
        if self._stream_started is None:
            self._stream_started = self.clock()
            self._set(reducers.set_connection(self.state, ConnectionState.LIVE))

    def _stable_stream(self) -> bool:
        return self._stream_started is not None and self.clock() - self._stream_started >= 30

    def subscribe(self, listener: Callable[[AppState], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[AppState], None]) -> None:
        """Remove a previous subscriber (host switching must not leak
        emitters; idempotent)."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def request_resync(self) -> None:
        """Recycle a possibly half-open stream through snapshot and cursor resume.

        Cancel the parked frame pull even if no more frames arrive. Rapid signals
        coalesce into one recovery; cross-thread delivery wakes the owning loop.
        """
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._resync_event.set)
            except RuntimeError:
                self._resync_event.set()  # loop closed; the flag still trips `wait()`
            return
        self._resync_event.set()

    def _emit(self) -> None:
        for listener in list(self._listeners):
            listener(self.state)

    def _host_ungranted(self, host_id: str) -> bool:
        granted = self.state.granted_hosts
        return granted is not None and host_id not in granted

    def _set(self, state: AppState) -> None:
        # Revocation is terminal: late status or metadata work must not replace it. A new
        # sign-in builds a new controller.
        if self.state.revoked and not (
            state.revoked and state.connection.state == ConnectionState.ACCESS_REVOKED
        ):
            return
        self.state = state
        self._emit()

    def apply_grants(self, entries: list[dict[str, Any]]) -> AppState:
        """Merge verified host grants into authoritative state.

        Later sync emissions cannot restore revoked hosts or their selections.
        """
        self._set(reducers.merge_hosts(self.state, entries))
        return self.state

    def accept_snapshot(self, host_id: str, doc: dict[str, Any]) -> AppState:
        """Merge a creation read while preserving the current metadata cursor/liveness."""
        validate("relaydeck/gateway/v1/snapshot", doc)
        if doc["host"]["id"] != host_id:
            raise TransportError("upstream_incompatible", "snapshot host mismatch", False)
        updated = reducers.apply_snapshot(self.state, snapshot_from_wire(doc))
        self._set(replace(updated, connection=self.state.connection))
        return self.state

    def prepare_resume(self, host_id: str, doc: dict[str, Any]) -> None:
        """Seed the next metadata run from the lifecycle's validated fresh snapshot.

        This queues cursor reconnection; it does not assert a connected socket.
        """
        self.accept_snapshot(host_id, doc)
        self._resume_snapshot = (host_id, doc)

    def select(self, host_id: str, session_key: str, terminal_kind: str) -> AppState:
        """Store route selection in authoritative state so sync preserves it."""
        self._set(reducers.select_session(self.state, host_id, session_key, terminal_kind))
        return self.state

    async def run(self, host_id: str, stop: Callable[[], bool] = lambda: False) -> AppState:
        """Run until stop() or revocation. Returns the final state."""
        self._loop = asyncio.get_running_loop()
        self.last_run_exit = "running"
        self._retry_attempt = 0
        self._outage_since = None
        self._reset_gap_failures()
        recovered_auth = False
        self._set(reducers.set_connection(self.state, ConnectionState.CONNECTING))
        while not stop() and not self.state.revoked:
            try:
                self._stream_started = None
                await self._sync_once(host_id, stop)
                return self.state
            except TransportError as exc:
                if stop() or self.state.revoked:
                    return self.state
                if self._gap_stalled(host_id, exc.code):
                    self._reconnecting("gap")
                    self._set(
                        reducers.set_connection(
                            self.state, ConnectionState.DISCONNECTED, "gap_stalled"
                        )
                    )
                    return self.state
                if self._stable_stream():
                    recovered_auth = False
                    self.backoff.reset()
                    self._retry_attempt = 0
                    self._outage_since = None
                if exc.code in ("unauthenticated", "forbidden"):
                    if exc.code == "unauthenticated" and self.recover_auth and not recovered_auth:
                        recovered_auth = True
                        self._reconnecting("reauthenticating")
                        try:
                            recovered = await self.recover_auth()
                        except TransportError as recovery_error:
                            self._set(
                                reducers.set_connection(
                                    self.state, ConnectionState.DISCONNECTED, recovery_error.code
                                )
                            )
                            return self.state
                        if stop() or self.state.revoked:
                            return self.state
                        if recovered:
                            if self._host_ungranted(host_id):
                                self._set(
                                    reducers.set_connection(
                                        self.state, ConnectionState.DISCONNECTED, "host_revoked"
                                    )
                                )
                                return self.state
                            await self.sleep(self.backoff.next_delay_ms() / 1000)
                            continue
                    if exc.code == "forbidden" and self._host_ungranted(host_id):
                        # Per-host grant loss, not account revocation: the
                        # verified listing dropped this host, so a gateway
                        # denial for it must not nuke writes account-wide.
                        self._set(
                            reducers.set_connection(
                                self.state, ConnectionState.DISCONNECTED, "host_revoked"
                            )
                        )
                        return self.state
                    self._set(reducers.revoke(self.state))
                    return self.state
                if not exc.retryable:
                    self._set(
                        reducers.set_connection(self.state, ConnectionState.DISCONNECTED, exc.code)
                    )
                    return self.state
                self._reconnecting(exc.code)
                await self.sleep(self.backoff.next_delay_ms() / 1000)
        return self.state

    async def _sync_once(self, host_id: str, stop: Callable[[], bool]) -> None:
        # A signal received before this fresh snapshot is already covered
        # by it. Keep signals DURING the snapshot/pull: those still recycle.
        if self.state.revoked:
            return
        signaled = self._resync_event.is_set()
        self._resync_event.clear()
        if self.state.connection.state != ConnectionState.RECONNECTING:
            self._set(
                replace(
                    reducers.set_connection(self.state, ConnectionState.SYNCHRONIZING), stale=True
                )
            )
        prepared: tuple[str, dict[str, Any]] | None = None if signaled else self._resume_snapshot
        self._resume_snapshot = None
        raw = (
            prepared[1]
            if prepared is not None and prepared[0] == host_id
            else await self.transport.snapshot(host_id)
        )
        if self.state.revoked:
            return
        try:
            validate("relaydeck/gateway/v1/snapshot", raw)
        except SchemaError as exc:
            raise TransportError("upstream_incompatible", f"bad snapshot: {exc}", False) from exc
        snapshot = snapshot_from_wire(raw)
        self._set(replace(reducers.apply_snapshot(self.state, snapshot), stale=True))
        stream = self.transport.stream(host_id, snapshot.cursor)
        pull_iter = stream.__aiter__()
        try:
            while True:
                if stop() or self.state.revoked:
                    return
                # Race the next frame against the link signal so a parked pull (half-open socket)
                # still recycles. Cancelling the pull leaves no __anext__ in flight, so the final
                # close is legal.
                pull = asyncio.ensure_future(pull_iter.__anext__())
                wake = asyncio.ensure_future(self._resync_event.wait())
                watched: set[asyncio.Task[Any]] = {pull, wake}
                try:
                    done, _pending = await asyncio.wait(
                        watched, return_when=asyncio.FIRST_COMPLETED
                    )
                except BaseException:
                    pull.cancel()
                    wake.cancel()
                    await asyncio.gather(pull, wake, return_exceptions=True)
                    raise
                if self.state.revoked:
                    pull.cancel()
                    wake.cancel()
                    await asyncio.gather(pull, wake, return_exceptions=True)
                    return
                if wake in done:
                    # Link signal: recycle now. Rapid signals coalesce
                    # into this single recovery: the event clears exactly
                    # once, so N rapid signals yield one resync.
                    self._resync_event.clear()
                    pull.cancel()
                    wake.cancel()
                    await asyncio.gather(pull, wake, return_exceptions=True)
                    raise TransportError("link_change", "connectivity changed", True)
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                try:
                    frame = pull.result()
                except StopAsyncIteration:
                    if stop():
                        return
                    raise TransportError("stream_closed", "Metadata stream closed", True) from None
                if frame.event == "stream_open":
                    # Internal transport signal after HTTP acceptance, not
                    # a gateway/upstream event. Idle hosts need this too.
                    self._accept_stream_open(frame, host_id, snapshot.cursor)
                else:
                    self._apply_frame(frame)
                if self._stable_stream():
                    self.backoff.reset()
                    self._retry_attempt = 0
                    self._outage_since = None
        finally:
            await stream.aclose()

    def _accept_stream_open(self, frame: SseFrame, host_id: str, cursor: str) -> None:
        if self.state.revoked:
            return
        if frame.data:
            try:
                boundary = json.loads(frame.data)
            except ValueError:
                boundary = None
            if (
                not isinstance(boundary, dict)
                or set(boundary) != {"host_id", "cursor", "sequence"}
                or boundary["host_id"] != host_id
                or boundary["cursor"] != cursor
                or type(boundary["sequence"]) is not int
                or boundary["sequence"] < 0
            ):
                raise TransportError("upstream_incompatible", "bad stream boundary", False)
            if self._stream_started is None:
                # apply_snapshot resets the reducer's sequence. Restore the
                # authenticated replay boundary before checking any event;
                # never infer it from the first event or parse opaque cursors.
                self._set(
                    replace(
                        self.state,
                        connection=replace(
                            self.state.connection, last_sequence=boundary["sequence"]
                        ),
                    )
                )
        self._stream_opened()

    def _apply_frame(self, frame: SseFrame) -> None:
        if frame.event == "error":
            try:
                doc = json.loads(frame.data or "{}")
            except ValueError:
                doc = {}
            code = str(doc.get("code", "upstream_incompatible"))
            if code in ("unauthenticated", "forbidden"):
                raise TransportError(code, code, False)
            elif code in ("stale_epoch", "stale_revision"):
                self._set(reducers.set_connection(self.state, ConnectionState.SYNCHRONIZING, code))
                raise TransportError(code, code, True)
            else:
                raise TransportError(code, code, bool(doc.get("retryable", True)))
            return
        if frame.event != "metadata":
            return
        try:
            doc = json.loads(frame.data)
            validate_metadata_event(doc)
        except (ValueError, SchemaError):
            self._set(
                reducers.set_connection(self.state, ConnectionState.SYNCHRONIZING, "bad_event")
            )
            raise TransportError("upstream_incompatible", "bad event", False) from None
        previous_sequence = self.state.connection.last_sequence
        state, action = reducers.apply_event(self.state, event_from_wire(doc))
        if action not in ("gap", "stale_epoch"):
            # Scripted adapters can prove opening by a validated frame.
            # Production uses stream_open, including on an idle host.
            self._stream_opened()
            state, action = reducers.apply_event(self.state, event_from_wire(doc))
        self._set(state)
        if state.connection.last_sequence > previous_sequence and self._gap_boundary is not None:
            host = state.inventory.hosts.get(state.connection.host_id)
            identity = (state.connection.host_id, host.epoch if host else "")
            # A snapshot may rewind to a retained replay prefix. Reapplying
            # that prefix is not new progress past the previous gap boundary.
            if (
                identity != self._gap_boundary[:2]
                or state.connection.last_sequence > self._gap_sequence
            ):
                self._reset_gap_failures()
        if action in ("gap", "stale_epoch"):
            raise TransportError(action, action, True)


@dataclass
class TerminalStreamClient:
    """One lease: epoch/offset tracking, acks, generations, resume (wire v2).

    The socket factory takes a single-use ticket minted via
    `GatewayTransport.terminal_ticket`. Output continuity is (epoch,
    next_offset); an epoch change or offset gap reports through on_gap
    and the owner resyncs explicitly. Input carries a per-writer sequence;
    reader replacement preserves it. The server deduplicates duplicate
    frames; the client never automatically replays input.
    """

    socket_factory: Callable[[str], TerminalSocket]
    on_output: Callable[[int, bytes], None]
    on_control: Callable[[dict[str, Any]], None]
    on_gap: Callable[[str], None]
    on_reset: Callable[[str], None] | None = None
    on_input_failure: Callable[[int, int], None] | None = None
    diagnostics: LeaseTrace | None = field(default=None, kw_only=True)
    lease_id: str = ""
    generation: int = 0
    epoch: str = ""
    next_offset: int = 0
    offered_offset: int | None = None
    _socket: TerminalSocket | None = None
    _acked: int = 0
    _input_seq: int = 0
    _opens: int = 0
    # True while opening again on a lease this client already streamed: the socket must then
    # carry `resume_query()`, because the server holds the lease for exactly that reader.
    reopening: bool = False

    def _lease_trace(self) -> LeaseTrace:
        return self.diagnostics or LeaseTrace.for_session(
            self, "", "", self.lease_id, generation=self.generation
        )

    async def open(
        self, lease_id: str, generation: int, ticket: str, epoch: str = "", offset: int = 0
    ) -> None:
        same_writer = (self.lease_id, self.generation) == (lease_id, generation)
        self.lease_id = lease_id
        self.generation = generation
        self.epoch = epoch
        self.next_offset = offset
        self._acked = offset
        # Reader replacement is not a new writer. Preserve assigned sequence
        # numbers (including uncertain sends), never reuse or replay them.
        if not same_writer:
            self._input_seq = 0
        self.offered_offset = None
        self.reopening = self._opens > 0
        self._opens += 1
        socket = self.socket_factory(ticket)
        self._socket = socket
        trace = self._lease_trace()
        try:
            await socket.connect()
        except Exception:
            trace.emit("socket_close", reason="receive_failure", socket=socket)
            raise
        trace.emit("socket_open", reason="ok", socket=socket)
        mark_current("socket")

    async def run(self) -> None:
        socket = self._socket
        trace = self._lease_trace()
        if socket is None:
            return
        while self._socket is socket:
            try:
                kind, payload = await socket.receive()
            except Exception as exc:
                retired = self._socket is not socket
                trace.emit(
                    "socket_close",
                    reason="socket_replaced"
                    if retired
                    else (exc.code if isinstance(exc, TransportError) else "receive_failure"),
                    current_lease_id=self.lease_id,
                    socket=socket,
                )
                if retired:
                    return
                raise
            if self._socket is not socket:
                trace.emit(
                    "socket_close",
                    reason="socket_replaced",
                    current_lease_id=self.lease_id,
                    socket=socket,
                )
                return
            if kind == "bytes":
                try:
                    chunk = terminal_wire.decode_output(payload)
                except ValueError:
                    self.on_gap("bad_frame")
                    trace.emit("socket_close", reason="bad_frame", socket=socket)
                    return
                if self.epoch and chunk.epoch != self.epoch:
                    self._report_reset(f"epoch {self.epoch} -> {chunk.epoch}")
                    trace.emit("socket_close", reason="epoch_changed", socket=socket)
                    return
                self.epoch = chunk.epoch
                end_offset = chunk.offset + len(chunk.data)
                if chunk.offset < self.next_offset and end_offset <= self.next_offset:
                    # Ticket-only opens can replay retained bytes before our
                    # preserved cursor. They are already rendered: never
                    # rewind or feed them to the emulator a second time.
                    # Empty frames at the current cursor still signal liveness.
                    continue
                if chunk.offset > self.next_offset:
                    self.offered_offset = chunk.offset
                    self.on_gap(f"offset {self.next_offset} -> {chunk.offset}")
                    trace.emit("socket_close", reason="offset_gap", socket=socket)
                    return
                offset = self.next_offset
                data = chunk.data[offset - chunk.offset :]
                self.next_offset = end_offset
                if data:
                    mark_current("first_byte")
                self.on_output(offset, data)
                if self.next_offset - self._acked >= 4096:
                    await self.ack()
            else:
                try:
                    doc = json.loads(payload)
                    validate("relaydeck/gateway/v1/terminal-control", doc)
                except (ValueError, SchemaError):
                    self.on_gap("bad_control")
                    trace.emit("socket_close", reason="bad_control", socket=socket)
                    return
                if doc.get("kind") == "reset":
                    self._report_reset(str(doc.get("detail", "reset")))
                    trace.emit("socket_close", reason="reset", socket=socket)
                    return
                if (
                    doc.get("kind") in ("gap", "bye")
                    and self.on_input_failure is not None
                    and doc.get("generation") == self.generation
                    and type(doc.get("input_seq")) is int
                ):
                    self.on_input_failure(doc["generation"], doc["input_seq"])
                if doc.get("kind") == "gap":
                    self.on_gap(str(doc.get("detail", "gap")))
                    trace.emit("socket_close", reason="gap", socket=socket)
                    return
                self.on_control(doc)
                if doc.get("kind") == "bye":
                    trace.emit(
                        "socket_close",
                        reason=doc.get("detail", "bye"),
                        source="server",
                        socket=socket,
                    )
                    return

    def _report_reset(self, reason: str) -> None:
        """Require reattachment after epoch rotation or reset.

        Use the gap callback when no reset listener is installed.
        """
        if self.on_reset is not None:
            self.on_reset(reason)
        else:
            self.on_gap(reason)

    def resume_query(self) -> str:
        """Query fragment carrying (epoch, offset) so a reconnect resumes
        exactly where this client stopped. Factories append it to the
        ticket URL: `?ticket=...&` + resume_query()."""
        return f"epoch={quote(self.epoch, safe='')}&offset={self.next_offset}"

    async def _send(self, kind: str, data: bytes) -> bool:
        socket = self._socket
        if socket is None:
            if kind == "resize":
                return False
            raise TransportError("host_offline", "Terminal stream closed", True)
        self._input_seq += 1
        await socket.send_bytes(
            terminal_wire.encode_input(self.lease_id, self.generation, self._input_seq, kind, data)
        )
        return True

    async def send_input(self, data: bytes) -> None:
        await self._send("text", data)

    async def send_key(self, name: str) -> None:
        await self._send("key", name.encode())

    async def resize(self, cols: int, rows: int) -> bool:
        try:
            return await self._send("resize", struct.pack(">HH", cols, rows))
        except TransportError:
            return False  # A late size report is never retried after reader close.

    async def ack(self) -> None:
        socket = self._socket
        if socket is None:
            return
        self._acked = self.next_offset
        await socket.send_text(json.dumps({"kind": "ack", "applied": self.next_offset}))

    async def close(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            trace = self._lease_trace()
            try:
                try:
                    await socket.send_text(json.dumps({"kind": "bye"}))
                except TransportError:
                    pass
            finally:
                try:
                    await socket.close()
                finally:
                    trace.emit("socket_close", reason="local_close", socket=socket)

    def resume_offset(self) -> int:
        return self.next_offset

    def resume_seq(self) -> int:
        """Back-compat alias: resume offsets replaced sequences in wire v2."""
        return self.next_offset
