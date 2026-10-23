"""Standalone, offline status-line hook; persist only allowlisted numeric allowance data."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import select
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 64 * 1024
# Deeper documents are refused before the parser sees them; a payload this nested is never real.
MAX_DEPTH = 32
# The whole stdin read; a writer that never closes the pipe must not hold the status line.
READ_DEADLINE_S = 2.0
DEFAULT_CACHE = Path.home() / ".config/relaydeck/state/statusline-limits.json"
LOCK_NAME = ".statusline-limits.lock"
# Lock file: "<receipt_ms>:<confirm_ms>:<generation>" as ASCII. The receipt is written on every
# confirmation and fences older writers. The confirm time moves only on a fresh payload that
# carries limit rows.
LOCK_RE = re.compile(rb"([0-9]{1,16}):([0-9]{1,16})?:([0-9a-f]{32})?")
LEGACY_LOCK_RE = re.compile(rb"[0-9]{1,16}")
GENERATION_DOMAIN = b"relaydeck-api-generation-v1\0"
SESSION_DOMAIN = b"relaydeck-session-v1\0"
# One "<session digest>:<generation>" line per session, oldest update first.
SESSIONS_NAME = ".statusline-sessions"
SESSION_RE = re.compile(rb"([0-9a-f]{32}):([0-9a-f]{32})")
MAX_SESSIONS = 64
SESSIONS_MAX_BYTES = MAX_SESSIONS * 66


def too_deep(raw: bytes, limit: int = MAX_DEPTH) -> bool:
    """True when JSON brackets nest deeper than `limit` (string contents are skipped)."""
    depth = 0
    in_string = escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > limit:
                return True
        elif byte in (0x7D, 0x5D):
            depth = max(0, depth - 1)
    return False


def read_stdin(limit: int = MAX_BYTES, deadline_s: float = READ_DEADLINE_S) -> bytes | None:
    """At most limit+1 bytes from stdin, or None when the deadline passes first."""
    fd = sys.stdin.fileno()
    end = time.monotonic() + deadline_s
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        chunk = os.read(fd, min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def api_generation(payload: dict[str, Any]) -> str | None:
    """Opaque digest of the numeric API-response fields. A repaint that only redraws the status line
    repeats these values, so the digest moves only when a new API response arrived. The session
    id is hashed under a fixed domain string and never stored. None when the fields are absent or
    invalid."""
    session = payload.get("session_id")
    cost = payload.get("cost")
    duration = cost.get("total_api_duration_ms") if isinstance(cost, dict) else None
    if (
        not isinstance(session, str)
        or not 0 < len(session) <= 256
        or not isinstance(duration, int | float)
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        return None
    material = GENERATION_DOMAIN + session.encode("utf-8") + b"\0" + repr(float(duration)).encode()
    return hashlib.sha256(material).hexdigest()[:32]


def session_key(payload: dict[str, Any]) -> str | None:
    """Digest of the session id under its own domain string; the raw id is never stored."""
    session = payload.get("session_id")
    if not isinstance(session, str) or not 0 < len(session) <= 256:
        return None
    return hashlib.sha256(SESSION_DOMAIN + session.encode("utf-8")).hexdigest()[:32]


def read_lock(fd: int) -> tuple[int, int | None, str | None] | None:
    """(receipt_ms, confirm_ms, generation) from the lock file, or None when empty or malformed."""
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 64)
    match = LOCK_RE.fullmatch(raw)
    if match:
        receipt, confirm, generation = match.groups()
        return (
            int(receipt),
            int(confirm) if confirm is not None else None,
            generation.decode() if generation is not None else None,
        )
    if LEGACY_LOCK_RE.fullmatch(raw):  # written by the previous hook: a receipt only
        return int(raw), None, None
    return None


def windows_from(value: object) -> dict[str, dict[str, int | float]]:
    windows: dict[str, dict[str, int | float]] = {}
    if not isinstance(value, dict):
        return windows
    for name in ("five_hour", "seven_day"):
        row = value.get(name)
        if not isinstance(row, dict):
            continue
        used, reset = row.get("used_percentage"), row.get("resets_at")
        if (
            not isinstance(used, int | float)
            or isinstance(used, bool)
            or not 0 <= used <= 100
            or not math.isfinite(used)
            or type(reset) is not int
            or not 0 <= reset <= 253402300799
        ):
            continue
        windows[name] = {"used_percentage": used, "resets_at": reset}
    return windows


def live(
    windows: dict[str, dict[str, int | float]], now_ms: int
) -> dict[str, dict[str, int | float]]:
    return {name: row for name, row in windows.items() if row["resets_at"] * 1000 > now_ms}


def merge_windows(
    old: dict[str, dict[str, int | float]], new: dict[str, dict[str, int | float]], kind: str
) -> dict[str, dict[str, int | float]]:
    """Merge live rows: add missing rows; fresh replaces; repaint preserves existing rows.
    Baseline accepts newer resets; legacy also accepts higher use at equal reset."""
    merged = dict(old)
    for name, row in new.items():
        prev = merged.get(name)
        if prev is None or kind == "fresh":
            merged[name] = row
        elif kind == "repaint":
            continue
        elif row["resets_at"] > prev["resets_at"]:
            merged[name] = row
        elif (
            kind == "legacy"
            and row["resets_at"] == prev["resets_at"]
            and row["used_percentage"] > prev["used_percentage"]
        ):
            merged[name] = row
    return merged


def classify(known: dict[str, str], session: str | None, generation: str | None) -> str:
    if session is None or generation is None:
        return "legacy"
    stored = known.get(session)
    if stored is None:
        return "baseline"
    return "repaint" if stored == generation else "fresh"


def remember(
    known: dict[str, str], session: str | None, generation: str | None, kind: str
) -> dict[str, str]:
    """A fresh or first-seen session moves to the end; the oldest entries are evicted."""
    if session is None or generation is None or kind in ("legacy", "repaint"):
        return known
    updated = {key: value for key, value in known.items() if key != session}
    updated[session] = generation
    while len(updated) > MAX_SESSIONS:
        del updated[next(iter(updated))]
    return updated


def read_sessions(path: Path) -> dict[str, str]:
    """The stored session map, or an empty one when the file is absent or untrustworthy."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > SESSIONS_MAX_BYTES
        ):
            return {}
        raw = stream.read(SESSIONS_MAX_BYTES + 1)
    lines = raw.split(b"\n")
    if len(raw) > SESSIONS_MAX_BYTES or lines.pop() != b"":
        return {}
    known: dict[str, str] = {}
    for line in lines:
        match = SESSION_RE.fullmatch(line)
        if match is None or match[1].decode() in known:
            return {}
        known[match[1].decode()] = match[2].decode()
    return known if len(known) <= MAX_SESSIONS else {}


def sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(target: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".statusline-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    sync_directory(target.parent)


def read_cache(path: Path) -> dict[str, Any] | None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > MAX_BYTES
        ):
            raise ValueError
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError
    if too_deep(raw):
        raise ValueError
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"captured_ms", "windows"}
        or type(value["captured_ms"]) is not int
        or value["captured_ms"] < 0
        or not value["windows"]
        or windows_from(value["windows"]) != value["windows"]
    ):
        raise ValueError
    return value


def persist(
    path: Path,
    windows: dict[str, dict[str, int | float]],
    captured_ms: int,
    generation: str | None = None,
    session: str | None = None,
) -> bool:
    """Persist classified windows under the receipt fence; only fresh live limits confirm freshness.
    Consume the generation last; empty payloads update bookkeeping only."""
    try:
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            return False
        if windows_from(windows) != windows or type(captured_ms) is not int or captured_ms < 0:
            return False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(p.is_symlink() for p in path.parents) or path.parent.stat().st_uid != os.getuid():
            return False
        os.chmod(path.parent, 0o700)
        lock_fd = os.open(
            path.parent / LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(lock_fd, "rb") as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return False
            os.fchmod(lock.fileno(), 0o600)
            deadline = time.monotonic() + 0.15
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.005)
            try:
                old = read_cache(path)
            except FileNotFoundError:
                old = None
            # Lock metadata holds only the latest receipt clock, never stdin. It prevents
            # an older DIFFERENT payload winning after a newer identical repaint, while
            # the cache's captured_ms remains the first actual observation of that payload.
            prior = read_lock(lock.fileno())
            if prior is not None and captured_ms < prior[0]:
                return False
            if old is not None and captured_ms < old["captured_ms"]:
                return False
            sessions_path = path.parent / SESSIONS_NAME
            known = read_sessions(sessions_path)
            kind = classify(known, session, generation)
            incoming = live(windows, captured_ms)
            confirm = prior[1] if prior is not None else None
            if kind == "fresh" and incoming:
                confirm = captured_ms
            kept = generation if generation is not None else (prior[2] if prior else None)
            receipted = False
            if windows:
                stored = live(old["windows"], captured_ms) if old is not None else {}
                if kind == "fresh":
                    stored = {k: v for k, v in stored.items() if k not in windows or k in incoming}
                merged = merge_windows(stored, incoming, kind)
                if not merged:
                    if old is not None:
                        # Fence older writers before deleting the cache timestamp.
                        _receipt(lock.fileno(), captured_ms, confirm, kept)
                        receipted = True
                        path.unlink()
                        sync_directory(path.parent)
                elif old is None or merged != old["windows"]:
                    body = {"captured_ms": captured_ms, "windows": merged}
                    write_atomic(
                        path,
                        json.dumps(
                            body, sort_keys=True, separators=(",", ":"), allow_nan=False
                        ).encode(),
                    )
            if not receipted:
                _receipt(lock.fileno(), captured_ms, confirm, kept)
            # Publish the cache and receipt before consuming this generation.
            remembered = remember(known, session, generation, kind)
            if list(remembered.items()) != list(known.items()):
                write_atomic(
                    sessions_path, "".join(f"{k}:{v}\n" for k, v in remembered.items()).encode()
                )
            return bool(windows)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return False


def _receipt(fd: int, receipt_ms: int, confirm_ms: int | None, generation: str | None) -> None:
    text = f"{receipt_ms}:{'' if confirm_ms is None else confirm_ms}:{generation or ''}"
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, text.encode("ascii"))
    os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    os.fsync(fd)


def display(payload: dict[str, Any]) -> str:
    model = payload.get("model")
    name = model.get("display_name", model.get("id", "?")) if isinstance(model, dict) else "?"
    name = re.sub(r"[^A-Za-z0-9 ._:/-]", "", name)[:60] if isinstance(name, str) else "?"
    context = payload.get("context_window")
    used = context.get("used_percentage") if isinstance(context, dict) else None
    percent = (
        f"{used:g}%"
        if (
            isinstance(used, int | float)
            and not isinstance(used, bool)
            and 0 <= used <= 100
            and math.isfinite(used)
        )
        else "?"
    )
    return f"{name or '?'} | ctx {percent}"[:80]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()
    payload: dict[str, Any] = {}
    try:
        data = read_stdin()
        receipt = int(time.time() * 1000)
        if data is not None and len(data) <= MAX_BYTES and not too_deep(data):
            value = json.loads(data)
            if isinstance(value, dict):
                payload = value
                persist(
                    args.cache_path,
                    windows_from(payload.get("rate_limits")),
                    receipt,
                    api_generation(payload),
                    session_key(payload),
                )
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        pass
    print(display(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
