"""Cumulative per-session token usage from local SQLite state databases.

One source root is a tool home directory holding `state.db` plus
`profiles/*/state.db`, all sharing one schema. Each database carries
`session_model_usage`: cumulative token counters per
(session, model, provider, url, mode, task) with first/last-seen REAL
seconds, and `sessions`: one row per session with cumulative totals, the
working folder (`cwd`), the parent session, and activity times.

Events are `Basis.CUMULATIVE_DELTA`: the reader keeps the previous totals
per row in its cursor and emits the difference. A re-read emits nothing new
(the store replaces a retried key); a reset (totals decrease) emits
nothing, records a gap, and rebases. First sight of a row emits its full
current totals attributed to its last-seen day, which is exact for new rows
and approximate for rows that accumulated over several days before the
first read. `*_cost_usd` columns are ignored (the engine prices itself);
message tables are never opened.

Reads are live read-only snapshots (`mode=ro`, one transaction per pass
phase): no sidecar files are created and writers are never blocked. Copying
is infeasible (databases reach gigabytes); `immutable=1` would silently
ignore the write-ahead log.

Every SQL read is bounded: rows stream in fixed chunks, a progress handler
aborts statements at the budget deadline, retrieved text is capped (an
over-long field skips and counts its row, never truncates identity), and
row scans resume through keyset cursors. A cut scan reports `more` with
PARTIAL coverage, never partial-as-complete.

Session identity is the bare session id so Herdr linking works. An id seen
in more than one database keeps its bare key in exactly one database (the
side that already emitted for it, else the first in sorted order; sticky in
the cursor) and uses a database-namespaced key everywhere else, so both
sides count without ever merging. Event keys always carry the database tag,
so identical rows in two databases can never replace each other. The sticky
collision count is recorded in the cursor state (`col-n`); namespaced rows
cannot exact-link, but their working-folder digest may still infer.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import (
    MAX_DIR_ENTRIES,
    Basis,
    Batch,
    Budget,
    Counters,
    Coverage,
    Cursor,
    FileCursor,
)
from relaydeck.usage.readers._files import (
    SCHEMA_KEY,
    EventCollector,
    context_of,
    key_text,
    make_event,
    session_text,
)

FORMAT_ID = "sqlite-session-usage-v1"
SCHEMA = "sqlite-usage-1"
USAGE_TABLE = "session_model_usage"
SESSIONS_TABLE = "sessions"
MAIN_DB = "state.db"

_USAGE_PK = (
    "session_id",
    "model",
    "billing_provider",
    "billing_base_url",
    "billing_mode",
    "task",
)
_USAGE_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
_USAGE_REQUIRED = _USAGE_PK + _USAGE_COUNTERS + ("api_call_count", "first_seen", "last_seen")
_SESSIONS_REQUIRED = (
    "id",
    "model",
    "cwd",
    "parent_session_id",
    "started_at",
    "last_activity_at",
    "ended_at",
    "api_call_count",
) + _USAGE_COUNTERS

_SHARD_BYTES = 60 * 1024
_ROW_BYTES_EST = 256
_MAX_IDS_PER_DB = 100_000
_MAX_STICKY = 1024
_BUSY_TIMEOUT_S = 1.0
_DB_PREFIX = "db:"
_FETCH_ROWS = 200
# Retrieval cap for one text field: far above every legitimate value (paths
# top out at 4096 chars), low enough that one chunk stays small. A longer
# field skips and counts its row; identity is never truncated.
_MAX_TEXT_CHARS = 8192
_PROGRESS_OPS = 1000


def _ms(value: object) -> int | None:
    """REAL seconds since the epoch to integer milliseconds, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        ms = int(value * 1000)
    except (OverflowError, ValueError):
        return None
    return ms if 0 < ms < 2**62 else None


