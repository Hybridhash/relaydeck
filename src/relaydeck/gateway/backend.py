"""Gateway backend seam.

The HTTP/WebSocket layer speaks only to this interface. Production
wiring connects it to connector clients and the journal;
tests and development use FakeBackend.
"""

from __future__ import annotations

import asyncio
import json as _json
import secrets
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from relaydeck.contracts.schemas import (
    COMMAND_STATUSES,
    GATEWAY_PROTOCOL,
    SchemaError,
    validate_metadata_event,
)
from relaydeck.gateway.cursors import CursorError, encode_cursor
from relaydeck.gateway.leases import Attachment, LeaseError, LeaseRecord, LeaseStore
from relaydeck.gateway.ring import STATUS_OK, RingStore, StreamFanout


@dataclass(frozen=True)
class TerminalLease:
    lease_id: str
    host_id: str
    terminal_id: str
    generation: int
    owner: str
    expires_ms: int
    epoch: str = "e1"
    mode: str = "control"
    device_id: str = ""
    attachment_id: str = ""
    resume_pending: bool = False


def _to_lease(rec: LeaseRecord) -> TerminalLease:
    return TerminalLease(
        lease_id=rec.lease_id,
        host_id=rec.host_id,
        terminal_id=rec.terminal_id,
        generation=rec.generation,
        owner=rec.account,
        expires_ms=rec.expires_ms,
        epoch=rec.epoch,
        mode=rec.mode,
        device_id=rec.device_id,
        attachment_id=rec.attachment_id,
        resume_pending=rec.handoff_original_expiry_ms is not None,
    )


def _reraise(exc: LeaseError) -> BackendError:
    return BackendError(exc.code, exc.message, exc.retryable, exc.details)


@dataclass(frozen=True)
class OutputBatch:
    chunks: tuple[tuple[int, bytes], ...]
    next_offset: int


@dataclass(frozen=True)
class ResizeRequest:
    """One controller-authorized PTY geometry change. Recorded
    in the backend resize_log when a control lease's resize frame
    validates. The log is observability only; the serving socket separately
    forwards a fresh frame to the authenticated connector's PTY bridge."""

    lease_id: str
    generation: int
    input_seq: int
    cols: int
    rows: int


@dataclass(frozen=True)
class StreamBatch:
    """One offset-addressed ring read. `status` is ok, stale_offset or
    stale_epoch; stale batches carry no bytes (ring.RingRead rule)."""

    status: str
    chunks: tuple[tuple[int, bytes], ...]
    next_offset: int
    epoch: str


class BackendError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})


