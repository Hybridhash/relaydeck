"""Durable command-journal core, shared by gateway and connector.

Persists accepted intent (command UUID, principal, target, generation,
type payload + payload digest) in stdlib sqlite3 so a gateway crash can
never lose the dedupe window, double-dispatch an intent, or blind-retry
an uncertain outcome.

Lifecycle (internal statuses; wire mapping in `wire_status`):
  accepted   journaled, never dispatched. Proven undispatched: the
             `dispatched` marker is written BEFORE bytes hit the wire
             (write-ahead), so absence of the marker is authoritative.
  dispatched marker journaled, handed to the tunnel pump. May or may not
             have executed upstream.
  acked      connector confirmed intent receipt (its own journal accepted
             the UUID). Still no outcome.
  succeeded / failed / rejected
             terminal outcomes. `rejected` = never executed (refused or
             expired before dispatch); `failed` = executed, upstream error.
  unknown    dispatched/acked, then crashed or timed out before any
             outcome. Reconcile explicitly via `record()` (which carries
             any recorded partial progress); NEVER blind-retry.
  expired    accepted past its TTL without dispatch. Proven non-execution
             (same write-ahead argument as accepted).

TTL rule (`expire_sweep`): accepted past deadline -> expired; dispatched
or acked past deadline -> unknown (it may have executed; expiry proves
nothing after dispatch). Enforced on the live path at BOTH enqueue
(`submit_command`/`drain_outbox` sweep first) and dispatch
(`mark_dispatched` refuses past-deadline intents, settling expired):
expired resubmits return expired/conflict, never accepted.

Retention and tombstone policy (acceptance-documented):
  * Full records (payload + result bodies) are kept while non-terminal
    and for TERMINAL_FULL_MS (24 h) after reaching a terminal status.
  * Older terminal records are reduced to tombstones by `purge()`:
    routing fields (id, host, kind, target, digest, status, timestamps)
    survive, payload/result bodies are dropped. Tombstones still dedupe
    (same ID + same digest) and still conflict (same ID + changed
    digest), so the dedupe window outlives the bodies.
  * Tombstones older than TOMBSTONE_MS (7 days) are purged entirely. A
    resubmitted ID after purge is a NEW command (documented dedupe-window
    limit, dedupe holds "within the documented retention
    window", not forever).
  * Raw terminal-frame receipts (`frame_receipts`) are a SEPARATE table:
    an input-seq ACK proves receipt at the connector boundary, never
    command execution or agent progress. Frame rows are dropped
    by `purge()` after FRAME_RECEIPT_MS (1 h); they never influence
    command status and command transitions never consult them.

Durability policy: the database file is created 0600 (matching the
connector credential-file precedent in enrollment.py) and re-chmodded on
open; `journal_mode=WAL` + `synchronous=FULL` (every commit fsyncs);
one explicit commit per mutation through the `_commit` choke point,
which re-chmod(2) existing WAL sidecars to 0600 after every commit
(they inherit the process umask at creation). Residual window, stated
honestly: prompt bytes are world-readable between sqlite creating (or
recreating after checkpoint-delete) a sidecar mid-transaction and the
post-commit chmod, and a crash in that window leaves umask-perm
sidecars until the next open/commit re-chmod. One connection shared
across threads (`check_same_thread` off) behind a lock, because
FastAPI serves sync routes from a thread pool while tests and pumps
may share the handle.

Wire gap (proposed, `relaydeck/contracts/schemas.py` v1 command-record): the v1 command-record
schema has no `acked` / `expired` statuses and no `result_digest`
field, so `wire_status` maps acked -> dispatched and expired ->
rejected (+error_code "expired"), and digests stay journal-side until
the schema grows them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from relaydeck.contracts.canonical import canonical_json
from relaydeck.contracts.schemas import GATEWAY_PROTOCOL
from relaydeck.offload import OrderedGate, run_blocking

SCHEMA_VERSION = 1
P = ParamSpec("P")
T = TypeVar("T")

TERMINAL_STATUSES = ("succeeded", "failed", "rejected", "expired")
NONTERMINAL_STATUSES = ("accepted", "dispatched", "acked", "unknown")
ALL_STATUSES = NONTERMINAL_STATUSES + TERMINAL_STATUSES

# Retention policy (documented above; acceptance item).
TERMINAL_FULL_MS = 24 * 60 * 60 * 1000
TOMBSTONE_MS = 7 * 24 * 60 * 60 * 1000
FRAME_RECEIPT_MS = 60 * 60 * 1000
# Accepted intents without an explicit expires_at die after this long so
# commands never queue indefinitely while the home Mac is offline.
DEFAULT_ACCEPT_TTL_MS = 5 * 60 * 1000

MAX_RESULT_BYTES = 4096
MAX_PROGRESS_BYTES = 4096


class JournalConflict(Exception):
    """Same command ID, different canonical body (command_conflict)."""

    def __init__(self, command_id: str) -> None:
        super().__init__(f"command {command_id} conflicts")
        self.code = "command_conflict"
        self.command_id = command_id


def canonical_body(host_id: str, kind: str, target: Any, payload: Any) -> str:
    """Canonical bytes covered by the payload digest (same field set as
    FakeBackend._canonical so in-memory and durable dedupe agree)."""
    return canonical_json(
        {"host_id": host_id, "kind": kind, "target": target, "payload": payload},
    )


def digest_for(host_id: str, kind: str, target: Any, payload: Any) -> str:
    return hashlib.sha256(canonical_body(host_id, kind, target, payload).encode()).hexdigest()


def wire_status(status: str) -> str:
    """Map an internal status onto the v1 command-record enum. acked is
    still dispatched from the client's view (intent downstream, no
    outcome); expired never executed, hence rejected (error_code
    "expired" disambiguates; see `status`)."""
    if status == "acked":
        return "dispatched"
    if status == "expired":
        return "rejected"
    return status


_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
  command_id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  device_id TEXT NOT NULL DEFAULT '',
  host_id TEXT NOT NULL,
  host_epoch TEXT NOT NULL DEFAULT '',
  generation INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL,
  target_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  result_digest TEXT NOT NULL DEFAULT '',
  result_json TEXT NOT NULL DEFAULT '',
  progress_json TEXT NOT NULL DEFAULT '',
  error_code TEXT NOT NULL DEFAULT '',
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  expires_ms INTEGER NOT NULL DEFAULT 0,
  tombstone INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE INDEX IF NOT EXISTS idx_commands_host ON commands(host_id);
CREATE TABLE IF NOT EXISTS frame_receipts (
  lease_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  input_seq INTEGER NOT NULL,
  received_ms INTEGER NOT NULL,
  PRIMARY KEY (lease_id, generation, input_seq)
);
"""