def _count(value: object) -> int | None:
    """A counter cell: None stays None (unknown), integral values pass, else None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _file_identity(path: Path) -> list[int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _fingerprint(path: Path) -> list[Any] | None:
    """Change detector: the db file plus its write-ahead log (WAL-only writes must
    not look unchanged). None when the db itself cannot be statted."""
    main = _file_identity(path)
    if main is None:
        return None
    return [main[2], main[3], _file_identity(Path(str(path) + "-wal"))]


def _text(column: str) -> str:
    """Bounded retrieval of a text column. Text longer than the cap arrives
    truncated one past it (the caller skips and counts it); every other type
    passes through untouched, so the existing validation sees exactly what it
    saw before. Column names are module constants, never input."""
    return (
        f"CASE WHEN typeof({column}) = 'text'"
        f" THEN substr({column}, 1, {_MAX_TEXT_CHARS + 1})"
        f" ELSE {column} END"
    )


class _BudgetCut(Exception):
    """Internal control flow: a scan hit the byte or deadline budget."""


def _head_digest(path: Path) -> str | None:
    """Hex SHA-256 of the file's first 4096 bytes (the store requires 64 hex)."""
    try:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read(4096)).hexdigest()
    except OSError:
        return None


class _Reader:
    def __init__(self, cursor: Cursor, budget: Budget) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.events = EventCollector(budget.max_events)
        self.files: dict[str, FileCursor] = dict(cursor.files)
        self.totals_cache: dict[str, dict[str, list[Any]]] = {}
        self.sticky: dict[str, str] = {}
        state = cursor.state.get("col")
        if isinstance(state, str) and state:
            try:
                parsed = json.loads(state)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                for sid, holder in list(parsed.items())[:_MAX_STICKY]:
                    if isinstance(sid, str) and isinstance(holder, str):
                        self.sticky[sid] = holder
        self.skipped = 0
        self.partial = False
        self.more = False
        self.retry_blocked = False
        self.bytes_est = 0
        self.usable_any = False

    def expired(self) -> bool:
        return (time.monotonic() - self.started) * 1000 >= self.budget.deadline_ms

    def _connect(self, path: Path) -> sqlite3.Connection | None:
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
        except sqlite3.Error:
            return None
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA temp_store=MEMORY")
            db.execute("BEGIN")
            deadline = self.started + self.budget.deadline_ms / 1000
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, _PROGRESS_OPS)
        except sqlite3.Error:
            db.close()
            return None
        return db

    @staticmethod
    def _tables(db: sqlite3.Connection) -> set[str]:
        try:
            rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        except sqlite3.Error:
            return set()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str] | None:
        # Table names are module constants, never input.
        try:
            rows = db.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return None
        return {str(row[1]) for row in rows}

    def _entry_context(self, rel: str) -> dict[str, object]:
        entry = self.files.get(_DB_PREFIX + rel)
        if entry is None:
            return {}
        parsed = context_of(entry)
        return parsed if isinstance(parsed, dict) else {}

    def _totals(self, rel: str) -> dict[str, list[Any]]:
        """Previous totals merged from the db's shard entries: row id to
        [seq, 5 counters, session id]. The session id keeps holder resolution
        stable when a collision forms after one side already emitted."""
        if rel in self.totals_cache:
            return self.totals_cache[rel]
        totals: dict[str, list[Any]] = {}
        for key, shard in self.files.items():
            if not key.startswith(f"{_DB_PREFIX}{rel}#"):
                continue
            packed = context_of(shard)
            if not isinstance(packed, dict):
                continue
            for pk, values in packed.items():
                if (
                    isinstance(pk, str)
                    and isinstance(values, list)
                    and len(values) == 7
                    and all(type(v) is int for v in values[:6])
                    and isinstance(values[6], str)
                ):
                    totals[pk] = values
        self.totals_cache[rel] = totals
        return totals

    def _store_db(
        self,
        rel: str,
        path: Path,
        fingerprint: list[Any] | None,
        schema: str,
        resume: list[Any] | None,
        totals: dict[str, list[Any]],
    ) -> None:
        info = _file_identity(path)
        head = _head_digest(path)
        if info is None or head is None:
            return
        dev, ino, size, mtime = info
        context: dict[str, object] = {"fp": fingerprint, "schema": schema}
        if resume is not None:
            context["resume"] = resume
        self.files[_DB_PREFIX + rel] = FileCursor(
            dev, ino, 0, size, mtime, head, json.dumps(context, sort_keys=True)
        )
        shards: dict[str, dict[str, list[int]]] = {"0": {}}
        current = "0"
        for pk in sorted(totals):
            shards[current][pk] = totals[pk]
            if len(json.dumps(shards[current], sort_keys=True)) > _SHARD_BYTES:
                del shards[current][pk]
                current = str(len(shards))
                shards[current] = {pk: totals[pk]}
        for key in [k for k in self.files if k.startswith(f"{_DB_PREFIX}{rel}#")]:
            del self.files[key]
        for shard, packed in shards.items():
            self.files[f"{_DB_PREFIX}{rel}#{shard}"] = FileCursor(
                dev, ino, 0, size, mtime, head, json.dumps(packed, sort_keys=True)
            )

    def _session_key(self, rel: str, session_id: str, holders: dict[str, str]) -> str:
        holder = holders.get(session_id)
        if holder is None or holder == rel:
            kept = session_text(session_id)
            return kept if kept is not None else key_text("session", session_id)
        namespaced = f"{key_text('db', rel)[:16]}/{session_id}"
        return session_text(namespaced) or key_text("session", namespaced)

    def _emit(
        self,
        kind: str,
        pk: str,
        session_key: str,
        session_id: str,
        occurred_ms: int,
        model: object,
        current: list[int],
        totals: dict[str, list[Any]],
        parent: str | None,
        digest: str | None,
        dbtag: str,
    ) -> bool:
        """Delta current minus previous under a fresh sequence key. False when the
        event budget is exhausted (the caller stops without committing this row).
        The key carries the database tag: identical rows in two databases must
        never share a key (the store would replace one with the other)."""
        previous = totals.get(pk)
        seq = -1 if previous is None else previous[0]
        if previous is not None and current == previous[1:6]:
            return True
        if previous is not None and any(c < p for c, p in zip(current, previous[1:6], strict=True)):
            totals[pk] = [seq + 1, *current, session_id]
            self.skipped += 1
            self.partial = True
            return True
        base = previous[1:6] if previous is not None else [0, 0, 0, 0, 0]
        deltas = [c - p for c, p in zip(current, base, strict=True)]
        if previous is None and all(d == 0 for d in deltas):
            return True  # Absent baseline already means all-zero; store nothing.
        try:
            counters = Counters(
                input_uncached=deltas[0],
                output=deltas[1],
                cache_read=deltas[2],
                cache_write_unknown=deltas[3],
                reasoning=deltas[4],
            )
        except ValueError:
            totals[pk] = [seq + 1, *current, session_id]
            self.skipped += 1
            self.partial = True
            return True
        event = make_event(
            session=session_key,
            event_key=f"{kind}:{dbtag}:{pk}:{seq + 1}",
            occurred_ms=occurred_ms,
            model=model,
            counters=counters,
            basis=Basis.CUMULATIVE_DELTA,
            parent=parent,
            digest=digest,
        )
        if not self.events.offer(event):
            return False
        totals[pk] = [seq + 1, *current, session_id]
        return True

    def _digest_for(self, folder: object) -> str | None:
        if self.budget.digest is None or not isinstance(folder, str) or not folder:
            return None
        try:
            digest = self.budget.digest(folder)
        except Exception:
            return None
        return digest if isinstance(digest, str) and digest else None

    def _usage_row(
        self,
        rel: str,
        row: tuple[Any, ...],
        digests: dict[str, str | None],
        parents: dict[str, str | None],
        totals: dict[str, list[Any]],
        holders: dict[str, str],
        dbtag: str,
    ) -> bool:
        (session_id, model, provider, base_url, mode, task, calls, *rest) = row
        if not isinstance(session_id, str) or not session_id:
            self.skipped += 1
            self.partial = True
            return True
        for part in (model, provider, base_url, mode, task):
            if not isinstance(part, str):
                self.skipped += 1
                self.partial = True
                return True
        if any(
            len(part) > _MAX_TEXT_CHARS
            for part in (session_id, model, provider, base_url, mode, task)
        ):
            self.skipped += 1  # over-long identity: skip and count, never truncate
            self.partial = True
            return True
        raw = rest[:5]
        counters = [_count(v) for v in raw]
        if any(v is not None and c is None for v, c in zip(raw, counters, strict=True)):
            self.skipped += 1
            self.partial = True
            return True
        first_ms = _ms(rest[5])
        last_ms = _ms(rest[6])
        if last_ms is None or (first_ms is not None and last_ms < first_ms):
            self.skipped += 1
            self.partial = True
            return True
        if all(v is None for v in counters):
            if (_count(calls) or 0) > 0:
                self.skipped += 1
                self.partial = True
            return True
        current = [v or 0 for v in counters]
        pk = key_text(session_id, model, provider, base_url, mode, task)
        return self._emit(
            "smu1",
            pk,
            self._session_key(rel, session_id, holders),
            session_id,
            last_ms,
            model,
            current,
            totals,
            parents.get(session_id),
            digests.get(session_id),
            dbtag,
        )

    def _fallback_row(
        self,
        rel: str,
        row: tuple[Any, ...],
        totals: dict[str, list[Any]],
        holders: dict[str, str],
        dbtag: str,
    ) -> bool:
        (sid, model, folder, parent, started, last_active, ended, calls, *rest) = row
        if not isinstance(sid, str) or not sid:
            self.skipped += 1
            self.partial = True
            return True
        for part in (sid, model, folder, parent):
            if isinstance(part, str) and len(part) > _MAX_TEXT_CHARS:
                self.skipped += 1  # over-long field: skip and count, never truncate
                self.partial = True
                return True
        raw = rest[:5]
        counters = [_count(v) for v in raw]
        if any(v is not None and c is None for v, c in zip(raw, counters, strict=True)):
            self.skipped += 1
            self.partial = True
            return True
        if all(v is None for v in counters):
            if (_count(calls) or 0) > 0:
                self.skipped += 1
                self.partial = True
            return True
        occurred_ms: int | None = None
        for candidate in (last_active, ended, started):
            occurred_ms = _ms(candidate)
            if occurred_ms is not None:
                break
        if occurred_ms is None:
            self.skipped += 1
            self.partial = True
            return True
        current = [v or 0 for v in counters]
        pk = key_text("sessions", sid)
        parent_key = session_text(parent) if isinstance(parent, str) and parent else None
        return self._emit(
            "ses1",
            pk,
            self._session_key(rel, sid, holders),
            sid,
            occurred_ms,
            model,
            current,
            totals,
            parent_key,
            self._digest_for(folder),
            dbtag,
        )

    def _session_maps(
        self, db: sqlite3.Connection
    ) -> tuple[dict[str, str | None], dict[str, str | None]] | None:
        """Session id to (digest, parent): one ordered scan, all-or-nothing per pass."""
        digests: dict[str, str | None] = {}
        parents: dict[str, str | None] = {}
        try:
            cursor = db.execute(
                f"SELECT {_text('id')}, {_text('cwd')}, {_text('parent_session_id')}"  # noqa: S608 - constant table and column names
                " FROM sessions ORDER BY id"
            )
            while True:
                chunk = cursor.fetchmany(_FETCH_ROWS)
                if not chunk:
                    return digests, parents
                for sid, folder, parent in chunk:
                    self.bytes_est += _ROW_BYTES_EST
                    if self.bytes_est > self.budget.max_bytes or self.expired():
                        return None
                    if not isinstance(sid, str) or not sid:
                        continue
                    if any(
                        isinstance(part, str) and len(part) > _MAX_TEXT_CHARS
                        for part in (sid, folder, parent)
                    ):
                        self.skipped += 1
                        self.partial = True
                        continue
                    digests[sid] = self._digest_for(folder)
                    parents[sid] = (
                        session_text(parent) if isinstance(parent, str) and parent else None
                    )
        except sqlite3.Error:
            return None

    def _read_db(self, rel: str, path: Path, holders: dict[str, str]) -> None:
        context = self._entry_context(rel)
        resume = context.get("resume")
        resume_table: str | None = None
        resume_key: object = None
        if isinstance(resume, list) and len(resume) == 2 and resume[0] in ("u", "s"):
            resume_table, resume_key = resume
        totals = self._totals(rel)
        dbtag = key_text("db", rel)[:16]
        db = self._connect(path)
        if db is None:
            self.skipped += 1
            self.partial = True
            self.retry_blocked = True
            return
        # Fingerprint at open: a write landing mid-read must cause a re-read next
        # pass, never a skip (the snapshot predates it).
        open_fp = _fingerprint(path)
        try:
            tables = self._tables(db)
            usage_cols = self._columns(db, USAGE_TABLE) if USAGE_TABLE in tables else None
            sessions_cols = self._columns(db, SESSIONS_TABLE) if SESSIONS_TABLE in tables else None
            if usage_cols is not None and not set(_USAGE_REQUIRED) <= usage_cols:
                usage_cols = None
            if sessions_cols is not None and not set(_SESSIONS_REQUIRED) <= sessions_cols:
                sessions_cols = None
            if usage_cols is None and sessions_cols is None:
                self.skipped += 1
                self.partial = True
                self.usable_any = True  # Read fine; nothing usable inside.
                return
            schema = json.dumps(
                {
                    "u": sorted(usage_cols) if usage_cols else None,
                    "s": sorted(sessions_cols) if sessions_cols else None,
                },
                sort_keys=True,
            )
            maps = self._session_maps(db) if sessions_cols is not None else ({}, {})
            if maps is None:
                self.more = True
                self.partial = True
                return
            digests, parents = maps
            tail: list[Any] | None = None
            if usage_cols is not None and resume_table in (None, "u"):
                query = (
                    f"SELECT {_text('session_id')}, {_text('model')},"  # noqa: S608 - constant table and column names
                    f" {_text('billing_provider')}, {_text('billing_base_url')},"
                    f" {_text('billing_mode')}, {_text('task')},"
                    " api_call_count, input_tokens, output_tokens,"
                    " cache_read_tokens, cache_write_tokens, reasoning_tokens,"
                    " first_seen, last_seen FROM session_model_usage"
                )
                params: tuple[Any, ...] = ()
                if resume_table == "u" and isinstance(resume_key, list):
                    query += (
                        " WHERE (session_id, model, billing_provider, billing_base_url,"
                        " billing_mode, task) > (?,?,?,?,?,?)"
                    )
                    params = tuple(resume_key)
                query += (
                    " ORDER BY session_id, model, billing_provider, billing_base_url,"
                    " billing_mode, task"
                )
                last_key: list[Any] | None = None
                try:
                    cursor = db.execute(query, params)
                    while True:
                        chunk = cursor.fetchmany(_FETCH_ROWS)
                        if not chunk:
                            break
                        for row in chunk:
                            self.bytes_est += _ROW_BYTES_EST
                            if self.bytes_est > self.budget.max_bytes or self.expired():
                                self._store_db(
                                    rel, path, None, schema, self._resume(last_key, "u"), totals
                                )
                                self.more = True
                                self.partial = True
                                return
                            if not self._usage_row(
                                rel, row, digests, parents, totals, holders, dbtag
                            ):
                                self._store_db(
                                    rel, path, None, schema, self._resume(last_key, "u"), totals
                                )
                                self.more = True
                                self.partial = True
                                return
                            last_key = list(row[:6])
                except sqlite3.Error:
                    if self.expired():
                        self._store_db(rel, path, None, schema, self._resume(last_key, "u"), totals)
                        self.more = True
                        self.partial = True
                        return
                    self.skipped += 1
                    self.partial = True
                    self.retry_blocked = True
                    return
                tail = self._resume(last_key, "u")
            if usage_cols is None and sessions_cols is not None and resume_table in (None, "s"):
                start = resume_key if isinstance(resume_key, str) else None
                query = (
                    f"SELECT {_text('id')}, {_text('model')}, {_text('cwd')},"  # noqa: S608 - constant table and column names
                    f" {_text('parent_session_id')}, started_at,"
                    " last_activity_at, ended_at, api_call_count,"
                    " input_tokens, output_tokens, cache_read_tokens,"
                    " cache_write_tokens, reasoning_tokens FROM sessions"
                )
                fallback_params: tuple[Any, ...] = ()
                if start is not None:
                    query += " WHERE id > ?"
                    fallback_params = (start,)
                query += " ORDER BY id"
                last_sid: str | None = None
                try:
                    cursor = db.execute(query, fallback_params)
                    while True:
                        chunk = cursor.fetchmany(_FETCH_ROWS)
                        if not chunk:
                            break
                        for row in chunk:
                            self.bytes_est += _ROW_BYTES_EST
                            if self.bytes_est > self.budget.max_bytes or self.expired():
                                self._store_db(
                                    rel, path, None, schema, self._resume(last_sid, "s"), totals
                                )
                                self.more = True
                                self.partial = True
                                return
                            if not self._fallback_row(rel, row, totals, holders, dbtag):
                                self._store_db(
                                    rel, path, None, schema, self._resume(last_sid, "s"), totals
                                )
                                self.more = True
                                self.partial = True
                                return
                            last_sid = row[0] if isinstance(row[0], str) else last_sid
                except sqlite3.Error:
                    if self.expired():
                        self._store_db(rel, path, None, schema, self._resume(last_sid, "s"), totals)
                        self.more = True
                        self.partial = True
                        return
                    self.skipped += 1
                    self.partial = True
                    self.retry_blocked = True
                    return
                tail = self._resume(last_sid, "s")
            if usage_cols is not None and sessions_cols is not None:
                try:
                    mixed = db.execute(
                        "SELECT COUNT(*) FROM sessions s WHERE NOT EXISTS"
                        " (SELECT 1 FROM session_model_usage u WHERE u.session_id = s.id)"
                        " AND (COALESCE(s.api_call_count, 0) > 0"
                        " OR s.input_tokens IS NOT NULL OR s.output_tokens IS NOT NULL"
                        " OR s.cache_read_tokens IS NOT NULL"
                        " OR s.cache_write_tokens IS NOT NULL"
                        " OR s.reasoning_tokens IS NOT NULL)"
                    ).fetchone()
                except sqlite3.Error:
                    if self.expired():
                        # The rows are all emitted; only the gap-evidence count is
                        # missing. Resume past the last row so the next pass retries
                        # just it, never a partial-as-complete commit.
                        self._store_db(rel, path, None, schema, tail, totals)
                        self.more = True
                        self.partial = True
                        return
                    mixed = None
                if mixed is not None and mixed[0]:
                    self.skipped += mixed[0]
                    self.partial = True
            db.execute("COMMIT")
            self.usable_any = True
            self._store_db(rel, path, open_fp, schema, None, totals)
        finally:
            db.close()

    @staticmethod
    def _resume(last_key: list[Any] | str | None, table: str) -> list[Any] | None:
        if last_key is None:
            return None
        return [table, last_key]


