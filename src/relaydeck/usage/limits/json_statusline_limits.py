"""Read a private single-account hook cache; transcript root is deliberately separate."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path

from relaydeck.usage.contracts import (
    LIMIT_STALE_MS,
    Budget,
    Cursor,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
)

MAX_CACHE_BYTES = 64 * 1024
# The status-line hook keeps its lock file as "<receipt_ms>:<confirm_ms>:<generation>" (see
# statusline_limits_hook.read_lock). The installed hook is a standalone script and cannot import
# this module, so the name and format are mirrored here. tests/unit/test_usage_hook_freshness.py
# drives the real hook and checks that both sides agree.
LOCK_NAME = ".statusline-limits.lock"
MAX_WATERMARK_BYTES = 64
WATERMARK_RE = re.compile(rb"[0-9]{1,16}:([0-9]{1,16})?:(?:[0-9a-f]{32})?")
# A confirmation may be this far ahead of the reader's clock (clock skew); further is ignored.
FUTURE_SKEW_MS = 60 * 1000


def receipt_watermark(cache_path: Path, now_ms: int) -> int | None:
    """The hook's confirm time (last API-generation change), or None when it is absent, unsafe,
    malformed or in the future. Its receipt field is not used here.

    Read-only: the lock is never taken or waited on, and nothing is written. Anything that does not
    match the strict format falls back to the cache's own captured time.
    """
    try:
        fd = os.open(cache_path.parent / LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or info.st_size > MAX_WATERMARK_BYTES
            ):
                return None
            raw = stream.read(MAX_WATERMARK_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_WATERMARK_BYTES:
        return None
    match = WATERMARK_RE.fullmatch(raw)
    if match is None or match.group(1) is None:
        return None
    value = int(match.group(1))
    return value if value <= now_ms + FUTURE_SKEW_MS else None


class JsonStatuslineLimitsReader:
    format_id = "json-statusline-limits-v1"

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = (
            cache_path or Path.home() / ".config/relaydeck/state/statusline-limits.json"
        )

    def read(
        self, root: Path, kind: str, cursor: Cursor, budget: Budget, now_ms: int
    ) -> tuple[tuple[LimitSnapshot, ...], Cursor]:
        started = time.monotonic()
        path = self.cache_path
        if budget.max_files < 1 or budget.max_bytes < 1 or budget.deadline_ms <= 0:
            return (), cursor
        try:
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                return (), cursor
            parent = path.parent.stat()
            if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                return (), cursor
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                bound = min(MAX_CACHE_BYTES, budget.max_bytes)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > bound
                ):
                    return (), cursor
                raw = stream.read(bound + 1)
            if len(raw) > bound:
                return (), cursor
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"captured_ms", "windows"}:
                return (), cursor
            captured, rows = value["captured_ms"], value["windows"]
            if (
                type(captured) is not int
                or not 0 <= captured <= now_ms
                or not isinstance(rows, dict)
                or not rows
                or set(rows) - {"five_hour", "seven_day"}
            ):
                return (), cursor
            # Observed time: an identical repaint is still evidence the reading is current.
            watermark = receipt_watermark(path, now_ms)
            observed = max(captured, watermark) if watermark is not None else captured
            windows = []
            for name in ("five_hour", "seven_day"):
                if name not in rows:
                    continue
                row = rows[name]
                if not isinstance(row, dict) or set(row) != {"used_percentage", "resets_at"}:
                    return (), cursor
                used, reset = row["used_percentage"], row["resets_at"]
                if (
                    type(used) not in (int, float)
                    or not math.isfinite(used)
                    or not 0 <= used <= 100
                    or type(reset) is not int
                    or not 0 <= reset <= 253402300799
                ):
                    return (), cursor
                status = (
                    LimitStatus.EXPIRED
                    if now_ms >= reset * 1000
                    else LimitStatus.STALE
                    if now_ms - observed > LIMIT_STALE_MS
                    else LimitStatus.FRESH
                )
                windows.append(
                    LimitWindow(
                        name,
                        float(used),
                        300 if name == "five_hour" else 10080,
                        reset * 1000,
                        status,
                    )
                )
            if (time.monotonic() - started) * 1000 > budget.deadline_ms:
                return (), cursor
            worst = (
                LimitStatus.EXPIRED
                if any(w.status == LimitStatus.EXPIRED for w in windows)
                else LimitStatus.STALE
                if any(w.status == LimitStatus.STALE for w in windows)
                else LimitStatus.FRESH
            )
            snapshot = LimitSnapshot("", "single-account", observed, tuple(windows), worst)
            return (snapshot,), Cursor(
                state={"digest": hashlib.sha256(raw).hexdigest()}, revision=cursor.revision
            )
        except (OSError, ValueError, TypeError, OverflowError):
            return (), cursor