class Backend(ABC):
    requires_connector = False

    def epochs_for_host(self, host_id: str, account: str) -> list[dict[str, str]]:
        return []

    def rotate_streams(self, host_id: str) -> tuple[str, ...]:
        """Rotate continuity and return downstream leases to sever."""
        return ()

    def reset_terminal(
        self, host_id: str, terminal_id: str, epoch: str, account: str
    ) -> tuple[str, ...]:
        return ()

    @abstractmethod
    def snapshot(self, host_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def events_after(self, host_id: str, sequence: int) -> list[dict[str, Any]]: ...

    async def follow(
        self, host_id: str, after_sequence: int, heartbeat_s: float = 15.0
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Live event feed: replay, then new events, None per heartbeat.

        The default is a finite replay; production backends
        override with a push feed. Consumers treat None as a keepalive.
        """
        for event in self.events_after(host_id, after_sequence):
            yield event

    @abstractmethod
    def submit_command(self, command: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def command_status(self, command_id: str) -> dict[str, Any] | None: ...

    def command_owner(self, command_id: str) -> tuple[str, str] | None:
        return None

    @abstractmethod
    def agents_page(self, host_id: str, limit: int, offset: int) -> dict[str, Any]: ...

    @abstractmethod
    def issue_lease(
        self, host_id: str, terminal_id: str, owner: str, generation: int, epoch: str = "e1"
    ) -> TerminalLease: ...

    @abstractmethod
    def open_lease(self, lease_id: str) -> TerminalLease | None: ...

    @abstractmethod
    def write_input(
        self, lease_id: str, generation: int, input_seq: int, kind: str, data: bytes
    ) -> bool:
        """Store one input frame. False when the sequence is a duplicate
        within this generation (deduplicated, not an error)."""

    @abstractmethod
    def read_output(self, lease_id: str, after_offset: int) -> OutputBatch: ...

    @abstractmethod
    def publish_output(self, attachment_id: str, epoch: str, data: bytes) -> tuple[int, int]:
        """Append upstream bytes to an attachment ring; returns
        (start_offset, end_offset). Wrong-epoch publishes fail stale_epoch."""

    @abstractmethod
    def read_stream(self, lease_id: str, epoch: str, after_offset: int) -> StreamBatch:
        """Resume an attachment ring for one live lease. Empty `epoch` means
        no epoch knowledge (first connect): offset 0 serves from the live
        edge, offset > 0 without a matching epoch is stale_epoch. A resume
        past retention or the live edge is stale_offset. Stale reads carry
        no bytes; the route answers reset-required."""

    @abstractmethod
    def close_lease(self, lease_id: str) -> None: ...

    @abstractmethod
    def create_attachment(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        account: str,
        device_id: str,
        *,
        takeover: bool,
        owner_label: str = "",
        expected_control_revision: str | None = None,
    ) -> tuple[Attachment, tuple[str, ...]]:
        """Authorize or take over upstream ownership. Returns the
        attachment plus displaced downstream lease ids (close their
        sockets). Raises BackendError lease_conflict with holder
        disclosure when owned and takeover is not set."""

    @abstractmethod
    def get_attachment(self, attachment_id: str) -> Attachment | None: ...

    @abstractmethod
    def acquire_control(
        self,
        attachment_id: str,
        account: str,
        device_id: str,
        *,
        expected_generation: int | None = None,
    ) -> TerminalLease:
        """Exclusive control writer (re-acquire by the holder renews)."""

    @abstractmethod
    def issue_view(self, attachment_id: str, account: str, device_id: str) -> TerminalLease:
        """Read-only observer of an already-owned stream."""

    @abstractmethod
    def control_holder(
        self, host_id: str, terminal_id: str, account: str, device_id: str
    ) -> dict[str, Any]:
        """Read-only holder state plus its opaque control_revision."""

    @abstractmethod
    def renew_lease(self, lease_id: str, account: str, device_id: str) -> TerminalLease: ...

    @abstractmethod
    def release_lease(self, lease_id: str, account: str, device_id: str) -> Attachment:
        """Detach one lease; the attachment and remote process survive.
        Holder-only: a device mismatch is not_found (mirrors renew)."""

    @abstractmethod
    def lease_host(self, lease_id: str) -> str | None:
        """Host of a lease regardless of liveness (route guard only)."""

    @abstractmethod
    def close_attachment(self, attachment_id: str, account: str) -> tuple[str, ...]:
        """Explicit close; returns dead downstream lease ids."""

    @abstractmethod
    def revoke_leases_account(self, account: str) -> tuple[str, ...]: ...

    @abstractmethod
    def revoke_leases_device(self, account: str, device_id: str) -> tuple[str, ...]: ...

    @abstractmethod
    def revoke_leases_host(self, host_id: str) -> tuple[str, ...]: ...

    # -- journal/tunnel/live-push seam (concrete defaults) ------
    # JournalBackend overrides these; FakeBackend overrides the fanout
    # and attachment lookups so the live-push and tunnel paths behave
    # identically on both backends. Defaults fail closed (no fanout, no
    # outbox, no ack/result ingestion).

    def fanout_for(self, attachment_id: str) -> StreamFanout | None:
        """Live ring fanout for one attachment, if the backend keeps one."""
        return None

    def begin_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int
    ) -> bool:
        """Optional backend support; unknown backends release rather than retain control."""
        return False

    def finish_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int
    ) -> TerminalLease:
        raise BackendError("lease_expired", "reader handoff unsupported")

    def resolve_attachment(self, host_id: str, terminal_id: str) -> Attachment | None:
        """Open attachment for a (host, terminal) pair, if any."""
        return None

    def drain_outbox(self, host_id: str) -> list[dict[str, Any]]:
        """Pending gateway->connector tunnel messages; draining consumes."""
        return []

    def mark_command_dispatched(self, command_id: str, now_ms: int) -> bool:
        """Write-ahead dispatch marker for the tunnel pump: True when the
        intent moved accepted -> dispatched (pump must send); False when
        it already moved (pump must skip the send, never re-dispatch)."""
        return False

    def note_command_ack(self, command_id: str, host_id: str, now_ms: int) -> dict[str, Any] | None:
        """Ingest a connector command.ack; None when refused (unknown id
        or host binding mismatch)."""
        return None

    def note_command_result(
        self,
        command_id: str,
        host_id: str,
        outcome: str,
        result_digest: str,
        error_code: str,
        result: dict[str, Any] | None,
        now_ms: int,
    ) -> dict[str, Any] | None:
        """Ingest a connector command.result (outcome succeeded, failed,
        rejected, or unknown); None when refused."""
        return None

    def drain_socket_closes(self) -> tuple[str, ...]:
        """Lease ids whose sockets the app must sever now (session.close
        path); draining consumes."""
        return ()

    def ingest_terminal_output(
        self,
        host_id: str,
        terminal_id: str,
        epoch: str,
        data: bytes,
        now_ms: int,
    ) -> dict[str, Any]:
        """Ingest one tunnel terminal.output frame into the attachment
        ring. Returns ok offsets or an error code."""
        return {"error": "not_found"}

    def ingest_snapshot(
        self, host_id: str, snapshot: dict[str, Any], now_ms: int, cursor_secret: bytes
    ) -> dict[str, Any]:
        """Store a tunnel-pushed snapshot (replaces), stamping the
        resume cursor at the current event boundary: clients stream
        from `snapshot.cursor`, which must be a server HMAC cursor
        (the connector value is ignored). Returns ok or an error code.
        """
        return {"error": "not_found"}

    def ingest_event(
        self,
        host_id: str,
        partial: dict[str, Any],
        now_ms: int,
        cursor_secret: bytes,
    ) -> dict[str, Any]:
        """Store a tunnel-pushed metadata event with server-stamped
        sequence/cursor. Deduplicates by event_id (re-push safe).
        Returns the stored event or an error code."""
        return {"error": "not_found"}

    def publish_command_updated(
        self,
        status_record: dict[str, Any],
        host_epoch: str,
        cursor_secret: bytes,
        now_ms: int,
    ) -> dict[str, Any] | None:
        """Emit a command.updated event for a wire status record.

        Idempotent per (command_id, status): re-emission returns the
        stored event. None when no valid envelope can form (unknown
        status, empty target, or missing host epoch). The app.py tunnel
        handlers call this after a successful note_command_ack /
        note_command_result; without that hook nothing is emitted.
        """
        return None


MAX_EVENT_LOG_PER_HOST = 5000


class EventLogIndex:
    """Per-host event_id index + retention bound over event_logs lists.

    The lists stay the source of truth (callers inject and externally
    append in tests), so the index reconciles whenever a list's length
    diverges from the last-seen length: dedupe and sequencing are O(1)
    on the steady path and O(n) only right after an external mutation.
    Appends trim each host log to MAX_EVENT_LOG_PER_HOST, oldest first;
    resume cursors older than retention resync from what remains (logs
    are process memory and cursors already die on restart).
    """

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, dict[str, Any]]] = {}
        self._lengths: dict[str, int] = {}
        self._last_seq: dict[str, int] = {}

    def _reconcile(
        self, logs: dict[str, list[dict[str, Any]]], host_id: str
    ) -> dict[str, dict[str, Any]]:
        log = logs.get(host_id, [])
        if self._lengths.get(host_id) != len(log) or host_id not in self._by_id:
            index: dict[str, dict[str, Any]] = {}
            last = 0
            for event in log:
                event_id = event.get("event_id")
                if isinstance(event_id, str) and event_id:
                    index[event_id] = event
                seq = event.get("sequence", 0)
                if isinstance(seq, int) and seq > last:
                    last = seq
            self._by_id[host_id] = index
            self._lengths[host_id] = len(log)
            self._last_seq[host_id] = last
        return self._by_id[host_id]

    def find(
        self, logs: dict[str, list[dict[str, Any]]], host_id: str, event_id: str
    ) -> dict[str, Any] | None:
        """Stored event for an event_id, if the host log holds it."""
        return self._reconcile(logs, host_id).get(event_id)

    def last_sequence(self, logs: dict[str, list[dict[str, Any]]], host_id: str) -> int:
        """Highest sequence in the host log (0 when empty)."""
        self._reconcile(logs, host_id)
        return self._last_seq.get(host_id, 0)

    def next_sequence(self, logs: dict[str, list[dict[str, Any]]], host_id: str) -> int:
        return self.last_sequence(logs, host_id) + 1

    def append(
        self, logs: dict[str, list[dict[str, Any]]], host_id: str, stored: dict[str, Any]
    ) -> None:
        """Append one stamped event, indexing it and trimming the log."""
        log = logs.setdefault(host_id, [])
        index = self._reconcile(logs, host_id)
        log.append(stored)
        event_id = stored.get("event_id")
        if isinstance(event_id, str) and event_id:
            index[event_id] = stored
        seq = stored.get("sequence", 0)
        if isinstance(seq, int) and seq > self._last_seq.get(host_id, 0):
            self._last_seq[host_id] = seq
        overflow = len(log) - MAX_EVENT_LOG_PER_HOST
        if overflow > 0:
            for evicted in log[:overflow]:
                evicted_id = evicted.get("event_id")
                if isinstance(evicted_id, str) and index.get(evicted_id) is evicted:
                    del index[evicted_id]
            del log[:overflow]
        self._lengths[host_id] = len(log)


def build_command_updated(status_record: dict[str, Any], host_epoch: str) -> dict[str, Any] | None:
    """Command.updated partial for a wire status record, if formable.

    resource_id carries the command id; payload carries the wire status
    plus the routing target (no prompt/result bytes: the wire record
    excludes bodies). None when the record cannot form a valid
    envelope (unknown status, empty target, missing host epoch).
    """
    command_id = status_record.get("command_id")
    status = status_record.get("status")
    target = status_record.get("target")
    if (
        not isinstance(command_id, str)
        or not command_id
        or status not in COMMAND_STATUSES
        or not isinstance(target, dict)
        or not target
        or not host_epoch
    ):
        return None
    return {
        "event_id": f"cmdupd-{command_id}-{status}",
        "host_epoch": host_epoch,
        "type": "command.updated",
        "resource_id": command_id,
        "payload": {
            "command_id": command_id,
            "status": status,
            "target": dict(target),
            "kind": str(status_record.get("kind", "")),
            **(
                {"error_code": status_record["error_code"]}
                if status_record.get("error_code")
                else {}
            ),
        },
    }


def prepare_event(
    host_id: str,
    partial: dict[str, Any],
    sequence: int,
    cursor_secret: bytes,
    now_ms: int,
) -> dict[str, Any]:
    """Stamp a connector event partial into a full valid envelope.

    Sequence, cursor, protocol, and host binding are server-owned: the
    connector must not mint them (anything it sends for those keys is
    ignored). Raises ValueError when the partial cannot form a valid
    envelope."""
    if not isinstance(partial, dict):
        raise ValueError("event is not an object")
    event_id = partial.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = f"ev-{secrets.token_hex(8)}"
    host_epoch = partial.get("host_epoch", "")
    observed_at = partial.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        observed_at = datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    full: dict[str, Any] = {
        "protocol": GATEWAY_PROTOCOL,
        "event_id": event_id[:128],
        "host_id": host_id,
        "host_epoch": host_epoch,
        "sequence": sequence,
        "cursor": "",
        "type": partial.get("type", ""),
        "observed_at": observed_at,
    }
    for key in ("resource_id", "resource_revision", "payload"):
        if key in partial:
            full[key] = partial[key]
    try:
        full["cursor"] = encode_cursor(cursor_secret, {"host_id": host_id, "sequence": sequence})
    except CursorError as exc:
        raise ValueError("cursor secret misconfigured") from exc
    try:
        validate_metadata_event(full)
    except SchemaError as exc:
        raise ValueError(f"invalid event: {exc}") from exc
    return full


@dataclass
class FakeBackend(Backend):
    """Deterministic in-memory backend for tests and development."""

    now_ms: Callable[[], int] = field(default=lambda: 0)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    event_logs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    _command_owners: dict[str, tuple[str, str]] = field(default_factory=dict)
    ownership: LeaseStore = field(default_factory=LeaseStore)
    rings: RingStore = field(default_factory=RingStore)
    attach_index: dict[tuple[str, str], str] = field(default_factory=dict)
    resize_log: list[ResizeRequest] = field(default_factory=list)
    output_logs: dict[str, list[bytes]] = field(default_factory=dict)
    input_logs: dict[str, list[tuple[int, int, str, bytes]]] = field(default_factory=dict)
    offline_hosts: set[str] = field(default_factory=set)
    _followers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]]] = (
        field(default_factory=dict)
    )
    _event_index: EventLogIndex = field(default_factory=EventLogIndex)

    def snapshot(self, host_id: str) -> dict[str, Any]:
        if host_id in self.offline_hosts:
            raise BackendError("host_offline", f"host {host_id} offline", True)
        try:
            return self.snapshots[host_id]
        except KeyError:
            raise BackendError("not_found", f"unknown host {host_id}") from None

    def events_after(self, host_id: str, sequence: int) -> list[dict[str, Any]]:
        if host_id in self.offline_hosts:
            raise BackendError("host_offline", f"host {host_id} offline", True)
        return [e for e in self.event_logs.get(host_id, []) if e.get("sequence", 0) > sequence]

    def publish(self, host_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append to the journal with the next sequence; wake followers.

        Thread-safe: followers may live on the ASGI loop while the
        publisher runs elsewhere (Herdr subscriber, tests).
        """
        published = dict(event, sequence=self._event_index.next_sequence(self.event_logs, host_id))
        self._event_index.append(self.event_logs, host_id, published)
        self._wake(host_id, published)
        return published

    def publish_raw(self, host_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Fault injection: append with an explicit sequence (may gap)."""
        self._event_index.append(self.event_logs, host_id, event)
        self._wake(host_id, event)
        return event

    def _wake(self, host_id: str, event: dict[str, Any]) -> None:
        for loop, queue in self._followers.get(host_id, []):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    async def follow(
        self, host_id: str, after_sequence: int, heartbeat_s: float = 15.0
    ) -> AsyncIterator[dict[str, Any] | None]:
        if host_id in self.offline_hosts:
            raise BackendError("host_offline", f"host {host_id} offline", True)
        # Subscribe before replaying: anything published in between is
        # delivered twice and deduplicated downstream by sequence.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        entry = (asyncio.get_running_loop(), queue)
        self._followers.setdefault(host_id, []).append(entry)
        try:
            for event in self.events_after(host_id, after_sequence):
                yield event
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                except TimeoutError:
                    yield None
        finally:
            followers = self._followers.get(host_id, [])
            if entry in followers:
                followers.remove(entry)

    @staticmethod
    def _canonical(command: dict[str, Any]) -> str:
        return _json.dumps(
            {
                "host_id": command.get("host_id"),
                "kind": command.get("kind"),
                "target": command.get("target"),
                "payload": command.get("payload"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        cid = str(command["command_id"])
        current = self.commands.get(cid)
        if current is not None:
            if self._command_owners.get(cid) != (
                str(command.get("principal", "")),
                str(command["host_id"]),
            ):
                raise BackendError("command_conflict", "command belongs to another principal")
            if current.get("canonical") == self._canonical(command):
                return {k: v for k, v in current.items() if k not in ("payload", "canonical")}
            raise BackendError("command_conflict", f"command {cid} conflicts")
        record = {
            "protocol": GATEWAY_PROTOCOL,
            "command_id": cid,
            "host_id": command["host_id"],
            "kind": command["kind"],
            "target": command["target"],
            "status": "accepted",
            "updated_ms": self.now_ms(),
            "payload": command.get("payload", {}),
            "canonical": self._canonical(command),
        }
        self.commands[cid] = record
        self._command_owners[cid] = (str(command.get("principal", "")), str(command["host_id"]))
        return {k: v for k, v in record.items() if k not in ("payload", "canonical")}

    def agents_page(self, host_id: str, limit: int, offset: int) -> dict[str, Any]:
        snapshot = self.snapshot(host_id)
        agents = snapshot.get("agents", [])
        page = agents[offset : offset + limit]
        return {
            "host_id": host_id,
            "agents": [
                {
                    "terminal_id": a.get("terminal_id", ""),
                    "name": a.get("name", ""),
                    "agent": a.get("agent", ""),
                    "title": a.get("title", ""),
                    "status": a.get("status", "unknown"),
                    "focused": bool(a.get("focused", False)),
                    "revision": int(a.get("revision", 0)),
                }
                for a in page
            ],
            "total": len(agents),
        }

    def command_status(self, command_id: str) -> dict[str, Any] | None:
        record = self.commands.get(command_id)
        if record is None:
            return None
        return {k: v for k, v in record.items() if k not in ("payload", "canonical")}

    def command_owner(self, command_id: str) -> tuple[str, str] | None:
        return self._command_owners.get(command_id)

    def issue_lease(
        self, host_id: str, terminal_id: str, owner: str, generation: int, epoch: str = "e1"
    ) -> TerminalLease:
        """Legacy test/dev seam: bypasses the ownership check and pins a
        replacement control lease. Production uses create_attachment +
        acquire_control/issue_view."""
        rec = self.ownership.legacy_issue(
            host_id, terminal_id, owner, generation, epoch, self.now_ms()
        )
        return _to_lease(rec)

    def open_lease(self, lease_id: str) -> TerminalLease | None:
        rec = self.ownership.open_lease(lease_id, self.now_ms())
        return _to_lease(rec) if rec is not None else None

    def begin_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int
    ) -> bool:
        return self.ownership.begin_reader_handoff(
            lease_id, account, device_id, generation, self.now_ms()
        )

    def finish_reader_handoff(
        self, lease_id: str, account: str, device_id: str, generation: int
    ) -> TerminalLease:
        try:
            return _to_lease(
                self.ownership.finish_reader_handoff(
                    lease_id, account, device_id, generation, self.now_ms()
                )
            )
        except LeaseError as exc:
            raise _reraise(exc) from None

    def write_input(
        self, lease_id: str, generation: int, input_seq: int, kind: str, data: bytes
    ) -> bool:
        try:
            fresh = self.ownership.check_input(lease_id, generation, input_seq, self.now_ms())
        except LeaseError as exc:
            raise _reraise(exc) from None
        if fresh:
            if kind == "resize" and len(data) != 4:
                raise BackendError(
                    "forbidden", "bad resize payload", details={"reason": "bad_resize"}
                )
            self.input_logs.setdefault(lease_id, []).append((generation, input_seq, kind, data))
            if kind == "resize":
                cols, rows = struct.unpack(">HH", data)
                self.resize_log.append(ResizeRequest(lease_id, generation, input_seq, cols, rows))
        return fresh

    def read_output(self, lease_id: str, after_offset: int) -> OutputBatch:
        log = self.output_logs.get(lease_id, [])
        chunks: list[tuple[int, bytes]] = []
        offset = 0
        for data in log:
            if offset >= after_offset:
                chunks.append((offset, data))
            offset += len(data)
        return OutputBatch(tuple(chunks), offset)

    def publish_output(self, attachment_id: str, epoch: str, data: bytes) -> tuple[int, int]:
        attachment = self.ownership.get_attachment(attachment_id)
        if attachment is None or attachment.closed:
            raise BackendError("not_found", "no such attachment")
        if epoch != attachment.epoch:
            raise BackendError("stale_epoch", "ring epoch rotated")
        fanout = self.rings.get(attachment_id)
        if fanout is None:
            fanout = self.rings.rotate(attachment_id, epoch, self.now_ms())
        if fanout.ring.epoch != epoch:  # pragma: no cover - store invariant
            raise BackendError("stale_epoch", "ring epoch rotated")
        return fanout.publish(data, self.now_ms())

    def read_stream(self, lease_id: str, epoch: str, after_offset: int) -> StreamBatch:
        rec = self.ownership.open_lease(lease_id, self.now_ms())
        if rec is None:
            raise BackendError("lease_expired", "no such lease")
        current = rec.epoch
        if epoch and epoch != current:
            return StreamBatch("stale_epoch", (), self._ring_end(rec.attachment_id), current)
        if not epoch and after_offset > 0:
            # No epoch knowledge plus a nonzero offset: the cursor cannot
            # address this stream (docstring rule). Zero-byte stale batch,
            # mirroring the stale path above; offset 0 still first-connects.
            return StreamBatch("stale_epoch", (), self._ring_end(rec.attachment_id), current)
        if after_offset < 0:
            raise BackendError("forbidden", "bad resume offset")
        fanout = self.rings.get(rec.attachment_id)
        if fanout is None:
            if after_offset == 0:
                return StreamBatch(STATUS_OK, (), 0, current)
            return StreamBatch("stale_offset", (), 0, current)
        ring = fanout.ring
        if after_offset == 0 and not epoch:
            # First connect without epoch knowledge: serve from the live
            # edge. The client derives completeness from the first chunk
            # offset (0 = full replay, >0 = incomplete-history notice).
            read = ring.read(current, ring.base_offset)
        else:
            read = ring.read(current, after_offset)
        return StreamBatch(read.status, read.chunks, read.next_offset, read.epoch)

    def _ring_end(self, attachment_id: str) -> int:
        fanout = self.rings.get(attachment_id)
        return fanout.ring.end_offset if fanout is not None else 0

    def close_lease(self, lease_id: str) -> None:
        self.ownership.hard_remove(lease_id)

    def create_attachment(
        self,
        host_id: str,
        terminal_id: str,
        kind: str,
        account: str,
        device_id: str,
        *,
        takeover: bool,
        owner_label: str = "",
        expected_control_revision: str | None = None,
    ) -> tuple[Attachment, tuple[str, ...]]:
        try:
            attachment, displaced = self.ownership.attach(
                host_id=host_id,
                terminal_id=terminal_id,
                kind=kind,
                account=account,
                device_id=device_id,
                takeover=takeover,
                owner_label=owner_label,
                now_ms=self.now_ms(),
                expected_control_revision=expected_control_revision,
            )
        except LeaseError as exc:
            raise _reraise(exc) from None
        # First attach opens the ring; every takeover rebinds it so old
        # offsets can never address the new stream .
        self.rings.rotate(attachment.attachment_id, attachment.epoch, self.now_ms())
        self.attach_index[(host_id, terminal_id)] = attachment.attachment_id
        return attachment, displaced

    def get_attachment(self, attachment_id: str) -> Attachment | None:
        return self.ownership.get_attachment(attachment_id)

    def acquire_control(
        self,
        attachment_id: str,
        account: str,
        device_id: str,
        *,
        expected_generation: int | None = None,
    ) -> TerminalLease:
        try:
            rec = self.ownership.acquire_control(
                attachment_id,
                account,
                device_id,
                self.now_ms(),
                expected_generation=expected_generation,
            )
        except LeaseError as exc:
            raise _reraise(exc) from None
        return _to_lease(rec)

    def issue_view(self, attachment_id: str, account: str, device_id: str) -> TerminalLease:
        try:
            rec = self.ownership.issue_view(attachment_id, account, device_id, self.now_ms())
        except LeaseError as exc:
            raise _reraise(exc) from None
        return _to_lease(rec)

    def control_holder(
        self, host_id: str, terminal_id: str, account: str, device_id: str
    ) -> dict[str, Any]:
        return self.ownership.control_holder(
            host_id, terminal_id, account, device_id, self.now_ms()
        )

    def renew_lease(self, lease_id: str, account: str, device_id: str) -> TerminalLease:
        try:
            rec = self.ownership.renew(lease_id, account, device_id, self.now_ms())
        except LeaseError as exc:
            raise _reraise(exc) from None
        return _to_lease(rec)

    def release_lease(self, lease_id: str, account: str, device_id: str) -> Attachment:
        try:
            return self.ownership.release(lease_id, account, device_id)
        except LeaseError as exc:
            raise _reraise(exc) from None

    def lease_host(self, lease_id: str) -> str | None:
        return self.ownership.lease_host(lease_id)

    def close_attachment(self, attachment_id: str, account: str) -> tuple[str, ...]:
        try:
            dead = self.ownership.close_attachment(attachment_id, account)
        except LeaseError as exc:
            raise _reraise(exc) from None
        self.rings.drop(attachment_id)
        for key, att_id in list(self.attach_index.items()):
            if att_id == attachment_id:
                del self.attach_index[key]
        return dead

    def revoke_leases_account(self, account: str) -> tuple[str, ...]:
        return self.ownership.revoke_account(account)

    def revoke_leases_device(self, account: str, device_id: str) -> tuple[str, ...]:
        return self.ownership.revoke_device(account, device_id)

    def revoke_leases_host(self, host_id: str) -> tuple[str, ...]:
        return self.ownership.revoke_host(host_id)

    def append_output(self, lease_id: str, data: bytes) -> None:
        self.output_logs.setdefault(lease_id, []).append(data)

    def fanout_for(self, attachment_id: str) -> StreamFanout | None:
        return self.rings.get(attachment_id)

    def epochs_for_host(self, host_id: str, account: str) -> list[dict[str, str]]:
        return [
            {
                "terminal_id": att.terminal_id,
                "epoch": att.epoch,
                "kind": att.kind,
                "mode": self.ownership.upstream_mode(att.attachment_id, self.now_ms()),
            }
            for att in self.ownership.attachments_for_host(host_id, account)
        ]

    def reset_terminal(
        self, host_id: str, terminal_id: str, epoch: str, account: str
    ) -> tuple[str, ...]:
        att = self.resolve_attachment(host_id, terminal_id)
        if att is None or att.account != account or att.epoch != epoch:
            return ()
        renewed, dead = self.ownership.rotate_attachment(att.attachment_id)
        self.rings.rotate(renewed.attachment_id, renewed.epoch, self.now_ms())
        return dead

    def rotate_streams(self, host_id: str) -> tuple[str, ...]:
        dead: list[str] = []
        for att, leases in self.ownership.rotate_host(host_id):
            self.rings.rotate(att.attachment_id, att.epoch, self.now_ms())
            dead.extend(leases)
        return tuple(dead)

    def resolve_attachment(self, host_id: str, terminal_id: str) -> Attachment | None:
        att_id = self.attach_index.get((host_id, terminal_id))
        if att_id is None:
            return None
        attachment = self.ownership.get_attachment(att_id)
        if attachment is None or attachment.closed or attachment.host_id != host_id:
            return None
        return attachment

    def ingest_terminal_output(
        self,
        host_id: str,
        terminal_id: str,
        epoch: str,
        data: bytes,
        now_ms: int,
    ) -> dict[str, Any]:
        attachment = self.resolve_attachment(host_id, terminal_id)
        if attachment is None:
            return {"error": "not_found"}
        try:
            start, end = self.publish_output(attachment.attachment_id, epoch, data)
        except BackendError as exc:
            return {"error": exc.code}
        return {"ok": [start, end]}

    def ingest_snapshot(
        self, host_id: str, snapshot: dict[str, Any], now_ms: int, cursor_secret: bytes
    ) -> dict[str, Any]:
        if host_id in self.offline_hosts:
            return {"error": "host_offline"}
        host = snapshot.get("host", {})
        if not isinstance(host, dict) or host.get("id") != host_id:
            return {"error": "forbidden"}
        sequence = self._event_index.last_sequence(self.event_logs, host_id)
        try:
            stamped = dict(
                snapshot,
                cursor=encode_cursor(cursor_secret, {"host_id": host_id, "sequence": sequence}),
            )
        except CursorError as exc:
            return {"error": "forbidden", "detail": str(exc)}
        self.snapshots[host_id] = stamped
        return {"ok": True}

    def ingest_event(
        self,
        host_id: str,
        partial: dict[str, Any],
        now_ms: int,
        cursor_secret: bytes,
    ) -> dict[str, Any]:
        if host_id in self.offline_hosts:
            return {"error": "host_offline"}
        wanted = partial.get("event_id") if isinstance(partial, dict) else None
        if isinstance(wanted, str) and wanted:
            existing = self._event_index.find(self.event_logs, host_id, wanted)
            if existing is not None:
                return {"ok": existing, "duplicate": True}
        sequence = self._event_index.next_sequence(self.event_logs, host_id)
        try:
            stored = prepare_event(host_id, partial, sequence, cursor_secret, now_ms)
        except ValueError as exc:
            return {"error": "forbidden", "detail": str(exc)}
        self._event_index.append(self.event_logs, host_id, stored)
        self._wake(host_id, stored)
        return {"ok": stored}

    def publish_command_updated(
        self,
        status_record: dict[str, Any],
        host_epoch: str,
        cursor_secret: bytes,
        now_ms: int,
    ) -> dict[str, Any] | None:
        partial = build_command_updated(status_record, host_epoch)
        if partial is None:
            return None
        host_id = str(status_record.get("host_id", ""))
        if not host_id:
            return None
        event_id = str(partial["event_id"])
        existing = self._event_index.find(self.event_logs, host_id, event_id)
        if existing is not None:
            return existing
        try:
            stored = prepare_event(
                host_id,
                partial,
                self._event_index.next_sequence(self.event_logs, host_id),
                cursor_secret,
                now_ms,
            )
        except ValueError:
            return None
        self._event_index.append(self.event_logs, host_id, stored)
        self._wake(host_id, stored)
        return stored