class _NoDatabase(Exception):
    pass


def _discover(root: Path, reader: _Reader) -> list[tuple[str, Path]]:
    try:
        if not root.is_dir() or root.is_symlink():
            raise _NoDatabase
    except OSError:
        raise _NoDatabase from None
    found: list[tuple[str, Path]] = []
    main = root / MAIN_DB
    try:
        if main.is_symlink():
            reader.skipped += 1
            reader.partial = True
        elif main.is_file():
            found.append((MAIN_DB, main))
    except OSError:
        reader.skipped += 1
        reader.partial = True
        reader.retry_blocked = True
    profiles = root / "profiles"
    try:
        entries = sorted(p.name for p in profiles.iterdir()) if profiles.is_dir() else []
    except OSError:
        reader.skipped += 1
        reader.partial = True
        reader.retry_blocked = True
        entries = []
    for name in entries[:MAX_DIR_ENTRIES]:
        if name in (".", ".."):
            continue
        candidate = profiles / name / MAIN_DB
        try:
            if candidate.is_symlink():
                reader.skipped += 1
                reader.partial = True
            elif candidate.is_file():
                found.append((f"profiles/{name}/{MAIN_DB}", candidate))
        except OSError:
            reader.skipped += 1
            reader.partial = True
            reader.retry_blocked = True
    if len(entries) > MAX_DIR_ENTRIES:
        reader.more = True
        reader.partial = True
    found.sort()
    if not found:
        raise _NoDatabase
    cap = max(reader.budget.max_files, 1)
    if len(found) > cap:
        reader.more = True
        reader.partial = True
    return found[:cap]


