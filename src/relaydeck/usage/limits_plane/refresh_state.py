"""File-backed on-demand refresh state.

One JSON document holding cooldowns, saved tool session ids, attempt records
and the tab-ownership journal. Mode 0600, atomic replace. All access goes
through RefreshState, which serializes with a lock. A corrupt or unreadable
file starts empty (fail open: a restart must not wedge refreshes; worst case
one extra refresh runs). No screen text, paths or prompts are ever stored:
only ids, timestamps, journal phases and result codes.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from relaydeck.contracts.canonical import canonical_json

STATE_FILENAME = "limits-refresh.json"
STATE_VERSION = 1
MAX_JOURNAL = 200
# Journal phases, newest wins per (attempt, tab).
PHASES = frozenset({"intent", "created", "closed", "uncertain", "recovered", "server_replaced"})
RESOLVED = frozenset({"closed", "recovered", "server_replaced"})


@dataclass(frozen=True)
class SavedSession:
    session_id: str
    provenance: str  # "hint" (exit hint + corroboration)
    saved_ms: int


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    finished_ms: int
    result: str


def session_scope(kind: str, root: str, workspace: str) -> str:
    """Opaque saved-session key, scoped per tool + session root + workspace.
    JSON-encoded so no separator can collide with path characters."""
    return json.dumps([kind, root, workspace], separators=(",", ":"))


def socket_identity(socket_path: str) -> tuple[int, int] | None:
    """(st_dev, st_ino) of the Herdr socket: a replaced server re-creates it,
    so a mismatch proves discontinuity. None when it cannot be statted."""
    try:
        info = os.stat(socket_path)
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _clean_str(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        return None
    if any(ord(c) < 32 for c in value):
        return None
    return value


def _clean_ms(value: Any) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


class RefreshState:
    """Thread-safe access to the refresh state document."""

    def __init__(self, path: Path, clock_ms: Callable[[], int] | None = None) -> None:
        self._path = path
        self._clock = clock_ms
        self._lock = Lock()
        self._cooldowns: dict[str, int] = {}
        self._sessions: dict[str, SavedSession] = {}
        self._attempts: dict[str, AttemptRecord] = {}
        self._journal: list[dict[str, Any]] = []
        self._evicted = 0
        self._load()

    # -- cooldowns ----------------------------------------------------------

    def cooldown_until(self, kind: str) -> int:
        with self._lock:
            return self._cooldowns.get(kind, 0)

    def set_cooldown(self, kind: str, until_ms: int) -> None:
        if _clean_str(kind, 256) is None or _clean_ms(until_ms) is None:
            return
        with self._lock:
            self._cooldowns[kind] = until_ms
            self._save_locked()

    # -- saved sessions ------------------------------------------------------

    def saved_session(self, scope: str) -> SavedSession | None:
        with self._lock:
            return self._sessions.get(scope)

    def save_session(self, scope: str, session_id: str, provenance: str) -> None:
        if (
            _clean_str(scope, 1024) is None
            or _clean_str(session_id, 256) is None
            or provenance != "hint"
        ):
            return
        with self._lock:
            now = self._clock() if self._clock is not None else 0
            self._sessions[scope] = SavedSession(session_id, provenance, now)
            self._save_locked()

    def clear_session(self, scope: str) -> None:
        with self._lock:
            if self._sessions.pop(scope, None) is not None:
                self._save_locked()

    # -- attempts --------------------------------------------------------------

    def record_attempt(self, kind: str, attempt_id: str, finished_ms: int, result: str) -> None:
        if (
            _clean_str(kind, 256) is None
            or _clean_str(attempt_id, 64) is None
            or _clean_ms(finished_ms) is None
            or _clean_str(result, 64) is None
        ):
            return
        with self._lock:
            self._attempts[kind] = AttemptRecord(attempt_id, finished_ms, result)
            self._save_locked()

    def last_attempt(self, kind: str) -> AttemptRecord | None:
        with self._lock:
            return self._attempts.get(kind)

    # -- journal -----------------------------------------------------------------

    def journal_append(self, entry: dict[str, Any]) -> None:
        """Append a journal entry (must carry attempt/tab/phase/at_ms)."""
        if (
            not isinstance(entry, dict)
            or _clean_str(entry.get("attempt"), 64) is None
            or entry.get("phase") not in PHASES
            or _clean_ms(entry.get("at_ms")) is None
        ):
            return
        tab = entry.get("tab")
        if tab is not None and _clean_str(tab, 64) is None:
            return
        with self._lock:
            self._journal.append(
                {
                    key: entry[key]
                    for key in (
                        "attempt",
                        "tab",
                        "pane",
                        "phase",
                        "server",
                        "label",
                        "workspace",
                        "at_ms",
                        "note",
                    )
                    if key in entry
                }
            )
            self._prune_locked()
            self._save_locked()

    def journal_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._journal]

    def latest_phase(self, attempt: str, tab: str) -> str | None:
        with self._lock:
            phase: str | None = None
            for entry in self._journal:
                if entry.get("attempt") == attempt and entry.get("tab") == tab:
                    candidate = entry.get("phase")
                    phase = candidate if isinstance(candidate, str) else phase
            return phase

    def unresolved(self) -> list[dict[str, Any]]:
        """Newest entry per (attempt, tab) whose latest phase is unresolved."""
        with self._lock:
            newest: dict[tuple[str, str], dict[str, Any]] = {}
            for entry in self._journal:
                attempt, tab = entry.get("attempt"), entry.get("tab")
                if isinstance(attempt, str) and isinstance(tab, str):
                    newest[(attempt, tab)] = entry
            return [dict(entry) for entry in newest.values() if entry.get("phase") not in RESOLVED]

    # -- persistence ---------------------------------------------------------------

    def _prune_locked(self) -> None:
        if len(self._journal) <= MAX_JOURNAL:
            return
        resolved = [e for e in self._journal if e.get("phase") in RESOLVED]
        drop = len(self._journal) - MAX_JOURNAL
        drop_resolved = min(drop, len(resolved))
        keep_resolved = len(resolved) - drop_resolved
        seen = 0
        kept: list[dict[str, Any]] = []
        for entry in self._journal:
            if entry.get("phase") in RESOLVED:
                seen += 1
                if seen <= keep_resolved:
                    kept.append(entry)
            else:
                kept.append(entry)
        self._journal = kept
        if len(self._journal) > MAX_JOURNAL:
            overflow = len(self._journal) - MAX_JOURNAL
            self._journal = self._journal[overflow:]
            self._evicted += overflow

    def _save_locked(self) -> None:
        doc = {
            "version": STATE_VERSION,
            "cooldowns": dict(self._cooldowns),
            "sessions": {
                scope: {
                    "id": saved.session_id,
                    "provenance": saved.provenance,
                    "saved_ms": saved.saved_ms,
                }
                for scope, saved in self._sessions.items()
            },
            "attempts": {
                kind: {
                    "id": record.attempt_id,
                    "finished_ms": record.finished_ms,
                    "result": record.result,
                }
                for kind, record in self._attempts.items()
            },
            "journal": self._journal,
            "evicted_unresolved": self._evicted,
        }
        try:
            raw = canonical_json(doc).encode()
        except (TypeError, ValueError):
            return
        tmp = self._path.parent / (self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                # os.write may write fewer bytes than asked; never publish a truncated state.
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            with open(self._path, "rb") as stream:
                doc = json.loads(stream.read(1024 * 1024))
        except (OSError, ValueError):
            return
        if not isinstance(doc, dict) or doc.get("version") != STATE_VERSION:
            return
        cooldowns = doc.get("cooldowns")
        if isinstance(cooldowns, dict):
            for kind, until in cooldowns.items():
                if _clean_str(kind, 256) is not None and _clean_ms(until) is not None:
                    self._cooldowns[kind] = until
        sessions = doc.get("sessions")
        if isinstance(sessions, dict):
            for scope, row in sessions.items():
                if (
                    _clean_str(scope, 1024) is None
                    or not isinstance(row, dict)
                    or _clean_str(row.get("id"), 256) is None
                    or row.get("provenance") != "hint"
                    or _clean_ms(row.get("saved_ms")) is None
                ):
                    continue
                self._sessions[scope] = SavedSession(row["id"], "hint", row["saved_ms"])
        attempts = doc.get("attempts")
        if isinstance(attempts, dict):
            for kind, row in attempts.items():
                if (
                    _clean_str(kind, 256) is None
                    or not isinstance(row, dict)
                    or _clean_str(row.get("id"), 64) is None
                    or _clean_ms(row.get("finished_ms")) is None
                    or _clean_str(row.get("result"), 64) is None
                ):
                    continue
                self._attempts[kind] = AttemptRecord(row["id"], row["finished_ms"], row["result"])
        journal = doc.get("journal")
        if isinstance(journal, list):
            for entry in journal[: MAX_JOURNAL * 2]:
                if (
                    isinstance(entry, dict)
                    and _clean_str(entry.get("attempt"), 64) is not None
                    and entry.get("phase") in PHASES
                    and _clean_ms(entry.get("at_ms")) is not None
                ):
                    self._journal.append(entry)
        evicted = doc.get("evicted_unresolved")
        if type(evicted) is int and evicted >= 0:
            self._evicted = evicted