def _utcnow_ms() -> int:
    return int(time.time() * 1000)


class CommandJournal:
    """Thread-safe durable intent store. See module docstring for the
    lifecycle, retention/tombstone, and durability policies."""

    def __init__(self, path: str | Path, *, clock: Callable[[], int] | None = None) -> None:
        self._path = Path(os.path.expanduser(str(path)))
        self._clock = clock or _utcnow_ms
        self._lock = threading.Lock()
        self._async_lock = OrderedGate()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Pre-create 0600 so sqlite never inherits a lax umask for the
        # main file (creds-file precedent: enrollment._write_private).
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, stat.S_IRUSR | stat.S_IWUSR)
        os.close(fd)
        os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL;")
            self._db.execute("PRAGMA synchronous=FULL;")
            row = self._db.execute("SELECT v FROM journal_meta WHERE k='version'").fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO journal_meta (k, v) VALUES ('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif row["v"] != str(SCHEMA_VERSION):
                raise RuntimeError(f"journal schema v{row['v']} != v{SCHEMA_VERSION}")
            self._commit()
        self._chmod_sidecars()

    def _commit(self) -> None:
        """Single mutation choke point: commit, then re-chmod(2) any WAL
        sidecars to 0600 (they inherit the umask at creation). Call with
        the lock held, like every other `self._db` use."""
        self._db.commit()
        self._chmod_sidecars()

    def _chmod_sidecars(self) -> None:
        for suffix in ("-wal", "-shm"):
            try:
                sidecar = Path(str(self._path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    @property
    def path(self) -> Path:
        return self._path

    def durability(self) -> dict[str, Any]:
        """Live-connection durability pragmas (WAL + FULL), for tests/ops."""
        with self._lock:
            mode = self._db.execute("PRAGMA journal_mode;").fetchone()[0]
            sync = self._db.execute("PRAGMA synchronous;").fetchone()[0]
            return {"journal_mode": str(mode).lower(), "synchronous": int(sync)}

    def now(self) -> int:
        return self._clock()

    def _now(self, now: int | None) -> int:
        return self._clock() if now is None else now

    async def run(self, call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """FIFO loop callers; keep write-ahead completion before the next operation."""
        async with self._async_lock.hold():
            return await run_blocking(call, *args, **kwargs)

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                self._commit()
            finally:
                self._db.close()

    # -- intent ---------------------------------------------------------

    def submit(
        self,
        *,
        command_id: str,
        principal: str,
        host_id: str,
        kind: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        device_id: str = "",
        host_epoch: str = "",
        generation: int = 0,
        expires_ms: int = 0,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Persist an accepted intent. Same ID + same digest returns the
        CURRENT wire record (dedupe: never re-dispatch); same ID +
        changed digest raises JournalConflict. TTL is enforced here: a
        resubmitted row past its deadline settles first (accepted ->
        expired, dispatched/acked -> unknown), so an expired resubmit
        returns expired/conflict, never accepted; a fresh submit whose
        explicit expiry is already past lands expired, never accepted."""
        at = self._now(now)
        digest = digest_for(host_id, kind, target, payload)
        target_raw = canonical_json(target)
        payload_raw = canonical_json(payload)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is not None:
                if row["digest"] != digest or row["principal"] != principal:
                    raise JournalConflict(command_id)
                current = dict(row)
                settled = self._settle_if_past_deadline(current, at)
                if settled is not None:
                    current["status"] = settled
                    current["updated_ms"] = at
                return self._wire(current)
            initial = "expired" if (expires_ms and expires_ms <= at) else "accepted"
            self._db.execute(
                "INSERT INTO commands (command_id, principal, device_id, host_id,"
                " host_epoch, generation, kind, target_json, payload_json, digest,"
                " status, created_ms, updated_ms, expires_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    command_id,
                    principal,
                    device_id,
                    host_id,
                    host_epoch,
                    generation,
                    kind,
                    target_raw,
                    payload_raw,
                    digest,
                    initial,
                    at,
                    at,
                    expires_ms,
                ),
            )
            self._commit()
            fresh = self._db.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            return self._wire(dict(fresh))

    def record(self, command_id: str) -> dict[str, Any] | None:
        """Full internal record (explicit reconcile query): lifecycle
        status, digests, partial progress, tombstone flag. Unknown
        outcomes are reconciled HERE, never by resubmission."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                return None
            return self._internal(dict(row))

    def status(self, command_id: str) -> dict[str, Any] | None:
        """Schema-valid wire record (v1 command-record), or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                return None
            return self._wire(dict(row))

    def owner_of(self, command_id: str) -> tuple[str, str] | None:
        """(principal, host_id) for authorization checks, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT principal, host_id FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                return None
            return (str(row["principal"]), str(row["host_id"]))

    # -- lifecycle transitions (each returns True when it moved) --------

    def mark_dispatched(self, command_id: str, now: int | None = None) -> bool:
        """Write-ahead dispatch marker: call BEFORE bytes hit the wire.
        Only accepted -> dispatched; every other state is a no-op
        (never move backward, never resurrect). Past-deadline intents
        are REFUSED: the row settles to expired (proven never sent)
        and False is returned so the pump skips the send."""
        at = self._now(now)
        with self._lock:
            row = self._db.execute(
                "SELECT status, created_ms, expires_ms FROM commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if row is None or row["status"] != "accepted":
                return False
            if self._settle_if_past_deadline(
                {
                    "command_id": command_id,
                    "status": row["status"],
                    "created_ms": row["created_ms"],
                    "expires_ms": row["expires_ms"],
                },
                at,
            ):
                return False
            self._db.execute(
                "UPDATE commands SET status='dispatched', updated_ms=? WHERE command_id=?",
                (at, command_id),
            )
            self._commit()
            return True

    def mark_acked(self, command_id: str, now: int | None = None) -> bool:
        """Connector confirmed intent receipt (its journal holds the
        UUID). Only dispatched -> acked; idempotent on acked."""
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is not None and row["status"] == "acked":
                return True
        return self._move(command_id, ("dispatched",), "acked", self._now(now))

    def mark_result(
        self,
        command_id: str,
        outcome: str,
        *,
        result_digest: str = "",
        result: dict[str, Any] | None = None,
        error_code: str = "",
        now: int | None = None,
    ) -> bool:
        """Record a terminal outcome. succeeded/failed (execution claims)
        only from dispatched/acked/unknown: such a result for a
        never-dispatched (accepted) intent is refused (forged outcomes
        for unsent intents fail closed). rejected (refused before
        execution) is also accepted from accepted: a refusal proves
        non-execution. Any resurrection of a terminal record is refused."""
        if outcome not in ("succeeded", "failed", "rejected"):
            raise ValueError(f"bad outcome {outcome}")
        raw = ""
        if result is not None:
            raw = canonical_json(result)
            if len(raw.encode()) > MAX_RESULT_BYTES:
                raise ValueError("result too large")
        at = self._now(now)
        allowed = (
            ("accepted", "dispatched", "acked", "unknown")
            if outcome == "rejected"
            else ("dispatched", "acked", "unknown")
        )
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None or row["status"] not in allowed:
                return False
            self._db.execute(
                "UPDATE commands SET status=?, result_digest=?, result_json=?,"
                " error_code=?, updated_ms=? WHERE command_id=?",
                (outcome, result_digest, raw, error_code, at, command_id),
            )
            self._commit()
            return True

    def mark_unknown(self, command_id: str, now: int | None = None) -> bool:
        """Crash/timeout after dispatch: outcome uncertain, reconcile
        only. Only from dispatched/acked; accepted (proven unsent) never
        becomes unknown."""
        return self._move(command_id, ("dispatched", "acked"), "unknown", self._now(now))

    def note_progress(
        self, command_id: str, progress: dict[str, Any], now: int | None = None
    ) -> bool:
        """Record partial multi-step identities (created tab/pane ids)
        so reconcile-after-crash reuses them instead of duplicating
        creation. Non-terminal records only."""
        raw = canonical_json(progress)
        if len(raw.encode()) > MAX_PROGRESS_BYTES:
            raise ValueError("progress too large")
        at = self._now(now)
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES:
                return False
            self._db.execute(
                "UPDATE commands SET progress_json=?, updated_ms=? WHERE command_id=?",
                (raw, at, command_id),
            )
            self._commit()
            return True

    def _move(self, command_id: str, allowed: tuple[str, ...], to: str, at: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM commands WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None or row["status"] not in allowed:
                return False
            self._db.execute(
                "UPDATE commands SET status=?, updated_ms=? WHERE command_id=?",
                (to, at, command_id),
            )
            self._commit()
            return True

    # -- expiry, retention, recovery --------------------------------------

    def _deadline(self, row: dict[str, Any]) -> int:
        explicit = int(row["expires_ms"] or 0)
        if explicit:
            return explicit
        return int(row["created_ms"]) + DEFAULT_ACCEPT_TTL_MS

    def _settle_if_past_deadline(self, row: dict[str, Any], at: int) -> str | None:
        """Settle one row past its deadline (accepted -> expired;
        dispatched/acked -> unknown). Returns the new status, or None
        when no settle applied. Call with the lock held."""
        status = str(row["status"])
        if status not in ("accepted", "dispatched", "acked"):
            return None
        if self._deadline(row) > at:
            return None
        to = "expired" if status == "accepted" else "unknown"
        self._db.execute(
            "UPDATE commands SET status=?, updated_ms=? WHERE command_id=?",
            (to, at, row["command_id"]),
        )
        self._commit()
        return to

    def expire_sweep(self, now: int | None = None) -> dict[str, list[str]]:
        """Enforce TTL: accepted past deadline -> expired (proven never
        dispatched); dispatched/acked past deadline -> unknown (may have
        executed). Returns the transitioned ids by outcome."""
        at = self._now(now)
        out: dict[str, list[str]] = {"expired": [], "unknown": []}
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM commands WHERE status IN ('accepted','dispatched','acked')"
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                if self._deadline(row) > at:
                    continue
                to = "expired" if row["status"] == "accepted" else "unknown"
                self._db.execute(
                    "UPDATE commands SET status=?, updated_ms=? WHERE command_id=?",
                    (to, at, row["command_id"]),
                )
                out[to].append(str(row["command_id"]))
            self._commit()
        return out

    def purge(self, now: int | None = None) -> dict[str, int]:
        """Apply the retention policy: terminal records older than
        TERMINAL_FULL_MS become tombstones (bodies dropped); tombstones
        older than TOMBSTONE_MS are purged; frame receipts older than
        FRAME_RECEIPT_MS are dropped. Returns counts."""
        at = self._now(now)
        with self._lock:
            cur = self._db.execute(
                "UPDATE commands SET payload_json='', result_json='', progress_json='',"
                " tombstone=1 WHERE status IN ('succeeded','failed','rejected','expired')"
                " AND tombstone=0 AND updated_ms <= ?",
                (at - TERMINAL_FULL_MS,),
            )
            tombstoned = cur.rowcount
            cur = self._db.execute(
                "DELETE FROM commands WHERE tombstone=1 AND updated_ms <= ?",
                (at - TOMBSTONE_MS,),
            )
            purged = cur.rowcount
            cur = self._db.execute(
                "DELETE FROM frame_receipts WHERE received_ms <= ?", (at - FRAME_RECEIPT_MS,)
            )
            frames = cur.rowcount
            self._commit()
        return {"tombstoned": tombstoned, "purged": purged, "frames": frames}

    def recover(self, now: int | None = None) -> dict[str, list[str]]:
        """Crash recovery after reopening the journal from disk. Every
        dispatched/acked record becomes unknown (crash after upstream
        dispatch: reconcile only, never blind-retry). Accepted records
        are proven undispatched (write-ahead marker absent) and stay
        accepted: the caller may dispatch each listed id ONCE with the
        same UUID under idempotent downstream semantics, subject to TTL
        (accepted-but-expired is swept to expired, never dispatched).
        Returns {"unknown": [...], "redispatchable": [...]}."""
        at = self._now(now)
        out: dict[str, list[str]] = {"unknown": [], "redispatchable": []}
        with self._lock:
            rows = self._db.execute(
                "SELECT command_id FROM commands WHERE status IN ('dispatched','acked')"
            ).fetchall()
            for raw in rows:
                cid = str(raw["command_id"])
                self._db.execute(
                    "UPDATE commands SET status='unknown', updated_ms=? WHERE command_id=?",
                    (at, cid),
                )
                out["unknown"].append(cid)
            pending = self._db.execute("SELECT * FROM commands WHERE status='accepted'").fetchall()
            for raw in pending:
                row = dict(raw)
                if self._deadline(row) <= at:
                    self._db.execute(
                        "UPDATE commands SET status='expired', updated_ms=? WHERE command_id=?",
                        (at, row["command_id"]),
                    )
                else:
                    out["redispatchable"].append(str(row["command_id"]))
            self._commit()
        out["unknown"].sort()
        out["redispatchable"].sort()
        return out

    # -- frame receipts (NOT command outcomes) ------------------------------

    def record_frame_receipt(
        self, lease_id: str, generation: int, input_seq: int, now: int | None = None
    ) -> bool:
        """Record one raw terminal input-frame receipt (ACK = received at
        this boundary). True when first seen, False on duplicate. Never
        touches command rows: frame receipt is not a command outcome."""
        at = self._now(now)
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO frame_receipts (lease_id, generation, input_seq,"
                    " received_ms) VALUES (?,?,?,?)",
                    (lease_id, generation, input_seq, at),
                )
            except sqlite3.IntegrityError:
                # A failed INSERT leaves a write transaction open (legacy mode never
                # auto-rolls-back), which would hold the RESERVED lock and trip close()'s
                # checkpoint. Roll back; nothing was added.
                self._db.rollback()
                return False
            self._commit()
            return True

    def is_frame_received(self, lease_id: str, generation: int, input_seq: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM frame_receipts WHERE lease_id=? AND generation=? AND input_seq=?",
                (lease_id, generation, input_seq),
            ).fetchone()
            return row is not None

    # -- shapes --------------------------------------------------------------

    @staticmethod
    def _wire(row: dict[str, Any]) -> dict[str, Any]:
        from relaydeck.contracts.launch import public_command_result

        out: dict[str, Any] = {
            "protocol": GATEWAY_PROTOCOL,
            "command_id": row["command_id"],
            "host_id": row["host_id"],
            "kind": row["kind"],
            "target": json.loads(row["target_json"]),
            "status": wire_status(str(row["status"])),
            "updated_ms": int(row["updated_ms"]),
        }
        error = str(row["error_code"] or "")
        if row["status"] == "expired" and not error:
            error = "expired"
        if error:
            out["error_code"] = error
        if row["status"] in ("succeeded", "failed") and row.get("result_json"):
            result = public_command_result(json.loads(row["result_json"]))
            if result is not None:
                out["result"] = result
        return out

    @staticmethod
    def _internal(row: dict[str, Any]) -> dict[str, Any]:
        def _grab(key: str) -> Any:
            raw = row.get(key) or ""
            return json.loads(raw) if raw else {}

        return {
            "protocol": GATEWAY_PROTOCOL,
            "command_id": row["command_id"],
            "principal": row["principal"],
            "device_id": row["device_id"],
            "host_id": row["host_id"],
            "host_epoch": row["host_epoch"],
            "generation": int(row["generation"]),
            "kind": row["kind"],
            "target": _grab("target_json"),
            "payload": _grab("payload_json"),
            "digest": row["digest"],
            "status": row["status"],
            "result_digest": row["result_digest"],
            "result": _grab("result_json"),
            "progress": _grab("progress_json"),
            "error_code": row["error_code"],
            "created_ms": int(row["created_ms"]),
            "updated_ms": int(row["updated_ms"]),
            "expires_ms": int(row["expires_ms"]),
            "tombstone": bool(row["tombstone"]),
        }