def _scan_ids(
    dbs: list[tuple[str, Path]], reader: _Reader
) -> tuple[dict[str, str], set[str], bool]:
    """Session id to bare-key holder, db rels to skip (ids unknown this pass),
    and whether the scan itself was cut short (deferred work, not a stop).

    The holder is sticky: once an id collides, its bare holder never changes,
    so keys never flap between bare and namespaced across passes.
    """
    seen: dict[str, list[str]] = {}
    skip: set[str] = set()
    cut = False
    for index, (rel, path) in enumerate(dbs):
        if reader.expired():
            reader.partial = True
            skip.update(r for r, _ in dbs[index:])
            cut = True
            break
        db = reader._connect(path)
        if db is None:
            skip.add(rel)
            reader.skipped += 1
            reader.partial = True
            reader.retry_blocked = True
            continue
        try:
            # Table and column names are module constants, never input. Any
            # failure here skips the db for the pass: emitting without its ids
            # could miss a collision.
            names = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            ids: set[str] = set()
            capped = False
            for table, column in ((USAGE_TABLE, "session_id"), (SESSIONS_TABLE, "id")):
                if table not in names or capped:
                    continue
                cursor = db.execute(f"SELECT {_text(column)} FROM {table}")  # noqa: S608 - constant table and column names
                while not capped:
                    chunk = cursor.fetchmany(_FETCH_ROWS)
                    if not chunk:
                        break
                    for (value,) in chunk:
                        # No byte charge: this scan has no resume, so spending
                        # the byte budget here could wedge a pass forever. Time
                        # (deadline handler plus this check), row count (the id
                        # cap) and memory (fixed chunks) are all bounded.
                        if reader.expired():
                            raise _BudgetCut
                        if isinstance(value, str) and value:
                            if len(value) > _MAX_TEXT_CHARS:
                                reader.skipped += 1
                                reader.partial = True
                            else:
                                ids.add(value)
                        if len(ids) > _MAX_IDS_PER_DB:
                            capped = True
                            break
            if capped:
                reader.skipped += 1  # ids beyond the cap: collisions may be missed
                reader.partial = True
            db.execute("COMMIT")
        except _BudgetCut:
            db.close()
            reader.partial = True
            skip.update(r for r, _ in dbs[index:])
            cut = True
            break
        except sqlite3.Error:
            db.close()
            if reader.expired():
                reader.partial = True
                skip.update(r for r, _ in dbs[index:])
                cut = True
                break
            skip.add(rel)
            reader.skipped += 1
            reader.partial = True
            reader.retry_blocked = True
            continue
        db.close()
        for sid in ids:
            seen.setdefault(sid, []).append(rel)
    for rel, _ in dbs:
        reader._totals(rel)
    holders: dict[str, str] = {}
    for sid, rels in seen.items():
        if len(rels) < 2:
            continue
        holder = reader.sticky.get(sid)
        if holder is None:
            # A side that already emitted for this id keeps the bare key, so a
            # collision forming late never flaps settled keys; simultaneous
            # first sight falls back to sorted order.
            veterans = [
                rel
                for rel in rels
                if any(values[6] == sid for values in reader._totals(rel).values())
            ]
            holder = veterans[0] if len(veterans) == 1 else sorted(rels)[0]
        reader.sticky.pop(sid, None)
        reader.sticky[sid] = holder
        while len(reader.sticky) > _MAX_STICKY:
            reader.sticky.pop(next(iter(reader.sticky)))
        holders[sid] = holder
    return holders, skip, cut


