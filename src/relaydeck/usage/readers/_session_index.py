"""Same-root session metadata for the event-usage reader.

The fixed leaf `session-index.db` beside `sessions/` maps session ids to
their model and working folder. Reads are live read-only snapshots: `mode=ro`
(a WAL database stays consistent; `immutable` would hide the WAL and is never
used), `query_only`, in-memory temp store, one `BEGIN` snapshot, no copies,
no sidecars, a short busy timeout.

A missing, oversized, over-rowed, corrupt or locked index yields no
enrichment (None), never an error: events are still stored, unverified.
Field allowlist: `session_id`, `model_id`, `workspace_root` only; title,
prompt and search columns are never selected.

Retrieval is bounded: at most MAX_INDEX_ROWS + 1 rows stream in chunks (one
past the cap proves over-rowed), every text field arrives capped at its own
validation limit, and an optional deadline aborts the statement.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaydeck.usage.contracts import MAX_KEY_CHARS, MAX_MODEL_CHARS
from relaydeck.usage.link_digests import MAX_PATH_CHARS

INDEX_FILENAME = "session-index.db"
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_INDEX_ROWS = 100_000
BUSY_TIMEOUT_S = 1.0
_FETCH_ROWS = 200
_PROGRESS_OPS = 1000


def _capped(column: str, cap: int) -> str:
    """Bounded retrieval of a text column at its validation limit. Text longer
    than the cap arrives truncated one past it (the caller applies the same
    validation as before, so outcomes are unchanged); every other type passes
    through untouched. Column names are module constants, never input."""
    return (
        f"CASE WHEN typeof({column}) = 'text' THEN substr({column}, 1, {cap + 1}) ELSE {column} END"
    )


@dataclass(frozen=True)
class _Entry:
    model: str | None
    workspace: str | None


class SessionIndex:
    """Validated model/workspace lookups by session id, one snapshot."""

    def __init__(self, entries: dict[str, _Entry]) -> None:
        self._entries = entries

    def model(self, session_id: str) -> str | None:
        entry = self._entries.get(session_id)
        return entry.model if entry is not None else None

    def workspace(self, session_id: str) -> str | None:
        entry = self._entries.get(session_id)
        return entry.workspace if entry is not None else None


def load_session_index(
    root: Path, *, deadline_ms: float | None = None
) -> tuple[SessionIndex | None, int]:
    """(index or None, bytes charged to the pass budget).

    The leaf must be a regular file owned by the caller; anything else,
    including a symlink, a lock timeout, a schema mismatch, an over-rowed
    table or a missed deadline, is no enrichment rather than an error.
    """
    try:
        info = os.lstat(root / INDEX_FILENAME)
    except OSError:
        return None, 0
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_size > MAX_INDEX_BYTES
    ):
        return None, 0
    try:
        db = sqlite3.connect(
            f"file:{root / INDEX_FILENAME}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S
        )
    except (sqlite3.Error, ValueError):
        return None, 0
    rows: list[Any] = []
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("BEGIN")
        if deadline_ms is not None:
            deadline = time.monotonic() + deadline_ms / 1000
            if time.monotonic() >= deadline:
                return None, 0
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, _PROGRESS_OPS)
        cursor = db.execute(
            f"SELECT {_capped('session_id', MAX_KEY_CHARS)},"  # noqa: S608 - constant column names and int bounds
            f" {_capped('model_id', MAX_MODEL_CHARS)},"
            f" {_capped('workspace_root', MAX_PATH_CHARS)}"
            f" FROM sessions LIMIT {MAX_INDEX_ROWS + 1}"
        )
        while True:
            chunk = cursor.fetchmany(_FETCH_ROWS)
            if not chunk:
                break
            rows.extend(chunk)
            if len(rows) > MAX_INDEX_ROWS:
                return None, 0
    except sqlite3.Error:
        return None, 0
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    entries: dict[str, _Entry] = {}
    for session_id, model_id, workspace_root in rows:
        if not isinstance(session_id, str) or not session_id or len(session_id) > MAX_KEY_CHARS:
            continue
        model = (
            model_id if isinstance(model_id, str) and 0 < len(model_id) <= MAX_MODEL_CHARS else None
        )
        workspace = (
            workspace_root
            if isinstance(workspace_root, str) and 0 < len(workspace_root) <= MAX_PATH_CHARS
            else None
        )
        entries[session_id] = _Entry(model, workspace)
    return SessionIndex(entries), info.st_size
