"""Private numeric usage ledger; replacement and checkpoints commit together."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relaydeck.contracts.canonical import canonical_json
from relaydeck.usage.contracts import (
    CAPACITY_EXCEEDED,
    LIMIT_STALE_MS,
    MAX_CREDITS_MILLI,
    MAX_CURSOR_BYTES,
    MAX_CURSOR_STATE_BYTES,
    MAX_CURSOR_STATE_ENTRIES,
    MAX_EVENTS_PER_BATCH,
    MAX_FILE_CONTEXT_BYTES,
    MAX_KEY_CHARS,
    MAX_MODEL_CHARS,
    MAX_PLAN_LABEL_CHARS,
    MAX_QUERY_DAYS,
    MAX_QUERY_ROWS,
    MAX_SOURCES,
    MAX_TRACKED_FILES,
    RETENTION_DAYS,
    ApplyResult,
    Batch,
    Counters,
    Coverage,
    Cursor,
    FileCursor,
    KindCoverage,
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    PaneCwdObservation,
    RowScope,
    SessionMapping,
    UsageEvent,
    UsageQuery,
    UsageReport,
    UsageRow,
    UsageUnavailable,
    encoded_cursor_bytes,
    encoded_state_bytes,
    parse_space_id,
    space_wire_id,
)
from relaydeck.usage.pricing import PriceTable

_COVERAGE = {
    Coverage.COMPLETE: 0,
    Coverage.PARTIAL: 1,
    Coverage.UNVERIFIED: 2,
    Coverage.UNAVAILABLE: 3,
}
_LIMIT = {
    LimitStatus.FRESH: 0,
    LimitStatus.STALE: 1,
    LimitStatus.EXPIRED: 2,
    LimitStatus.NOT_REPORTED: 3,
}
_UNIT_RE = re.compile(r"[a-z_]{1,16}\Z")


_PLAN_LABEL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._-")


def _validate_plan_label(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_PLAN_LABEL_CHARS
        or any(c not in _PLAN_LABEL_CHARS for c in value)
    ):
        raise UsageUnavailable()


def _validate_amounts(window: LimitWindow) -> None:
    """Amount fields: finite, non-negative, validated unit, strict bool."""
    if any(
        amount is not None
        and (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0)
        for amount in (window.used_amount, window.limit_amount)
    ) or (
        window.unit is not None
        and (not isinstance(window.unit, str) or _UNIT_RE.fullmatch(window.unit) is None)
    ):
        raise UsageUnavailable()
    if type(window.over_allowance) is not bool:
        raise UsageUnavailable()


def _json(value: Any) -> str:
    return canonical_json(value, allow_nan=False)


def _key(value: str, maximum: int = MAX_KEY_CHARS) -> None:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise UsageUnavailable()


def _price_digest(prices: PriceTable) -> str:
    payload = {
        "rates": [asdict(r) for r in prices.rates],
        "provenance": list(prices.provenance),
        "sources": [list(s) for s in prices.snapshot_sources],
        "labels": [list(s) for s in prices.snapshot_labels],
    }
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def _bump_generation(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO meta VALUES('data_generation','1') ON CONFLICT(key) "
        "DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)"
    )


_SQL_TOTALS = (
    "json_array(json_object("
    "'input_uncached',SUM(CAST(json_extract(counters,'$.input_uncached') AS INTEGER)),"
    "'cache_read',SUM(CAST(json_extract(counters,'$.cache_read') AS INTEGER)),"
    "'cache_write_short',SUM(CAST(json_extract(counters,'$.cache_write_short') AS INTEGER)),"
    "'cache_write_long',SUM(CAST(json_extract(counters,'$.cache_write_long') AS INTEGER)),"
    "'output',SUM(CAST(json_extract(counters,'$.output') AS INTEGER)),"
    "'reasoning',SUM(CAST(json_extract(counters,'$.reasoning') AS INTEGER)),"
    "'cache_write_unknown',SUM(CAST(json_extract(counters,'$.cache_write_unknown') AS INTEGER))),"
    "CASE WHEN SUM(unp)>0 THEN NULL ELSE SUM(cost_micro) END,"
    "CASE WHEN SUM(unp)>0 THEN NULL WHEN COUNT(DISTINCT price_pin)=0 THEN NULL "
    "WHEN COUNT(DISTINCT price_pin)=1 THEN MIN(price_pin) ELSE 'mixed' END,"
    "SUM(CASE WHEN cost_micro IS NOT NULL THEN cost_micro ELSE 0 END),"
    "SUM(CASE WHEN cost_micro IS NOT NULL THEN tt ELSE 0 END),SUM(unp*tt),SUM(credits_milli))"
)


def _source(source: str, kind: str) -> str:
    parts = source.split("/")
    if len(parts) != 3 or parts[1] != kind:
        raise UsageUnavailable()
    for part in parts:
        _key(part)
    return parts[0]


def _safe_path(path: Path) -> None:
    if not path.is_absolute():
        raise UsageUnavailable()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise UsageUnavailable()


class SqliteUsageStore:
    """Host-partitioned ledger, with optional prices and injectable epoch-ms clock.

    Default prices are absent, never inferred. `clock` supplies apply's retention time;
    query/prune use their explicit now_ms. Neither optional input changes UsageStore.
    """

    def __init__(
        self,
        path: Path,
        timezone: str,
        *,
        prices: PriceTable | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.timezone = timezone
        self.prices = prices or PriceTable()
        self._price_digest = _price_digest(self.prices)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._zone = ZoneInfo(timezone)
            _safe_path(path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _safe_path(path)
            if path.parent.stat().st_uid != os.getuid():
                raise UsageUnavailable()
            os.chmod(path.parent, 0o700)
            for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                if file.exists() or file.is_symlink():
                    info = file.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        raise UsageUnavailable()
                    os.chmod(file, 0o600)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                    raise UsageUnavailable()
                self._db = sqlite3.connect(
                    path, timeout=1, isolation_level=None, check_same_thread=False
                )
                after = path.lstat()
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    self._db.close()
                    raise UsageUnavailable()
            finally:
                os.close(fd)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors(source TEXT PRIMARY KEY,
                  body TEXT NOT NULL,revision INTEGER NOT NULL,updated INTEGER NOT NULL,
                  retired INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS cursor_files(source TEXT NOT NULL,file_key TEXT NOT NULL,
                  dev INTEGER NOT NULL,ino INTEGER NOT NULL,offset INTEGER NOT NULL,
                  size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,head_digest TEXT NOT NULL,
                  context TEXT NOT NULL DEFAULT '',retired INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(source,file_key));
                CREATE TABLE IF NOT EXISTS receipts(source TEXT,revision INTEGER,
                  digest TEXT,updated INTEGER,PRIMARY KEY(source,revision));
                CREATE TABLE IF NOT EXISTS events(host TEXT,kind TEXT,source TEXT,
                  session TEXT,event TEXT,occurred INTEGER,day TEXT,model TEXT,
                  counters TEXT,verified INTEGER,parent_session_key TEXT,
                  meter TEXT NOT NULL DEFAULT 'tokens',
                  credits_milli INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(host,kind,source,session,event));
                CREATE INDEX IF NOT EXISTS event_days ON events(host,day,occurred);
                CREATE INDEX IF NOT EXISTS event_sessions ON events(source,session,occurred);
                CREATE TABLE IF NOT EXISTS mappings(source TEXT,session TEXT,interval TEXT,
                  host TEXT,kind TEXT,workspace TEXT,first INTEGER,last INTEGER,
                  generation INTEGER NOT NULL DEFAULT 0,label TEXT,repo_fp TEXT,
                  rank INTEGER NOT NULL DEFAULT 1,
                  PRIMARY KEY(source,session,interval));
                CREATE INDEX IF NOT EXISTS mapping_times ON mappings(source,session,first,last);
                CREATE TABLE IF NOT EXISTS session_cwd(source TEXT,session TEXT,
                  digest TEXT,updated INTEGER,PRIMARY KEY(source,session));
                CREATE TABLE IF NOT EXISTS pane_cwd(source TEXT,interval TEXT,
                  host TEXT,kind TEXT,digest TEXT,workspace TEXT,generation INTEGER,
                  first INTEGER,last INTEGER,PRIMARY KEY(source,interval));
                CREATE INDEX IF NOT EXISTS pane_cwd_match
                  ON pane_cwd(source,kind,digest,first,last);
                CREATE TABLE IF NOT EXISTS mapping_gaps(source TEXT,gap TEXT,host TEXT,kind TEXT,
                  first INTEGER,last INTEGER,PRIMARY KEY(source,gap));
                CREATE INDEX IF NOT EXISTS mapping_gap_times ON mapping_gaps(source,first,last);
                CREATE TABLE IF NOT EXISTS limits(host TEXT,kind TEXT,account TEXT,
                  quota TEXT,window TEXT,observed INTEGER,body TEXT,status TEXT,
                  source TEXT,error TEXT,plan_label TEXT,retired INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(host,kind,account,quota,window));
                CREATE TABLE IF NOT EXISTS coverage(source TEXT PRIMARY KEY,host TEXT,
                  kind TEXT,value TEXT,observed INTEGER,updated INTEGER,
                  retired INTEGER NOT NULL DEFAULT 0);
            """)
            with self._transaction() as db:
                row = db.execute("SELECT value FROM meta WHERE key='timezone'").fetchone()
                if row and row[0] != timezone:
                    raise UsageUnavailable()
                self._migrate_schema(db)
                self._migrate_cursors(db)
                db.execute("INSERT OR IGNORE INTO meta VALUES('timezone',?)", (timezone,))
        except (OSError, sqlite3.Error, ValueError, ZoneInfoNotFoundError, UsageUnavailable):
            if hasattr(self, "_db"):
                self._db.close()
            raise UsageUnavailable() from None

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        """Upgrade prior ledgers atomically, retaining surviving rows and checkpoints."""
        if "parent_session_key" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
            db.execute("ALTER TABLE events ADD COLUMN parent_session_key TEXT")
        # the meter each event is accounted in; old rows read as token events.
        events = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if "meter" not in events:
            db.execute("ALTER TABLE events ADD COLUMN meter TEXT NOT NULL DEFAULT 'tokens'")
        if "credits_milli" not in events:
            db.execute("ALTER TABLE events ADD COLUMN credits_milli INTEGER NOT NULL DEFAULT 0")
        # price once at ingest (digest tracks the pricing table).
        if "cost_micro" not in events:
            db.execute("ALTER TABLE events ADD COLUMN cost_micro INTEGER")
        if "price_pin" not in events:
            db.execute("ALTER TABLE events ADD COLUMN price_pin TEXT")
        if "price_digest" not in events:
            db.execute("ALTER TABLE events ADD COLUMN price_digest TEXT")
        # workspace generations and episode labels; old rows read as generation 0.
        columns = {row[1] for row in db.execute("PRAGMA table_info(mappings)")}
        if "generation" not in columns:
            db.execute("ALTER TABLE mappings ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
        if "label" not in columns:
            db.execute("ALTER TABLE mappings ADD COLUMN label TEXT")
        if "repo_fp" not in columns:
            db.execute("ALTER TABLE mappings ADD COLUMN repo_fp TEXT")
        # evidence ladder rank; old rows read as rank 1.
        if "rank" not in columns:
            db.execute("ALTER TABLE mappings ADD COLUMN rank INTEGER NOT NULL DEFAULT 1")
        # unconfigured sources retire instead of lingering;
        # old rows start live (the next startup marks what is gone).
        for table in ("coverage", "cursors", "cursor_files", "limits"):
            if "retired" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
        primary = tuple(
            row[1]
            for row in sorted(db.execute("PRAGMA table_info(limits)"), key=lambda row: row[5])
            if row[5]
        )
        if primary == ("host", "kind", "account", "quota", "window"):
            # snapshot plan labels; old rows read as None.
            if "plan_label" not in {row[1] for row in db.execute("PRAGMA table_info(limits)")}:
                db.execute("ALTER TABLE limits ADD COLUMN plan_label TEXT")
            return
        if primary not in (("host", "kind", "quota", "window"), ("kind", "quota", "window")):
            raise UsageUnavailable("corrupt")
        # replacement is keyed per account, and rows carry their provider
        # source and fetch error. Legacy rows keep their data with account "",
        # source "" and no error.
        db.execute("ALTER TABLE limits RENAME TO limits_legacy")
        db.execute("""CREATE TABLE limits(host TEXT,kind TEXT,account TEXT,
            quota TEXT,window TEXT,observed INTEGER,body TEXT,status TEXT,
            source TEXT,error TEXT,plan_label TEXT,retired INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(host,kind,account,quota,window))""")
        db.execute("""INSERT INTO
            limits(host,kind,account,quota,window,observed,body,status,source,error)
            SELECT host,kind,'',quota,window,observed,body,status,'',NULL
            FROM limits_legacy""")
        db.execute("DROP TABLE limits_legacy")

    @staticmethod
    def _migrate_cursors(db: sqlite3.Connection) -> None:
        if "context" not in {row[1] for row in db.execute("PRAGMA table_info(cursor_files)")}:
            db.execute("ALTER TABLE cursor_files ADD COLUMN context TEXT NOT NULL DEFAULT ''")
        version = db.execute("SELECT value FROM meta WHERE key='cursor_files_version'").fetchone()
        if version:
            if version[0] != "1":
                raise UsageUnavailable("corrupt")
            return
        try:
            for source, raw in db.execute("SELECT source,body FROM cursors"):
                body = json.loads(raw)
                files = {key: FileCursor(**value) for key, value in body["files"].items()}
                state = dict(body["state"])
                db.executemany(
                    "INSERT INTO cursor_files(source,file_key,dev,ino,offset,size,"
                    "mtime_ns,head_digest,context) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            source,
                            key,
                            file.dev,
                            file.ino,
                            file.offset,
                            file.size,
                            file.mtime_ns,
                            file.head_digest,
                            file.context,
                        )
                        for key, file in files.items()
                    ),
                )
                db.execute("UPDATE cursors SET body=? WHERE source=?", (_json(state), source))
            db.execute("INSERT INTO meta VALUES('cursor_files_version','1')")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UsageUnavailable("corrupt") from None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise UsageUnavailable("closed")
            try:
                self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield self._db
                self._db.commit()
            except BaseException as exc:
                if self._db.in_transaction:
                    self._db.rollback()
                if isinstance(exc, sqlite3.Error):
                    code = getattr(exc, "sqlite_errorcode", 0) & 255
                    raise UsageUnavailable(
                        "busy"
                        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        else "corrupt"
                    ) from None
                raise

    def _horizon(self, now_ms: int, db: sqlite3.Connection) -> int:
        today = datetime.fromtimestamp(now_ms / 1000, self._zone).date()
        earliest = today - timedelta(days=RETENTION_DAYS - 1)
        cutoff = int(datetime.combine(earliest, day_time(), self._zone).timestamp() * 1000)
        row = db.execute("SELECT value FROM meta WHERE key='cutoff'").fetchone()
        return max(cutoff, int(row[0]) if row else cutoff)

    @staticmethod
    def _cursor_body(cursor: Cursor) -> str:
        if len(cursor.files) > MAX_TRACKED_FILES or len(cursor.state) > MAX_CURSOR_STATE_ENTRIES:
            raise UsageUnavailable()
        for key, value in cursor.state.items():
            _key(key)
            if not isinstance(value, str):
                raise UsageUnavailable()
        if encoded_state_bytes(cursor.state) > MAX_CURSOR_STATE_BYTES:
            raise UsageUnavailable()
        for key, file in cursor.files.items():
            _key(key)
            if (
                any(
                    type(n) is not int or n < 0
                    for n in (file.dev, file.ino, file.offset, file.size, file.mtime_ns)
                )
                or file.offset > file.size
            ):
                raise UsageUnavailable()
            if len(file.head_digest) != 64 or any(
                c not in "0123456789abcdef" for c in file.head_digest
            ):
                raise UsageUnavailable()
            if (
                not isinstance(file.context, str)
                or len(file.context.encode()) > MAX_FILE_CONTEXT_BYTES
            ):
                raise UsageUnavailable()
        if encoded_cursor_bytes(cursor) > MAX_CURSOR_BYTES:
            raise UsageUnavailable()
        return _json(
            {
                "files": {
                    k: {
                        name: value
                        for name, value in asdict(v).items()
                        if name != "context" or value != ""
                    }
                    for k, v in cursor.files.items()
                },
                "state": dict(cursor.state),
            }
        )

    def cursor(self, source_key: str) -> Cursor:
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT body,revision FROM cursors WHERE source=?", (source_key,)
            ).fetchone()
            if row is None:
                return Cursor()
            try:
                state = json.loads(row[0])
                return Cursor(
                    {
                        key: FileCursor(*values)
                        for key, *values in db.execute(
                            "SELECT file_key,dev,ino,offset,size,mtime_ns,head_digest,context "
                            "FROM cursor_files WHERE source=?",
                            (source_key,),
                        )
                    },
                    state,
                    row[1],
                )
            except (TypeError, ValueError, KeyError):
                raise UsageUnavailable("corrupt") from None

    def apply(
        self,
        kind: str,
        batch: Batch,
        mappings: tuple[SessionMapping, ...],
        *,
        source_key: str,
        cwd: tuple[PaneCwdObservation, ...] = (),
    ) -> ApplyResult:
        try:
            return self._apply(kind, batch, mappings, source_key=source_key, cwd=cwd)
        except (ValueError, TypeError, OverflowError):
            raise UsageUnavailable() from None

    def apply_limits(
        self,
        kind: str,
        provider_key: str,
        snapshots: tuple[LimitSnapshot, ...],
        observed_ms: int,
    ) -> None:
        """Limits-plane write: atomically replace, per (host, kind, account, quota),
        the full latest window set. Windows a reading no longer reports are removed;
        a zero-window snapshot replaces its quota's set with an empty one. Other
        accounts' readings are untouched, cursors and revisions are untouched, and a
        reading older than the retention horizon is skipped. Never call this with a
        failed fetch: failures keep last-good by not writing at all."""
        try:
            self._apply_limits(kind, provider_key, snapshots, observed_ms)
        except (ValueError, TypeError, OverflowError):
            raise UsageUnavailable() from None

    def record_limits_failure(
        self,
        kind: str,
        provider_key: str,
        error: LimitError,
        observed_ms: int,
        sources: Collection[str] = (),
    ) -> int:
        """Limits-plane failure: decide whether the previous reading may still be shown.

        Last-good is reused only for an account that was positively identified. A reading with no
        account identity, or any `auth_required` failure (the sign-in may now be another account or
        none), is replaced by an error reading with no windows, so the old percentage is never
        shown. A transient failure (timeout, unavailable, offline, parse) for an identified account
        keeps its readings, which then age. Other sources and other accounts' stored rows are not
        read as current by the display path (see `_limits`). The block's own readings are the rows
        whose source is its format, or one of `sources` (the source labels it has written).
        Returns the number of quotas suppressed."""
        host = _source(provider_key, kind)
        names = sorted({provider_key.split("/")[2], *sources} - {""})
        LimitError(error)
        if type(observed_ms) is not int or observed_ms < 0:
            raise UsageUnavailable()
        suppressed = 0
        with self._transaction() as db:
            marks = ",".join("?" for _ in names)
            groups = db.execute(
                "SELECT DISTINCT account,quota,source FROM limits "
                f"WHERE host=? AND kind=? AND source IN ({marks}) AND retired=0",
                (host, kind, *names),
            ).fetchall()
            for account, quota, source in groups:
                if account and error is not LimitError.AUTH_REQUIRED:
                    continue  # a known account with a transient error: last-good, aged
                db.execute(
                    "DELETE FROM limits WHERE host=? AND kind=? AND account=? AND quota=?",
                    (host, kind, account, quota),
                )
                db.execute(
                    "INSERT INTO limits(host,kind,account,quota,window,observed,body,"
                    "status,source,error,retired) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        host,
                        kind,
                        account,
                        quota,
                        "",
                        observed_ms,
                        None,
                        LimitStatus.NOT_REPORTED.value,
                        source,
                        error.value,
                        0,
                    ),
                )
                suppressed += 1
        if groups:
            _bump_generation(db)
        return suppressed

    def _apply_limits(
        self,
        kind: str,
        provider_key: str,
        snapshots: tuple[LimitSnapshot, ...],
        observed_ms: int,
    ) -> None:
        host = _source(provider_key, kind)
        if not isinstance(snapshots, tuple) or type(observed_ms) is not int or observed_ms < 0:
            raise UsageUnavailable()
        with self._transaction() as db:
            cutoff = self._horizon(observed_ms, db)
            for snapshot in snapshots:
                _key(snapshot.quota_key)
                if (
                    snapshot.kind != kind
                    or type(snapshot.observed_ms) is not int
                    or snapshot.observed_ms < 0
                ):
                    raise UsageUnavailable()
                if snapshot.observed_ms < cutoff:
                    continue
                account = snapshot.account_ref or ""
                if account:
                    _key(account)
                if (
                    not isinstance(snapshot.source, str)
                    or len(snapshot.source) > MAX_KEY_CHARS
                    or any(ord(c) < 32 for c in snapshot.source)
                ):
                    raise UsageUnavailable()
                if snapshot.error is not None:
                    LimitError(snapshot.error)
                LimitStatus(snapshot.status)
                _validate_plan_label(snapshot.plan_label)
                windows: tuple[LimitWindow | None, ...] = snapshot.windows or (None,)
                for window in windows:
                    if window is not None:
                        _key(window.window_key)
                        if (
                            type(window.used_percent) not in (int, float)
                            or not math.isfinite(window.used_percent)
                            or not 0 <= window.used_percent <= 100
                            or (
                                window.window_minutes is not None
                                and (
                                    type(window.window_minutes) is not int
                                    or window.window_minutes <= 0
                                )
                            )
                            or (
                                window.resets_at_ms is not None
                                and (
                                    type(window.resets_at_ms) is not int or window.resets_at_ms < 0
                                )
                            )
                        ):
                            raise UsageUnavailable()
                        LimitStatus(window.status)
                        _validate_amounts(window)
                db.execute(
                    "DELETE FROM limits WHERE host=? AND kind=? AND account=? AND quota=?",
                    (host, kind, account, snapshot.quota_key),
                )
                for window in windows:
                    db.execute(
                        "INSERT INTO limits(host,kind,account,quota,window,observed,body,"
                        "status,source,error,plan_label,retired) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            host,
                            kind,
                            account,
                            snapshot.quota_key,
                            window.window_key if window else "",
                            snapshot.observed_ms,
                            _json(asdict(window)) if window else None,
                            snapshot.status,
                            snapshot.source,
                            snapshot.error,
                            snapshot.plan_label,
                            0,
                        ),
                    )
        _bump_generation(db)

    def _apply(
        self,
        kind: str,
        batch: Batch,
        mappings: tuple[SessionMapping, ...],
        *,
        source_key: str,
        cwd: tuple[PaneCwdObservation, ...],
    ) -> ApplyResult:
        host = _source(source_key, kind)
        if (
            len(batch.events) > MAX_EVENTS_PER_BATCH
            or len(mappings) > MAX_TRACKED_FILES
            or len(cwd) > MAX_TRACKED_FILES
            or len(batch.mapping_gaps) > MAX_SOURCES
            or type(batch.expected_revision) is not int
            or batch.expected_revision < 0
        ):
            raise UsageUnavailable()
        Coverage(batch.coverage)
        body = self._cursor_body(batch.next_cursor)
        own = tuple(
            sorted(
                (m for m in mappings if m.source_key == source_key),
                key=lambda m: (m.session_key, m.interval_id),
            )
        )
        own_cwd = tuple(
            sorted(
                (o for o in cwd if o.source_key == source_key),
                key=lambda o: o.interval_id,
            )
        )
        digest = hashlib.sha256(
            _json(
                {
                    "kind": kind,
                    "batch": {
                        "events": [asdict(e) for e in batch.events],
                        "cursor": body,
                        "coverage": batch.coverage,
                        "limits": [asdict(s) for s in batch.limits],
                        "skipped": batch.skipped,
                        # Preserve legacy receipts for a batch without new gap data.
                        **(
                            {"mapping_gaps": [asdict(g) for g in batch.mapping_gaps]}
                            if batch.mapping_gaps
                            else {}
                        ),
                    },
                    "mappings": [asdict(m) for m in own],
                    # Pane observations ride the same receipt when present.
                    **({"cwd": [asdict(o) for o in own_cwd]} if own_cwd else {}),
                }
            ).encode()
        ).hexdigest()
        now = self._clock()
        with self._transaction() as db:
            applied = db.execute(
                "SELECT digest FROM receipts WHERE source=? AND revision=?",
                (source_key, batch.expected_revision),
            ).fetchone()
            if applied and applied[0] == digest:
                return ApplyResult()
            row = db.execute(
                "SELECT revision,body FROM cursors WHERE source=?", (source_key,)
            ).fetchone()
            if (row[0] if row else 0) != batch.expected_revision or applied:
                raise UsageUnavailable("stale_batch")
            cutoff = self._horizon(now, db)
            db.execute("INSERT OR REPLACE INTO meta VALUES('cutoff',?)", (str(cutoff),))
            events_changed = self._apply_events(db, host, kind, source_key, batch.events, cutoff)
            for gap in batch.mapping_gaps:
                _key(gap.gap_id)
                if (
                    gap.source_key != source_key
                    or type(gap.first_ms) is not int
                    or gap.first_ms < 0
                    or (
                        gap.last_ms is not None
                        and (type(gap.last_ms) is not int or gap.last_ms < gap.first_ms)
                    )
                ):
                    raise UsageUnavailable()
                prior_gap = db.execute(
                    "SELECT first FROM mapping_gaps WHERE source=? AND gap=?",
                    (source_key, gap.gap_id),
                ).fetchone()
                if prior_gap and prior_gap[0] != gap.first_ms:
                    raise UsageUnavailable()
                db.execute(
                    "INSERT INTO mapping_gaps VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(source,gap) DO UPDATE SET last=excluded.last",
                    (source_key, gap.gap_id, host, kind, gap.first_ms, gap.last_ms),
                )
            for m in own:
                for key in (m.session_key, m.interval_id, m.workspace_id):
                    _key(key)
                if m.label is not None:
                    _key(m.label, 128)
                if m.repo_fp is not None:
                    _key(m.repo_fp)
                if (
                    m.host_id != host
                    or m.kind != kind
                    or type(m.first_seen_ms) is not int
                    or type(m.last_seen_ms) is not int
                    or m.first_seen_ms < 0
                    or m.last_seen_ms < m.first_seen_ms
                    or type(m.generation) is not int
                    or m.generation < 0
                    or type(m.rank) is not int
                    or m.rank not in (1, 2)
                ):
                    raise UsageUnavailable()
                prior = db.execute(
                    "SELECT workspace,generation,first,rank FROM mappings "
                    "WHERE source=? AND session=? AND interval=?",
                    (source_key, m.session_key, m.interval_id),
                ).fetchone()
                if prior and prior != (m.workspace_id, m.generation, m.first_seen_ms, m.rank):
                    raise UsageUnavailable()
                db.execute(
                    "INSERT INTO mappings(source,session,interval,host,kind,workspace,first,"
                    "last,generation,label,repo_fp,rank) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source,session,interval) DO UPDATE SET "
                    "last=MAX(last,excluded.last),label=excluded.label,repo_fp=excluded.repo_fp",
                    (
                        source_key,
                        m.session_key,
                        m.interval_id,
                        host,
                        kind,
                        m.workspace_id,
                        m.first_seen_ms,
                        m.last_seen_ms,
                        m.generation,
                        m.label,
                        m.repo_fp,
                        m.rank,
                    ),
                )
            self._apply_session_cwd(db, source_key, batch.events, now)
            for o in own_cwd:
                _key(o.interval_id)
                _key(o.digest)
                _key(o.workspace_id)
                if (
                    o.host_id != host
                    or o.kind != kind
                    or type(o.first_seen_ms) is not int
                    or type(o.last_seen_ms) is not int
                    or o.first_seen_ms < 0
                    or o.last_seen_ms < o.first_seen_ms
                    or type(o.generation) is not int
                    or o.generation < 0
                ):
                    raise UsageUnavailable()
                prior_cwd = db.execute(
                    "SELECT digest,workspace,generation,first FROM pane_cwd "
                    "WHERE source=? AND interval=?",
                    (source_key, o.interval_id),
                ).fetchone()
                if prior_cwd and prior_cwd != (
                    o.digest,
                    o.workspace_id,
                    o.generation,
                    o.first_seen_ms,
                ):
                    raise UsageUnavailable()
                db.execute(
                    "INSERT INTO pane_cwd(source,interval,host,kind,digest,workspace,"
                    "generation,first,last) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source,interval) DO UPDATE SET last=MAX(last,excluded.last)",
                    (
                        source_key,
                        o.interval_id,
                        host,
                        kind,
                        o.digest,
                        o.workspace_id,
                        o.generation,
                        o.first_seen_ms,
                        o.last_seen_ms,
                    ),
                )
            for snapshot in batch.limits:
                _key(snapshot.quota_key)
                if (
                    snapshot.kind != kind
                    or type(snapshot.observed_ms) is not int
                    or snapshot.observed_ms < 0
                ):
                    raise UsageUnavailable()
                if snapshot.observed_ms < cutoff:
                    continue
                _validate_plan_label(snapshot.plan_label)
                windows: tuple[LimitWindow | None, ...] = snapshot.windows or (None,)
                for window in windows:
                    if window is not None:
                        _key(window.window_key)
                        if (
                            type(window.used_percent) not in (int, float)
                            or not math.isfinite(window.used_percent)
                            or not 0 <= window.used_percent <= 100
                            or (
                                window.window_minutes is not None
                                and (
                                    type(window.window_minutes) is not int
                                    or window.window_minutes <= 0
                                )
                            )
                            or (
                                window.resets_at_ms is not None
                                and (
                                    type(window.resets_at_ms) is not int or window.resets_at_ms < 0
                                )
                            )
                        ):
                            raise UsageUnavailable()
                        LimitStatus(window.status)
                        _validate_amounts(window)
                    LimitStatus(snapshot.status)
                    db.execute(
                        "INSERT INTO limits(host,kind,account,quota,window,observed,body,"
                        "status,source,error,plan_label,retired) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(host,kind,account,quota,window) DO UPDATE SET "
                        "observed=excluded.observed,body=excluded.body,status=excluded.status,"
                        "source=excluded.source,error=excluded.error,plan_label=excluded.plan_label,"
                        "retired=0 "
                        "WHERE excluded.observed >= limits.observed",
                        (
                            host,
                            kind,
                            "",
                            snapshot.quota_key,
                            window.window_key if window else "",
                            snapshot.observed_ms,
                            _json(asdict(window)) if window else None,
                            snapshot.status,
                            snapshot.source,
                            snapshot.error,
                            snapshot.plan_label,
                            0,
                        ),
                    )
            observed = now if batch.coverage != Coverage.UNAVAILABLE else None
            db.execute(
                "INSERT INTO coverage VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "value=excluded.value,observed=COALESCE(excluded.observed,coverage.observed),"
                "updated=excluded.updated,retired=0",
                (source_key, host, kind, batch.coverage, observed, now, 0),
            )
            cursor_changed = self._write_cursor(
                db,
                source_key,
                batch.next_cursor,
                batch.expected_revision + 1,
                now,
                row[1] if row else "{}",
            )
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?)",
                (source_key, batch.expected_revision, digest, now),
            )
            if events_changed or own or own_cwd or batch.limits or batch.mapping_gaps:
                _bump_generation(db)
            return ApplyResult(cursor_changed, events_changed)

    def _apply_events(
        self,
        db: sqlite3.Connection,
        host: str,
        kind: str,
        source: str,
        events: tuple[UsageEvent, ...],
        cutoff: int,
    ) -> int:
        # Last keyed replacement wins; intermediate changes are not durable progress.
        latest: dict[tuple[str, str], tuple[Any, ...] | None] = {}
        for event in events:
            _key(event.session_key)
            _key(event.event_key)
            if event.parent_session_key is not None:
                _key(event.parent_session_key)
            if event.model is not None:
                _key(event.model, MAX_MODEL_CHARS)
            if type(event.occurred_ms) is not int or event.occurred_ms < 0:
                raise UsageUnavailable()
            if type(event.accounting_verified) is not bool:
                raise UsageUnavailable()
            if event.cwd_digest is not None:
                _key(event.cwd_digest)
            counts = asdict(event.counters)
            if (
                any(type(n) is not int or n < 0 for n in counts.values())
                or event.counters.reasoning > event.counters.output
            ):
                raise UsageUnavailable()
            # token events carry counters, credit events carry credits_milli
            # with zero counters; the two are never mixed or converted.
            if event.meter not in ("tokens", "credits"):
                raise UsageUnavailable()
            if (
                type(event.credits_milli) is not int
                or not 0 <= event.credits_milli <= MAX_CREDITS_MILLI
            ):
                raise UsageUnavailable()
            if event.meter == "tokens" and event.credits_milli != 0:
                raise UsageUnavailable()
            if event.meter == "credits" and any(counts.values()):
                raise UsageUnavailable()
            payload = (
                None
                if event.occurred_ms < cutoff
                else (
                    event.occurred_ms,
                    datetime.fromtimestamp(event.occurred_ms / 1000, self._zone).date().isoformat(),
                    event.model,
                    counts,
                    int(event.accounting_verified),
                    event.parent_session_key,
                    event.meter,
                    event.credits_milli,
                )
            )
            latest[(event.session_key, event.event_key)] = payload
        changed = 0
        for (session, event_key), payload in latest.items():
            key = (host, kind, source, session, event_key)
            if payload is None:
                changed += db.execute(
                    "DELETE FROM events WHERE host=? AND kind=? AND source=? "
                    "AND session=? AND event=?",
                    key,
                ).rowcount
                continue
            prior = db.execute(
                "SELECT occurred,day,model,counters,verified,parent_session_key,meter,"
                "credits_milli FROM events WHERE host=? AND kind=? AND source=? "
                "AND session=? AND event=?",
                key,
            ).fetchone()
            if prior and (*prior[:3], json.loads(prior[3]), *prior[4:]) == payload:
                continue
            cost_micro, price_pin = (None, None)
            if payload[6] == "tokens":
                cost_micro, price_pin = self.prices.estimate(
                    payload[1], payload[2], Counters(**payload[3])
                )
            db.execute(
                "INSERT OR REPLACE INTO events(host,kind,source,session,event,occurred,"
                "day,model,counters,verified,parent_session_key,meter,credits_milli,"
                "cost_micro,price_pin,price_digest) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *key,
                    *payload[:3],
                    _json(payload[3]),
                    *payload[4:],
                    cost_micro,
                    price_pin,
                    self._price_digest,
                ),
            )
            changed += 1
        return changed

    @staticmethod
    def _apply_session_cwd(
        db: sqlite3.Connection, source: str, events: tuple[UsageEvent, ...], now: int
    ) -> None:
        """Upsert one working-folder digest per session (last event wins); sessions
        without a digest keep any digest stored earlier."""
        latest: dict[str, str] = {}
        for event in events:
            if event.cwd_digest is not None:
                latest[event.session_key] = event.cwd_digest
        for session, digest in latest.items():
            db.execute(
                "INSERT INTO session_cwd VALUES(?,?,?,?) ON CONFLICT(source,session) "
                "DO UPDATE SET digest=excluded.digest,updated=excluded.updated",
                (source, session, digest, now),
            )

    @staticmethod
    def _write_cursor(
        db: sqlite3.Connection,
        source: str,
        cursor: Cursor,
        revision: int,
        now: int,
        old_state: str,
    ) -> bool:
        previous = {
            key: tuple(values)
            for key, *values in db.execute(
                "SELECT file_key,dev,ino,offset,size,mtime_ns,head_digest,context "
                "FROM cursor_files WHERE source=?",
                (source,),
            )
        }
        files = {
            key: (f.dev, f.ino, f.offset, f.size, f.mtime_ns, f.head_digest, f.context)
            for key, f in cursor.files.items()
        }
        state = _json(dict(cursor.state))
        changed = previous != files or old_state != state
        db.executemany(
            "DELETE FROM cursor_files WHERE source=? AND file_key=?",
            ((source, key) for key in previous.keys() - files.keys()),
        )
        db.executemany(
            "INSERT INTO cursor_files VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source,file_key) DO UPDATE SET "
            "dev=excluded.dev,ino=excluded.ino,offset=excluded.offset,size=excluded.size,"
            "mtime_ns=excluded.mtime_ns,head_digest=excluded.head_digest,context=excluded.context,"
            "retired=0",
            (
                (source, key, *value, 0)
                for key, value in files.items()
                if previous.get(key) != value
            ),
        )
        db.execute(
            "INSERT INTO cursors VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
            "body=excluded.body,revision=excluded.revision,updated=excluded.updated,"
            "retired=0",
            (source, state, revision, now, 0),
        )
        # A source that just wrote is live, including files whose checkpoints
        # did not move (the upsert above only touches changed ones).
        db.execute("UPDATE cursor_files SET retired=0 WHERE source=? AND retired=1", (source,))
        return changed

    @staticmethod
    def _attributed_sql(*, inferred: bool) -> str:
        # evidence ladder. `ambig` counts distinct spaces among the episodes
        # containing the event: more than one stays not_linked and never falls
        # through to inferred. `inf` holds sessions with exactly one candidate
        # (workspace, generation) by digest+kind+overlap; it is empty when inferred
        # linking is off, so exact attribution is then byte-identical in outcome.
        # rank 1 (reported identity) always wins over rank 2 (process
        # evidence); `ambig` is judged within the best joined rank only, so an
        # ambiguous rank 2 never falls through to inferred either.
        if inferred:
            inf = (
                ", inf AS (SELECT s.host,s.kind,s.source,s.session,"
                "COUNT(DISTINCT p.workspace || CHAR(31) || p.generation) AS n,"
                "MIN(p.workspace) AS w,MIN(p.generation) AS g FROM sess s "
                "JOIN session_cwd c ON c.source=s.source AND c.session=s.session "
                "JOIN pane_cwd p ON p.source=s.source AND p.kind=s.kind "
                "AND p.digest=c.digest AND p.first<=s.smax AND p.last>=s.smin "
                "GROUP BY s.host,s.kind,s.source,s.session)"
            )
            join = (
                " LEFT JOIN inf i ON i.host=r.host AND i.kind=r.kind "
                "AND i.source=r.source AND i.session=r.session"
            )
            inf_workspace = "WHEN r.gapped=0 AND r.ambig<=1 AND i.n=1 THEN i.w "
            inf_generation = "WHEN r.gapped=0 AND r.ambig<=1 AND i.n=1 THEN i.g "
            inf_link = "WHEN r.gapped=0 AND r.ambig<=1 AND i.n=1 THEN 'inferred' "
        else:
            inf, join = "", ""
            inf_workspace = inf_generation = inf_link = ""
        return (
            """WITH best AS (
            SELECT e.*,(SELECT id FROM (
              SELECT b.rowid AS id,b.rank AS k0,
                CASE WHEN b.first<=e.occurred AND b.last>=e.occurred THEN 0
                  WHEN b.last<e.occurred THEN 1 ELSE 2 END AS k1,
                CASE WHEN b.first<=e.occurred AND b.last>=e.occurred THEN b.first END AS k2,
                CASE WHEN b.last<e.occurred THEN b.last END AS k3,
                CASE WHEN b.first>e.occurred THEN b.first END AS k4,
                b.last AS k5,b.interval AS k6
              FROM mappings b WHERE
                b.source=e.source AND b.session=e.session AND b.host=e.host AND b.kind=e.kind)
              ORDER BY k0,k1,k2 DESC,k3 DESC,k4 ASC,k5 DESC,k6 ASC LIMIT 1) AS best_mapping
            FROM events e WHERE e.host=? AND e.day BETWEEN ? AND ? AND e.occurred>=?
        ), candidates AS (
            SELECT e.*,m.workspace AS c_workspace,m.generation AS c_generation
            FROM best e LEFT JOIN mappings m ON m.rowid=e.best_mapping
        ),
        -- One best mapping per event: the order ends on the primary key, so the choice is
        -- total and needs no window over every (event, mapping) pair. gapped and ambig
        -- depend only on the event and are computed once per event.
        ranked AS MATERIALIZED (SELECT c.*,
              CASE WHEN EXISTS(SELECT 1 FROM mapping_gaps g WHERE
              g.source=c.source AND g.first<=c.occurred AND
              (g.last IS NULL OR g.last>=c.occurred)) THEN 1 ELSE 0 END AS gapped,
              (SELECT COUNT(*) FROM (SELECT DISTINCT m2.workspace FROM mappings m2
                WHERE m2.source=c.source AND m2.session=c.session AND m2.host=c.host
                AND m2.kind=c.kind AND m2.first<=c.occurred AND m2.last>=c.occurred
                AND m2.rank=(SELECT MIN(m4.rank) FROM mappings m4
                  WHERE m4.source=c.source AND m4.session=c.session AND m4.host=c.host
                  AND m4.kind=c.kind))) AS ambig
          FROM candidates c),
        sess AS (SELECT host,kind,source,session,MIN(occurred) AS smin,MAX(occurred) AS smax
          FROM ranked GROUP BY host,kind,source,session)"""
            + inf
            + """, attributed AS (SELECT r.host,r.kind,r.source,r.session,r.event,r.occurred,
            r.day,r.model,r.counters,r.verified,r.parent_session_key,r.meter,
            r.credits_milli,r.cost_micro,r.price_pin,
            (SELECT SUM(CAST(value AS INTEGER)) FROM json_each(r.counters)
              WHERE key<>'reasoning') AS tt,
            CASE WHEN r.gapped=0 AND r.ambig<=1 AND r.c_workspace IS NOT NULL
              THEN r.c_workspace """
            + inf_workspace
            + """END AS workspace,
            CASE WHEN r.gapped=0 AND r.ambig<=1 AND r.c_workspace IS NOT NULL
              THEN r.c_generation """
            + inf_generation
            + """END AS generation,
            CASE WHEN r.gapped=0 AND r.ambig<=1 AND r.c_workspace IS NOT NULL THEN 'exact' """
            + inf_link
            + "END AS link FROM ranked r"
            + join
            + ") "
        )

    def recent_linked_workspace(self, host_id: str, kind: str) -> str | None:
        """Most recently active linked workspace for host+kind, if any.

        Tab placement: a workspace where this kind's
        usage is already linked. Name only; never paths or prompts.
        """
        _key(host_id)
        _key(kind)
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT workspace FROM mappings WHERE host=? AND kind=? ORDER BY last DESC LIMIT 1",
                (host_id, kind),
            ).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            return None
        return row[0]

    def report_snapshot(
        self, query: UsageQuery, now_ms: int, *, max_rows: int = 20_000, inferred: bool = True
    ) -> UsageReport:
        """One bounded SQL read transaction; external pagination never reruns attribution."""
        if query.cursor is not None or not 1 <= max_rows <= 20_000:
            raise UsageUnavailable("invalid_query")
        report = self.query(query, now_ms, _row_limit=max_rows, inferred=inferred)
        if report.next_cursor is not None:
            # Over the cap: an explicit capacity state with no rows, never a partial report that
            # could be read as complete. Cursors cannot fix this, so the owner narrows the filter.
            return replace(report, rows=(), next_cursor=None, capacity=CAPACITY_EXCEEDED)
        return replace(report, next_cursor=None)

    def _local_hour(self, occurred_ms: int) -> int:
        """The host-local wall-clock hour of an instant. A repeated fall-back hour gives the same
        label for both occurrences; a skipped spring-forward hour never occurs, so it has no row."""
        return datetime.fromtimestamp(occurred_ms / 1000, self._zone).hour

    def query(
        self,
        query: UsageQuery,
        now_ms: int,
        *,
        _row_limit: int = MAX_QUERY_ROWS,
        inferred: bool = True,
    ) -> UsageReport:
        try:
            first, last = date.fromisoformat(query.from_day), date.fromisoformat(query.to_day)
            if (
                first.isoformat() != query.from_day
                or last.isoformat() != query.to_day
                or not 0 <= (last - first).days < MAX_QUERY_DAYS
                or (query.hour and first != last)  # hourly buckets: exactly one day
            ):
                raise ValueError
            _key(query.host_id)
            if query.space_id is not None:
                _key(query.space_id)
            # a bare space id means the workspace's current generation (resolved
            # below); `w2~g0` names a superseded generation explicitly.
            space_ws, space_gen = (
                parse_space_id(query.space_id) if query.space_id is not None else (None, None)
            )
            fields = [query.host_id, query.from_day, query.to_day, query.space_id, self.timezone]
            if query.hour:
                fields.append("hour")  # an hourly report never accepts a daily cursor
            signature = hashlib.sha256(_json(fields).encode()).hexdigest()
            after: tuple[str, ...] = ("", "", "", "", "", "", "")
            if query.cursor is not None:
                if len(query.cursor) > 4096:
                    raise ValueError
                payload = json.loads(base64.b64decode(query.cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(payload, list)
                    or len(payload) != (8 if query.hour else 7)
                    or payload[0] != signature
                    or any(not isinstance(s, str) or len(s) > MAX_KEY_CHARS for s in payload[1:])
                ):
                    raise ValueError
                after = tuple(payload[1:]) if query.hour else (*payload[1:], "")
            # The keyset hour: -1 sorts every non-hourly row alike; hourly rows are 0-23.
            hour_after = -1
            if query.hour and query.cursor is not None:
                hour_after = int(after[6])
                if not 0 <= hour_after <= 23:
                    raise ValueError
        except (ValueError, TypeError, UsageUnavailable):
            raise UsageUnavailable("invalid_query") from None

        prices = self.prices

        class Totals:
            # Each event is priced on its own (tier thresholds are per event), then summed.
            # An event is empty (no counter nonzero), priced (cost known, possibly 0) or unpriced.
            # credit events bypass token accounting entirely; their milli-credits sum.
            def __init__(self) -> None:
                self.counts = Counters()
                self.cost: int | None = 0
                self.pins: set[str] = set()
                self.priced_cost = 0
                self.priced_tokens = 0
                self.unpriced_tokens = 0
                self.credits_milli = 0

            def step(self, *args: Any) -> None:
                day, model, counters, meter, credits = args
                if meter == "credits":
                    self.credits_milli += credits
                    return
                value = Counters(**json.loads(counters))
                self.counts = self.counts + value
                cost, pin = prices.estimate(day, model, value)
                if pin is not None:
                    self.pins.add(pin)
                self.cost = None if cost is None or self.cost is None else self.cost + cost
                tokens = value.total
                if tokens == 0:
                    return
                if cost is None:
                    self.unpriced_tokens += tokens
                else:
                    self.priced_cost += cost
                    self.priced_tokens += tokens

            def finalize(self) -> str:
                pin = (
                    None
                    if self.cost is None or not self.pins
                    else (next(iter(self.pins)) if len(self.pins) == 1 else "mixed")
                )
                return _json(
                    [
                        asdict(self.counts),
                        self.cost,
                        pin,
                        self.priced_cost,
                        self.priced_tokens,
                        self.unpriced_tokens,
                        self.credits_milli,
                    ]
                )

        with self._transaction(write=False) as db:
            db.create_function("usage_hour", 1, self._local_hour, deterministic=True)
            args = (query.host_id, query.from_day, query.to_day, self._horizon(now_ms, db))
            # SQL sums over ingest-time prices; legacy Python
            # aggregate only while rows in range are unpriced/stale.
            stale = db.execute(
                "SELECT 1 FROM events WHERE host=? AND day BETWEEN ? AND ? "
                "AND occurred>=? AND (price_digest IS NULL OR price_digest<>?) LIMIT 1",
                (*args, self._price_digest),
            ).fetchone()
            if stale:
                # sqlite supports TEXT aggregates; typeshed restricts finalize to int.
                db.create_aggregate("usage_totals", 5, cast(Any, Totals))
                totals_expr = "usage_totals(day,model,counters,meter,credits_milli)"
            else:
                totals_expr = _SQL_TOTALS
            # Attribution is the expensive part of a report and three statements below read it,
            # so it runs once into a temporary table; the rollback or the DROP at the end of
            # this transaction removes it from the shared connection.
            db.execute("DROP TABLE IF EXISTS temp.usage_attributed")
            db.execute(
                "CREATE TEMP TABLE usage_attributed AS "
                + self._attributed_sql(inferred=inferred)
                + "SELECT * FROM attributed",
                args,
            )
            cte = "WITH attributed AS (SELECT * FROM temp.usage_attributed) "
            # Current generation per workspace, from the same tables attribution reads; a
            # bare space id filters to it, and rows label older generations `w2~g0`.
            current_gen = {
                workspace: gen
                for workspace, gen in db.execute(
                    "SELECT workspace,MAX(generation) FROM (SELECT workspace,generation "
                    "FROM mappings WHERE host=? UNION ALL SELECT workspace,generation "
                    "FROM pane_cwd WHERE host=?) GROUP BY workspace",
                    (query.host_id, query.host_id),
                )
            }
            if space_ws is not None and space_gen is None:
                space_gen = current_gen.get(space_ws, 0)
            # Hourly mode groups by the host-local wall-clock hour of each event; daily mode keeps
            # NULL, so the grouping and the keyset are the same as before for every other query.
            hour = "usage_hour(occurred)" if query.hour else "NULL"
            sql = (
                cte
                + f""", current_gen AS (SELECT workspace,MAX(generation) AS gen FROM (
                    SELECT workspace,generation FROM mappings WHERE host=?
                    UNION ALL SELECT workspace,generation FROM pane_cwd WHERE host=?)
                    GROUP BY workspace),
                eligible AS (SELECT * FROM attributed WHERE workspace IS NOT NULL
                    AND verified=1 AND (? IS NULL OR (workspace=? AND generation=?))),
                spread AS (SELECT day,'workspace' AS scope,workspace,generation,link,kind,model,
                    meter,json_array(source,session) AS identity,counters,credits_milli,
                    cost_micro,price_pin,tt,CASE WHEN meter='tokens' AND cost_micro IS NULL AND tt>0
                      THEN 1 ELSE 0 END AS unp,
                    {hour} AS hour FROM eligible
                  UNION ALL SELECT day,'overall',NULL,NULL,link,kind,model,meter,
                    json_array(source,session),counters,credits_milli,cost_micro,price_pin,
                    tt,CASE WHEN meter='tokens' AND cost_micro IS NULL AND tt>0
                      THEN 1 ELSE 0 END AS unp,
                    {hour} FROM eligible
                  UNION ALL SELECT day,'not_linked',NULL,NULL,link,kind,model,meter,
                    json_array(source,session),counters,credits_milli,cost_micro,price_pin,
                    tt,CASE WHEN meter='tokens' AND cost_micro IS NULL AND tt>0
                      THEN 1 ELSE 0 END AS unp,
                    {hour} FROM attributed
                    WHERE workspace IS NULL AND verified=1 AND ? IS NULL),
                grouped AS (SELECT day,scope,workspace,generation,kind,model,meter,hour,
                    COUNT(DISTINCT identity) AS sessions,
                    COUNT(DISTINCT CASE WHEN link='exact' THEN identity END) AS linked,
                    COUNT(DISTINCT CASE WHEN link='inferred' THEN identity END) AS inferred_n,
                    {totals_expr} AS totals
                  FROM spread GROUP BY day,scope,workspace,generation,kind,model,meter,hour)
                SELECT day,scope,wire,kind,model,meter,hour,sessions,linked,inferred_n,totals
                  FROM (
                  SELECT day,scope,
                    CASE WHEN workspace IS NULL THEN NULL
                      WHEN generation=COALESCE((SELECT gen FROM current_gen
                        WHERE current_gen.workspace=grouped.workspace),0) THEN workspace
                      ELSE workspace||'~g'||generation END AS wire,
                    kind,model,meter,hour,sessions,linked,inferred_n,totals FROM grouped)
                  WHERE (day,scope,COALESCE(wire,''),kind,COALESCE(model,''),meter,
                    COALESCE(hour,-1))>(?,?,?,?,?,?,?)
                ORDER BY day,scope,COALESCE(wire,''),kind,COALESCE(model,''),meter,
                    COALESCE(hour,-1) LIMIT ?"""
            )
            # The first CTE already closes with ')'; additional CTEs start with a comma.
            found = db.execute(
                sql,
                (
                    query.host_id,
                    query.host_id,
                    space_ws,
                    space_ws,
                    space_gen,
                    space_ws,
                    *after[:6],
                    hour_after,
                    _row_limit + 1,
                ),
            ).fetchall()
            rows = []
            for found_row in found[:_row_limit]:
                day, scope, wire, kind, model, meter, row_hour = found_row[:7]
                sessions, linked, inferred_n, totals = found_row[7:]
                (
                    counters,
                    cost,
                    pin,
                    priced_cost,
                    priced_tokens,
                    unpriced_tokens,
                    credits,
                ) = json.loads(totals)
                rows.append(
                    UsageRow(
                        day=day,
                        workspace_id=wire,
                        kind=kind,
                        model=model,
                        counters=Counters(**counters),
                        sessions=sessions,
                        cost_micro_usd=cost if meter == "tokens" else None,
                        scope=RowScope(scope),
                        price_snapshot=pin,
                        priced_cost_micro_usd=priced_cost,
                        priced_tokens=priced_tokens,
                        unpriced_tokens=unpriced_tokens,
                        hour=row_hour,
                        linked_sessions=linked,
                        inferred_sessions=inferred_n,
                        meter=meter,
                        credits_milli=credits,
                    )
                )
            next_cursor = None
            if len(found) > _row_limit:
                tail = rows[-1]
                position = [
                    signature,
                    tail.day,
                    tail.scope,
                    tail.workspace_id or "",
                    tail.kind,
                    tail.model or "",
                    tail.meter,
                ]
                if query.hour:
                    position.append(str(tail.hour))
                next_cursor = base64.urlsafe_b64encode(_json(position).encode()).decode()
            coverage: dict[str, KindCoverage] = {}
            for kind, value, observed in db.execute(
                "SELECT kind,value,observed FROM coverage WHERE host=? AND retired=0 "
                "AND source NOT GLOB '*/*/limits:*'",
                (query.host_id,),
            ):
                previous = coverage.get(kind, KindCoverage(kind, Coverage.COMPLETE))
                worst = max(previous.coverage, Coverage(value), key=_COVERAGE.__getitem__)
                times = [t for t in (previous.observed_ms, observed) if t is not None]
                coverage[kind] = KindCoverage(kind, worst, max(times) if times else None)
            lower = max(
                args[3], int(datetime.combine(first, day_time(), self._zone).timestamp() * 1000)
            )
            upper = int(
                datetime.combine(last + timedelta(days=1), day_time(), self._zone).timestamp()
                * 1000
            )
            for (kind,) in db.execute(
                "SELECT DISTINCT kind FROM mapping_gaps WHERE host=? AND first<? "
                "AND (last IS NULL OR last>=?)",
                (query.host_id, upper, lower),
            ):
                previous = coverage.get(kind, KindCoverage(kind, Coverage.COMPLETE))
                coverage[kind] = replace(
                    previous,
                    coverage=max(previous.coverage, Coverage.UNVERIFIED, key=_COVERAGE.__getitem__),
                )
            for kind, unmapped, linked, inferred_n, unverified in db.execute(
                cte + "SELECT kind,COUNT(DISTINCT CASE WHEN workspace IS NULL AND verified=1 "
                "THEN json_array(source,session) END),"
                "COUNT(DISTINCT CASE WHEN link='exact' AND verified=1 "
                "THEN json_array(source,session) END),"
                "COUNT(DISTINCT CASE WHEN link='inferred' AND verified=1 "
                "THEN json_array(source,session) END),"
                "MAX(verified=0) FROM attributed GROUP BY kind",
            ):
                previous = coverage.get(kind, KindCoverage(kind, Coverage.COMPLETE))
                worst = max(
                    previous.coverage,
                    Coverage.UNVERIFIED if unverified else Coverage.COMPLETE,
                    key=_COVERAGE.__getitem__,
                )
                coverage[kind] = KindCoverage(
                    kind, worst, previous.observed_ms, unmapped, linked, inferred_n
                )
            limits = self._limits(db, query.host_id, now_ms)
            # Recency is read from the same attributed rows but without the space filter, so the
            # space the caller selected does not hide another space's last day (one row per space).
            pairs = [
                (workspace, generation, day)
                for workspace, generation, day in db.execute(
                    cte + "SELECT workspace,generation,MAX(day) FROM attributed "
                    "WHERE workspace IS NOT NULL AND verified=1 "
                    "GROUP BY workspace,generation ORDER BY workspace,generation",
                )
            ]
            db.execute("DROP TABLE temp.usage_attributed")
            recency = tuple(
                (space_wire_id(workspace, gen, current_gen.get(workspace, gen)), day)
                for workspace, gen, day in pairs
            )
            stored_labels = self._space_labels(
                db, query.host_id, {(workspace, gen) for workspace, gen, _ in pairs}
            )
            labels = tuple(
                (space_wire_id(workspace, gen, current_gen.get(workspace, gen)), label)
                for (workspace, gen), label in sorted(stored_labels.items())
            )
            return UsageReport(
                query.host_id,
                now_ms,
                self.timezone,
                tuple(rows),
                limits,
                tuple(coverage[k] for k in sorted(coverage)),
                prices.price_table_date,
                next_cursor,
                workspace_days=recency,
                workspace_labels=labels,
            )

    @staticmethod
    def _space_labels(
        db: sqlite3.Connection, host: str, pairs: set[tuple[str, int]]
    ) -> dict[tuple[str, int], str]:
        """Latest stored label per (workspace, generation): history shows the label it had."""
        if not pairs:
            return {}
        condition = " OR ".join("(workspace=? AND generation=?)" for _ in pairs)
        params: list[str | int] = [host]
        for workspace, gen in pairs:
            params.extend((workspace, gen))
        found: dict[tuple[str, int], str] = {}
        for workspace, gen, label in db.execute(
            "SELECT workspace,generation,label FROM mappings WHERE host=? "
            f"AND label IS NOT NULL AND ({condition}) ORDER BY first DESC",
            params,
        ):
            found.setdefault((workspace, gen), label)
        return found

    def data_generation(self) -> int:
        """Report-input generation (meta counter); 0 before any change."""
        with self._transaction(write=False) as db:
            row = db.execute("SELECT value FROM meta WHERE key='data_generation'").fetchone()
        return int(row[0]) if row and str(row[0]).isdigit() else 0

    def price_digest(self) -> str:
        return self._price_digest

    def backfill_prices(self, limit: int = 2000) -> tuple[int, int]:
        """Price stale rows in one bounded transaction; returns (done, remaining)."""
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise UsageUnavailable()
        with self._transaction() as db:
            rows = db.execute(
                "SELECT rowid,day,model,counters,meter FROM events WHERE "
                "price_digest IS NULL OR price_digest<>? ORDER BY rowid LIMIT ?",
                (self._price_digest, limit),
            ).fetchall()
            for rowid, day, model, counters, meter in rows:
                cost_micro, price_pin = (None, None)
                if meter == "tokens":
                    cost_micro, price_pin = self.prices.estimate(
                        day, model, Counters(**json.loads(counters))
                    )
                db.execute(
                    "UPDATE events SET cost_micro=?,price_pin=?,price_digest=? WHERE rowid=?",
                    (cost_micro, price_pin, self._price_digest, rowid),
                )
            remaining = db.execute(
                "SELECT COUNT(*) FROM events WHERE price_digest IS NULL OR price_digest<>?",
                (self._price_digest,),
            ).fetchone()[0]
            if not remaining:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES('price_digest',?)",
                    (self._price_digest,),
                )
        return len(rows), int(remaining)

    def unmapped_sessions(
        self, host_id: str, from_day: str, to_day: str, now_ms: int, *, inferred: bool = True
    ) -> dict[str, int]:
        """Coverage-only query: no attribution ranking, token aggregation or price lookup.
        Session grain: any mapping row counts as rank-1 evidence; without one, a unique
        digest candidate overlapping the session's own event range counts as inferred."""
        try:
            first, last = date.fromisoformat(from_day), date.fromisoformat(to_day)
            _key(host_id)
            if (
                first.isoformat() != from_day
                or last.isoformat() != to_day
                or not 0 <= (last - first).days < MAX_QUERY_DAYS
            ):
                raise ValueError
        except (ValueError, TypeError, UsageUnavailable):
            raise UsageUnavailable("invalid_query") from None
        if inferred:
            inf = (
                "AND NOT EXISTS(SELECT 1 FROM session_cwd c JOIN pane_cwd p "
                "ON p.source=c.source AND p.digest=c.digest "
                "WHERE c.source=e.source AND c.session=e.session "
                "AND p.first<=(SELECT MAX(x.occurred) FROM events x "
                "WHERE x.source=e.source AND x.session=e.session) "
                "AND p.last>=(SELECT MIN(x.occurred) FROM events x "
                "WHERE x.source=e.source AND x.session=e.session) "
                "GROUP BY c.source,c.session "
                "HAVING COUNT(DISTINCT p.workspace || CHAR(31) || p.generation)=1)"
            )
        else:
            inf = ""
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT e.kind,COUNT(DISTINCT json_array(e.source,e.session)) FROM events e "
                "WHERE e.host=? AND e.day BETWEEN ? AND ? AND e.occurred>=? AND e.verified=1 "
                "AND (EXISTS(SELECT 1 FROM mapping_gaps g WHERE g.source=e.source "
                "AND g.first<=e.occurred AND (g.last IS NULL OR g.last>=e.occurred)) "
                "OR (NOT EXISTS(SELECT 1 FROM mappings m WHERE m.source=e.source "
                "AND m.session=e.session AND m.host=e.host AND m.kind=e.kind) " + inf + ")) "
                "GROUP BY e.kind",
                (host_id, from_day, to_day, self._horizon(now_ms, db)),
            ).fetchall()
            return dict(rows)

    @staticmethod
    def _limits(db: sqlite3.Connection, host: str, now: int) -> tuple[LimitSnapshot, ...]:
        grouped: dict[
            tuple[str, str, str],
            list[tuple[int, LimitWindow | None, LimitStatus, str, str | None, str | None]],
        ] = {}
        for kind, quota, account, observed, body, status, source, error, plan_label in db.execute(
            "SELECT kind,quota,account,observed,body,status,source,error,plan_label FROM limits "
            "WHERE host=? AND retired=0 ORDER BY kind,quota,account,window",
            (host,),
        ):
            current = LimitStatus(status)
            window = LimitWindow(**json.loads(body)) if body else None
            if window is not None:
                # Snapshot status is aggregate evidence, not a status to copy
                # onto every window (a fresh weekly window can coexist with expiry).
                current = LimitStatus(window.status)
                if window.resets_at_ms is not None and now >= window.resets_at_ms:
                    current = LimitStatus.EXPIRED
                elif now - observed > LIMIT_STALE_MS:
                    current = max(current, LimitStatus.STALE, key=_LIMIT.__getitem__)
                window = replace(window, status=current)
            else:
                current = LimitStatus.NOT_REPORTED
            grouped.setdefault((kind, quota, account), []).append(
                (observed, window, current, source, error, plan_label)
            )
        # Storage keeps one row per window, each with its own observation time, so a window that a
        # newer reading no longer reports would survive beside it. Only the quota's latest
        # observation set is returned: its windows, or none when that reading had no windows.
        # Several accounts can be stored for one provider source. Only the active account is
        # current: the account of the source's newest reading. An unknown identity ("") is its own
        # account, so a reading with no identity never leaves an older account's percentage on
        # screen, and a switched account hides the previous one's rows (they stay stored).
        latest_by_source: dict[tuple[str, str], int] = {}
        for (kind, _quota, _account), rows in grouped.items():
            for row in rows:
                if row[3]:  # a provider-plane reading; legacy ledger rows (source "") are separate
                    key = (kind, row[3])
                    latest_by_source[key] = max(latest_by_source.get(key, -1), row[0])
        active: dict[tuple[str, str], set[str]] = {}
        for (kind, _quota, account), rows in grouped.items():
            for row in rows:
                if row[3] and row[0] == latest_by_source[(kind, row[3])]:
                    active.setdefault((kind, row[3]), set()).add(account)
        snapshots = []
        for (kind, quota, account), rows in grouped.items():
            sources = {row[3] for row in rows if row[3]}
            if any(account not in active.get((kind, source), {account}) for source in sources):
                continue  # historical: another account is the active one for this source
            latest = max(row[0] for row in rows)
            newest = [row for row in rows if row[0] == latest]
            snapshots.append(
                LimitSnapshot(
                    kind,
                    quota,
                    latest,
                    tuple(row[1] for row in newest if row[1] is not None),
                    max((row[2] for row in newest), key=_LIMIT.__getitem__),
                    source=newest[0][3],
                    account_ref=account or None,
                    error=newest[0][4],
                    plan_label=newest[0][5],
                )
            )
        return tuple(snapshots)

    def retire_missing_sources(
        self,
        host_id: str,
        active_sources: Collection[str],
        active_limits: Collection[str],
    ) -> int:
        """Mark this host's coverage/cursor/cursor_files rows retired unless their
        source is still configured, and its limits rows retired unless their
        (kind, format) pair is still configured; un-mark rows that are
        configured again. Events and all other history are untouched, so a
        re-added source resumes from its kept cursor. Returns the number of
        rows flipped. Other hosts' rows are never touched."""
        _key(host_id)
        sources = tuple(dict.fromkeys(active_sources))
        limits = tuple(dict.fromkeys(active_limits))
        for key in (*sources, *limits):
            parts = key.split("/") if isinstance(key, str) else []
            if (
                len(parts) != 3
                or parts[0] != host_id
                or any(
                    not 0 < len(part) <= MAX_KEY_CHARS or any(ord(c) < 32 for c in part)
                    for part in parts
                )
            ):
                raise UsageUnavailable()
        pairs = sorted({(key.split("/")[1], key.split("/")[2]) for key in limits})
        prefix = f"{host_id}/"
        try:
            with self._transaction() as db:
                flipped = 0
                if sources:
                    marks = ",".join("?" for _ in sources)
                    flipped += db.execute(
                        "UPDATE coverage SET retired=1 WHERE host=? AND retired=0 "
                        f"AND source NOT IN ({marks})",
                        (host_id, *sources),
                    ).rowcount
                    flipped += db.execute(
                        "UPDATE coverage SET retired=0 WHERE host=? AND retired=1 "
                        f"AND source IN ({marks})",
                        (host_id, *sources),
                    ).rowcount
                    for table in ("cursors", "cursor_files"):
                        flipped += db.execute(
                            f"UPDATE {table} SET retired=1 WHERE retired=0 "
                            f"AND substr(source,1,?)=? AND source NOT IN ({marks})",
                            (len(prefix), prefix, *sources),
                        ).rowcount
                        flipped += db.execute(
                            f"UPDATE {table} SET retired=0 WHERE retired=1 "
                            f"AND substr(source,1,?)=? AND source IN ({marks})",
                            (len(prefix), prefix, *sources),
                        ).rowcount
                else:
                    flipped += db.execute(
                        "UPDATE coverage SET retired=1 WHERE host=? AND retired=0",
                        (host_id,),
                    ).rowcount
                    for table in ("cursors", "cursor_files"):
                        flipped += db.execute(
                            f"UPDATE {table} SET retired=1 WHERE retired=0 "
                            "AND substr(source,1,?)=?",
                            (len(prefix), prefix),
                        ).rowcount
                if pairs:
                    marks = ",".join("(?,?)" for _ in pairs)
                    flat = [part for pair in pairs for part in pair]
                    flipped += db.execute(
                        "UPDATE limits SET retired=1 WHERE host=? AND retired=0 "
                        f"AND (kind,source) NOT IN ({marks})",
                        (host_id, *flat),
                    ).rowcount
                    flipped += db.execute(
                        "UPDATE limits SET retired=0 WHERE host=? AND retired=1 "
                        f"AND (kind,source) IN ({marks})",
                        (host_id, *flat),
                    ).rowcount
                else:
                    flipped += db.execute(
                        "UPDATE limits SET retired=1 WHERE host=? AND retired=0",
                        (host_id,),
                    ).rowcount
                return flipped
        except sqlite3.Error:
            raise UsageUnavailable() from None

    def prune(self, now_ms: int) -> int:
        with self._transaction() as db:
            cutoff = self._horizon(now_ms, db)
            db.execute("INSERT OR REPLACE INTO meta VALUES('cutoff',?)", (str(cutoff),))
            remaining = 5000
            # Keep one old attribution anchor only while retained events need it.
            targets = (
                ("events", "occurred<?"),
                (
                    "mappings",
                    "last<? AND NOT (EXISTS(SELECT 1 FROM events e "
                    "WHERE e.source=mappings.source AND e.session=mappings.session) "
                    "AND interval=(SELECT m.interval FROM mappings m "
                    "WHERE m.source=mappings.source AND m.session=mappings.session AND m.last<? "
                    "ORDER BY m.rank,m.last DESC,m.interval LIMIT 1))",
                ),
                ("limits", "observed<?"),
                ("mapping_gaps", "last<?"),
                (
                    "session_cwd",
                    "updated<? AND NOT EXISTS(SELECT 1 FROM events e "
                    "WHERE e.source=session_cwd.source AND e.session=session_cwd.session)",
                ),
                ("pane_cwd", "last<?"),
                ("receipts", "updated<?"),
                ("cursor_files", "source IN (SELECT source FROM cursors WHERE updated<?)"),
                (
                    "cursors",
                    "updated<? AND NOT EXISTS(SELECT 1 FROM cursor_files f "
                    "WHERE f.source=cursors.source)",
                ),
                ("coverage", "updated<?"),
            )
            for table, condition in targets:
                params = (cutoff, cutoff, remaining) if table == "mappings" else (cutoff, remaining)
                cursor = db.execute(
                    f"DELETE FROM {table} WHERE rowid IN "
                    f"(SELECT rowid FROM {table} WHERE {condition} LIMIT ?)",
                    params,
                )
                remaining -= cursor.rowcount
                if not remaining:
                    break
            deleted = 5000 - remaining
            if deleted:
                _bump_generation(db)
            return deleted