def _resume_rel(cursor: Cursor, dbs: list[tuple[str, Path]]) -> str | None:
    for rel, _ in dbs:
        entry = cursor.files.get(_DB_PREFIX + rel)
        if entry is None:
            continue
        parsed = context_of(entry)
        if isinstance(parsed, dict) and isinstance(parsed.get("resume"), list):
            return rel
    return None


class SqliteSessionUsageReader:
    """Cumulative session usage; see the module docstring for the full contract."""

    format_id = FORMAT_ID
    tokens_reported = True
    lock_filename: str | None = None

    def lock_session_keys(self, rel: str) -> tuple[str | None, str | None]:
        return None, None

    def read(self, root: Path, cursor: Cursor, budget: Budget) -> Batch:
        if cursor.state.get(SCHEMA_KEY) != SCHEMA:
            cursor = Cursor({}, {}, cursor.revision)
        reader = _Reader(cursor, budget)
        try:
            dbs = _discover(root, reader)
        except _NoDatabase:
            return Batch((), cursor, Coverage.UNAVAILABLE, cursor.revision, retry_blocked=True)
        holders, skip, scan_cut = _scan_ids(dbs, reader)
        resume_rel = _resume_rel(cursor, dbs)
        resuming = resume_rel is not None
        deferred = scan_cut or bool(skip)
        for rel, path in dbs:
            if resuming:
                if rel != resume_rel:
                    continue
                resuming = False
            if rel in skip:
                reader.partial = True
                continue
            fingerprint = _fingerprint(path)
            entry = reader._entry_context(rel)
            if (
                fingerprint is not None
                and entry.get("resume") is None
                and entry.get("fp") == fingerprint
            ):
                reader.usable_any = True
                continue
            if reader.expired():
                reader.more = True
                reader.partial = True
                break
            reader._read_db(rel, path, holders)
            if reader.more:
                break
        reader.more = reader.more or deferred
        state = {
            "v:": SCHEMA,
            "col": json.dumps(reader.sticky, sort_keys=True),
            "col-n": str(len(reader.sticky)),
        }
        next_cursor = Cursor(dict(reader.files), state, cursor.revision + 1)
        if not reader.usable_any and not reader.more:
            return Batch((), cursor, Coverage.UNAVAILABLE, cursor.revision, retry_blocked=True)
        coverage = Coverage.PARTIAL if reader.partial or reader.more else Coverage.COMPLETE
        return Batch(
            reader.events.ordered(),
            next_cursor,
            coverage,
            cursor.revision,
            skipped=reader.skipped,
            more=reader.more,
            retry_blocked=reader.retry_blocked,
        )


READER = SqliteSessionUsageReader()
