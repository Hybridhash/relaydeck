"""Production gateway backend: journal-backed dispatch.

`JournalBackend` implements the full `Backend` ABC over durable and
real stores: command intent in `CommandJournal` (sqlite), attachment and
lease ownership in `LeaseStore`, terminal bytes in `RingStore`, and
observed metadata in injected snapshot/event stores.

Dispatch flow (write-ahead, never blind-retry):
  1. `submit_command` sweeps TTL first, then journals the intent
     (dedupe/conflict by UUID + digest) and enqueues one
     `command.dispatch` tunnel message per accepted UUID (bounded
     outbox; each UUID enqueued once per process). Expired resubmits
     return expired/conflict, never accepted.
  2. The connector-tunnel pump (`app.connector_socket`) drains the
     outbox (TTL-swept first), journals `mark_dispatched` BEFORE
     sending each message, and then sends. The marker refuses
     past-deadline intents (settling expired) so the pump never sends
     them. A crash between marker and send reconciles to unknown via
     `recover()`; a resubmitted UUID dedupes to the recorded outcome
     and is never re-enqueued while dispatched/acked/terminal.
  3. `note_command_ack` / `note_command_result` ingest connector replies
     with host-binding checks (a connector may only report its own
     host's commands). Results for never-dispatched (accepted) intents
     are refused: forged outcomes fail closed.
  4. `session.close` additionally closes the local attachment at submit
     time and stashes dead lease ids for `drain_socket_closes` (the app
     severs those sockets, mirroring DELETE /v1/terminal-attachments).
     Upstream pane.close still travels as a journaled dispatch so an
     offline connector converges when it returns.

Crash recovery runs in `__init__` via `journal.recover()`: dispatched
or acked records become unknown (reconcile only); accepted records are
proven undispatched and re-enqueued once with the SAME UUID (idempotent
downstream: the connector journal dedupes by UUID + digest).

Freshness / retention rules (enforced here):
  * `expected_revision` on a NEW agent.prompt is enforced against the
    observed snapshot before journaling: the target agent must exist
    and its revision must equal the assertion, else stale_revision and
    nothing is journaled. Absent/zero asserts nothing; non-prompt kinds
    stay journal-only (no agent resource mapping); replays dedupe to
    their recorded outcome without re-enforcement.
  * Per-host event logs are bounded (MAX_EVENT_LOG_PER_HOST, oldest
    first) with an O(1) event_id index (EventLogIndex); resume past
    retention resyncs from what remains.
  * Snapshot/event content comes from injected observed-state stores
    (`snapshots=`, `event_logs=`), populated in production by the
    connector metadata sync: tunnel `snapshot` (schema-validated,
    host-bound, replaces) and `event` (server-stamped sequence/cursor,
    `event_id` dedupe) via `ingest_snapshot` / `ingest_event`.

The app emits command.updated on accepted, dispatched and terminal transitions,
deduplicated by command ID/status. Status polling is account/host bound. Typed
generation-bound terminal input and resize use the authenticated connector tunnel.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from relaydeck.gateway.backend import (
    Backend,
    BackendError,
    EventLogIndex,
    OutputBatch,
    ResizeRequest,
    StreamBatch,
    TerminalLease,
    _reraise,
    _to_lease,
    build_command_updated,
    prepare_event,
)
from relaydeck.gateway.cursors import CursorError, encode_cursor
from relaydeck.gateway.leases import Attachment, LeaseError, LeaseStore
from relaydeck.gateway.metadata_follower import MAX_METADATA_FOLLOWERS, MetadataFollower
from relaydeck.gateway.ring import STATUS_OK, RingStore, StreamFanout
from relaydeck.journal import CommandJournal, JournalConflict
from relaydeck.offload import OrderedGate

MAX_OUTBOX_PER_HOST = 128


def _utcnow_ms() -> int:
    return int(time.time() * 1000)


class JournalBackend(Backend):
    """Full ABC implementation over durable + real stores."""

    requires_connector = True

    def __init__(
        self,
        journal: CommandJournal,
        *,
        now_ms: Callable[[], int] | None = None,
        ownership: LeaseStore | None = None,
        rings: RingStore | None = None,
        snapshots: dict[str, dict[str, Any]] | None = None,
        event_logs: dict[str, list[dict[str, Any]]] | None = None,
        offline_hosts: set[str] | None = None,
        record_inputs: bool = False,
    ) -> None:
        self.journal = journal
        self.command_lock = OrderedGate()
        self.now_ms = now_ms or _utcnow_ms
        self.ownership = ownership or LeaseStore()
        self.rings = rings or RingStore()
        self.snapshots: dict[str, dict[str, Any]] = snapshots if snapshots is not None else {}
        self.event_logs: dict[str, list[dict[str, Any]]] = (
            event_logs if event_logs is not None else {}
        )
        self.offline_hosts: set[str] = offline_hosts if offline_hosts is not None else set()
        self.output_logs: dict[str, list[bytes]] = {}
        # Accepted input is forwarded and then forgotten: keeping it would grow without bound and
        # hold typed secrets in memory. Tests opt in to observe what was accepted.
        self.record_inputs = record_inputs
        self.input_logs: dict[str, list[tuple[int, int, str, bytes]]] = {}
        self.resize_log: list[ResizeRequest] = []
        self.attach_index: dict[tuple[str, str], str] = {}
        self._outbox: dict[str, list[dict[str, Any]]] = {}
        self._reserved_outbox: dict[str, set[str]] = {}
        self._enqueued: set[str] = set()
        self._event_index = EventLogIndex()
        self._socket_closes: list[str] = []
        self._followers: dict[str, list[MetadataFollower]] = {}
        self._metadata_lock = threading.RLock()
        # Crash recovery: unknown-ify dispatched/acked; re-enqueue proven
        # undispatched intents once with the same UUID (idempotent).
        recovered = self.journal.recover(self.now_ms())
        for command_id in recovered["redispatchable"]:
            try:
                self._enqueue(command_id)
            except BackendError:
                break  # outbox bound: leftovers stay accepted until TTL.

    def close(self) -> None:
        self.journal.close()

    # -- metadata (observed-state stores; sync not implemented, see docstring) ---

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
        with self._metadata_lock:
            published = dict(
                event, sequence=self._event_index.next_sequence(self.event_logs, host_id)
            )
            self._append_metadata(host_id, published)
            return published

    def _append_metadata(self, host_id: str, event: dict[str, Any]) -> None:
        with self._metadata_lock:
            self._event_index.append(self.event_logs, host_id, event)
            for follower in self._followers.get(host_id, []):
                follower.offer(event)

    async def follow(
        self, host_id: str, after_sequence: int, heartbeat_s: float = 15.0
    ) -> AsyncIterator[dict[str, Any] | None]:
        if host_id in self.offline_hosts:
            raise BackendError("host_offline", f"host {host_id} offline", True)
        entry = MetadataFollower(asyncio.get_running_loop())
        with self._metadata_lock:
            if sum(map(len, self._followers.values())) >= MAX_METADATA_FOLLOWERS:
                raise BackendError("rate_limited", "Metadata follower capacity reached", True)
            for event in self.events_after(host_id, after_sequence):
                entry.offer(event)
            self._followers.setdefault(host_id, []).append(entry)
        try:
            while True:
                yield await entry.next(heartbeat_s)
        finally:
            entry.close()
            with self._metadata_lock:
                followers = self._followers.get(host_id, [])
                if entry in followers:
                    followers.remove(entry)
                if not followers:
                    self._followers.pop(host_id, None)

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

    # -- journal-backed commands -------------------------------------------

    def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        cid = str(command["command_id"])
        at = self.now_ms()
        self.journal.expire_sweep(at)  # TTL at enqueue: expired rows settle first.
        self.prepare_command(command, self.journal.record(cid))
        record, current = self.persist_command(command, at)
        return self.accept_command(record, current)

    def prepare_command(self, command: dict[str, Any], existing: dict[str, Any] | None) -> None:
        """Advisory loop-owned checks; the connector re-verifies generation before execution."""
        host_id = str(command["host_id"])
        if existing is None:
            # New intent only: replays dedupe to their recorded outcome
            # below and are never re-enforced (idempotent replay wins).
            self._enforce_expected_revision(command, host_id)
            if (
                len(self._outbox.get(host_id, ())) + len(self._reserved_outbox.get(host_id, ()))
            ) >= MAX_OUTBOX_PER_HOST:
                # Refuse before durable acceptance/local close effects; a
                # later recovery must not execute a supposedly refused ID.
                raise BackendError("rate_limited", "connector outbox full", True)

    def persist_command(
        self, command: dict[str, Any], at: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Journal-only phase; safe on the journal's worker lane."""
        cid, host_id = str(command["command_id"]), str(command["host_id"])
        try:
            record = self.journal.submit(
                command_id=cid,
                principal=str(command.get("principal", "")),
                device_id=str(command.get("device_id", "")),
                host_id=host_id,
                host_epoch=str(command.get("host_epoch", "")),
                generation=int(command.get("expected_revision") or 0),
                kind=str(command["kind"]),
                target=dict(command["target"]),
                payload=dict(command.get("payload", {})),
                expires_ms=int(command.get("expires_ms") or 0),
                now=at,
            )
        except JournalConflict as exc:
            raise BackendError("command_conflict", str(exc)) from None
        current = self.journal.record(cid)
        assert current is not None  # noqa: S101
        return record, current

    def accept_command(self, record: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Finish loop-owned effects only after durable acceptance."""
        if current["status"] == "accepted":
            if current["kind"] == "session.close":
                self._close_local_for_command(current)
            self.enqueue_record(str(current["command_id"]), current)
        return record

    def _enforce_expected_revision(self, command: dict[str, Any], host_id: str) -> None:
        """Refuse a stale agent.prompt before it journals (stale_revision).

        A truthy `expected_revision` is an explicit freshness assertion
        against the observed snapshot: the target agent (target.agent_id
        = snapshot terminal_id) must exist and its revision must equal
        the asserted one. Absent/zero asserts nothing (backward
        compatible); non-prompt kinds have no agent resource mapping and
        stay journal-only. Fails closed: an unverifiable assertion never
        dispatches.
        """
        expected = command.get("expected_revision")
        if expected is None:
            return
        if type(expected) is not int or expected < 0:
            raise BackendError("stale_revision", "invalid revision assertion")
        if expected == 0:
            return
        kind = str(command.get("kind", ""))
        target = command.get("target")
        agent_id = target.get("agent_id") if isinstance(target, dict) else None
        if kind != "agent.prompt" or not agent_id:
            return
        wanted = expected
        try:
            snapshot = self.snapshot(host_id)
        except BackendError as exc:
            raise BackendError(
                "stale_revision",
                "cannot verify expected_revision: no snapshot",
                details={"reason": "no_snapshot"},
            ) from exc
        agents = snapshot.get("agents", [])
        observed: int | None = None
        if isinstance(agents, list):
            for entry in agents:
                if isinstance(entry, dict) and entry.get("terminal_id") == agent_id:
                    revision = entry.get("revision", 0)
                    observed = revision if isinstance(revision, int) else 0
                    break
        if observed is None:
            raise BackendError(
                "stale_revision",
                f"unknown agent {agent_id}",
                details={"reason": "unknown_agent"},
            )
        if observed != wanted:
            raise BackendError(
                "stale_revision",
                f"agent {agent_id} moved (expected {wanted}, observed {observed})",
                details={
                    "reason": "revision_mismatch",
                    "expected": wanted,
                    "observed": observed,
                },
            )

    def _close_local_for_command(self, current: dict[str, Any]) -> None:
        """session.close: close the local
        attachment now and stash dead leases for socket severance. The
        upstream pane.close still travels as a journaled dispatch."""
        target = current.get("target") or {}
        terminal_id = str(target.get("terminal_id", ""))
        attachment = self.resolve_attachment(str(current["host_id"]), terminal_id)
        if attachment is None:
            return
        try:
            dead = self.close_attachment(attachment.attachment_id, str(current["principal"]))
        except BackendError:
            return  # Local race (already closed); upstream dispatch still covers it.
        self._socket_closes.extend(dead)

    def _enqueue(self, command_id: str) -> None:
        current = self.journal.record(command_id)
        self.enqueue_record(command_id, current)

    def enqueue_record(self, command_id: str, current: dict[str, Any] | None) -> None:
        """Apply an already-read journal record on the event-loop thread."""
        if command_id in self._enqueued:
            return
        if current is None or current["status"] != "accepted":
            return
        host_id = str(current["host_id"])
        if command_id in self._reserved_outbox.get(host_id, ()):
            return
        pending = self._outbox.setdefault(host_id, [])
        if len(pending) + len(self._reserved_outbox.get(host_id, ())) >= MAX_OUTBOX_PER_HOST:
            raise BackendError("rate_limited", "connector outbox full", True)
        pending.append(
            {
                "kind": "command.dispatch",
                "command_id": command_id,
                "principal": current["principal"],
                "host_id": host_id,
                "gateway_kind": current["kind"],
                "host_epoch": current["host_epoch"],
                "target": current["target"],
                "payload": current["payload"],
                "digest": current["digest"],
                "generation": current["generation"],
                "expires_ms": current["expires_ms"],
            }
        )
        self._enqueued.add(command_id)

    def command_status(self, command_id: str) -> dict[str, Any] | None:
        self.journal.expire_sweep(self.now_ms())
        return self.journal.status(command_id)

    def command_owner(self, command_id: str) -> tuple[str, str] | None:
        return self.journal.owner_of(command_id)

    # -- seam -------------------------------------------------------

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

    def drain_outbox(self, host_id: str) -> list[dict[str, Any]]:
        # TTL at enqueue: accepted rows past their deadline settle to
        # expired here; the pump's dispatch marker re-checks per id, so
        # a message drained just before its deadline still never sends.
        self.journal.expire_sweep(self.now_ms())
        return self.take_outbox(host_id)

    def take_outbox(self, host_id: str, *, reserve: bool = False) -> list[dict[str, Any]]:
        """Drain loop-owned messages after the durable expiry sweep completes."""
        pending = self._outbox.pop(host_id, [])
        for message in pending:
            self._enqueued.discard(str(message.get("command_id", "")))
            if reserve:
                self._reserved_outbox.setdefault(host_id, set()).add(str(message["command_id"]))
        return pending

    def restore_outbox(
        self, host_id: str, pending: list[dict[str, Any]], taken: list[dict[str, Any]] | None = None
    ) -> None:
        """Preserve accepted intents; a durable marker still refuses uncertain resends."""
        reserved = self._reserved_outbox.get(host_id, set())
        reserved.difference_update(
            str(m["command_id"]) for m in (taken if taken is not None else pending)
        )
        if not reserved:
            self._reserved_outbox.pop(host_id, None)
        now = self.now_ms()
        restored = [
            m
            for m in pending
            if str(m.get("command_id", "")) not in self._enqueued
            and (not m.get("expires_ms") or int(m["expires_ms"]) > now)
        ]
        self._outbox[host_id] = restored + self._outbox.get(host_id, [])
        self._enqueued.update(str(m["command_id"]) for m in restored)

    def mark_command_dispatched(self, command_id: str, now_ms: int) -> bool:
        # TTL at dispatch: past-deadline intents are refused (settled
        # expired) so the pump skips the send. False also covers stale
        # redelivery (already dispatched/settled): never send twice.
        return self.journal.mark_dispatched(command_id, now=now_ms)

    def note_command_ack(self, command_id: str, host_id: str, now_ms: int) -> dict[str, Any] | None:
        owner = self.journal.owner_of(command_id)
        if owner is None or owner[1] != host_id:
            return None  # Unknown id or cross-host injection: refuse.
        self.journal.mark_acked(command_id, now=now_ms)
        return self.journal.status(command_id)

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
        owner = self.journal.owner_of(command_id)
        if owner is None or owner[1] != host_id:
            return None
        if outcome == "unknown":
            self.journal.mark_unknown(command_id, now=now_ms)
            return self.journal.status(command_id)
        if outcome not in ("succeeded", "failed", "rejected"):
            return None
        try:
            recorded = self.journal.mark_result(
                command_id,
                outcome,
                result_digest=result_digest,
                result=result,
                error_code=error_code,
                now=now_ms,
            )
        except ValueError:
            # E.g. result past the 4 KiB cap: refuse the message (the
            # tunnel closes 1008) instead of raising out of the handler.
            return None
        if not recorded:
            return None  # e.g. result for a never-dispatched intent: refuse.
        return self.journal.status(command_id)

    def drain_socket_closes(self) -> tuple[str, ...]:
        pending = tuple(self._socket_closes)
        del self._socket_closes[:]
        return pending

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
        self._append_metadata(host_id, stored)
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
        self._append_metadata(host_id, stored)
        return stored

    # -- leases, rings, input (real stores; FakeBackend parity) --------------

    def issue_lease(
        self, host_id: str, terminal_id: str, owner: str, generation: int, epoch: str = "e1"
    ) -> TerminalLease:
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
            if self.record_inputs:
                self.input_logs.setdefault(lease_id, []).append((generation, input_seq, kind, data))
                if kind == "resize":
                    cols, rows = struct.unpack(">HH", data)
                    self.resize_log.append(
                        ResizeRequest(lease_id, generation, input_seq, cols, rows)
                    )
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

    def append_output(self, lease_id: str, data: bytes) -> None:
        self.output_logs.setdefault(lease_id, []).append(data)

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

    def control_holder(
        self, host_id: str, terminal_id: str, account: str, device_id: str
    ) -> dict[str, Any]:
        return self.ownership.control_holder(
            host_id, terminal_id, account, device_id, self.now_ms()
        )

    def issue_view(self, attachment_id: str, account: str, device_id: str) -> TerminalLease:
        try:
            rec = self.ownership.issue_view(attachment_id, account, device_id, self.now_ms())
        except LeaseError as exc:
            raise _reraise(exc) from None
        return _to_lease(rec)

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
