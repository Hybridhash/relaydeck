"""A limits reading stays fresh while the API generation keeps being confirmed.

Identical repaints are not new evidence. A new API generation (a response arrived) is. The hook
writes the cache and its lock; the reader takes the observed time from the lock's confirm field.
These tests drive the real hook and the real reader in a temporary directory. Nothing under
the owner's CLI settings or ~/.config/relaydeck is touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage.contracts import LIMIT_STALE_MS, Budget, Cursor, LimitStatus
from relaydeck.usage.limits import json_statusline_limits as reader_module
from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader
from relaydeck.usage.resources import statusline_limits_hook as hook

T0 = 1_700_000_000_000
MINUTE = 60 * 1000
GEN_A = "a" * 32
GEN_B = "b" * 32
SES_A = "e" * 32
SES_B = "f" * 32
WINDOWS = {
    "five_hour": {"used_percentage": 12.5, "resets_at": (T0 + 10 * 3600 * 1000) // 1000},
    "seven_day": {"used_percentage": 30, "resets_at": (T0 + 4 * 24 * 3600 * 1000) // 1000},
}
CHANGED = {
    "five_hour": {"used_percentage": 40, "resets_at": (T0 + 10 * 3600 * 1000) // 1000},
    "seven_day": {"used_percentage": 30, "resets_at": (T0 + 4 * 24 * 3600 * 1000) // 1000},
}
HOOK_PATH = Path(hook.__file__)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory / "statusline-limits.json"


def _read(cache: Path, now: int) -> Any:
    snapshots, _cursor = JsonStatuslineLimitsReader(cache).read(
        cache.parent, "kind-a", Cursor(), Budget(), now
    )
    return snapshots[0] if snapshots else None


def _lock(cache: Path) -> Path:
    return cache.parent / hook.LOCK_NAME


# -- freshness: a reading stays fresh while generations are confirmed ----------


def test_identical_windows_with_a_new_generation_are_fresh(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    later = T0 + 14 * MINUTE
    assert hook.persist(cache, WINDOWS, later, GEN_B, SES_A)  # same numbers, a new API response
    assert json.loads(cache.read_text())["captured_ms"] == T0  # cache keeps the first observation
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == later
    assert all(w.status == LimitStatus.FRESH for w in snapshot.windows)
    assert snapshot.status == LimitStatus.FRESH


def test_a_repaint_with_the_same_generation_does_not_move_the_age(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    for minute in (5, 10, 15):  # the timer keeps redrawing the same API response
        assert hook.persist(cache, WINDOWS, T0 + minute * MINUTE, GEN_A, SES_A)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0
    assert snapshot.status == LimitStatus.STALE  # 20 minutes since the last API generation


def test_changed_numbers_under_a_new_generation_take_the_new_captured_time(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    assert hook.persist(cache, CHANGED, T0 + 3 * MINUTE, GEN_B, SES_A)
    snapshot = _read(cache, T0 + 4 * MINUTE)
    assert snapshot.observed_ms == T0 + 3 * MINUTE
    assert snapshot.windows[0].used_percent == 40.0


def test_changed_numbers_under_the_same_generation_count_as_a_repaint(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    assert hook.persist(cache, CHANGED, T0 + 3 * MINUTE, GEN_A, SES_A)
    snapshot = _read(cache, T0 + 4 * MINUTE)
    assert snapshot.observed_ms == T0
    assert snapshot.windows[0].used_percent == 12.5


def test_a_fresh_generation_after_a_repaint_run_resets_the_age(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    assert hook.persist(cache, WINDOWS, T0 + 10 * MINUTE, GEN_A, SES_A)
    assert hook.persist(cache, WINDOWS, T0 + 16 * MINUTE, GEN_B, SES_A)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0 + 16 * MINUTE
    assert snapshot.status == LimitStatus.FRESH


def test_the_stale_threshold_stays_fifteen_minutes_not_two(cache: Path) -> None:
    assert LIMIT_STALE_MS == 15 * MINUTE
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    snapshot = _read(cache, T0 + LIMIT_STALE_MS)
    assert snapshot.status == LimitStatus.FRESH  # exactly the threshold is not stale
    snapshot = _read(cache, T0 + LIMIT_STALE_MS + 1)
    assert snapshot.status == LimitStatus.STALE


# -- missing fields and the fallback -------------------------------------------------------------


def test_missing_generation_falls_back_to_the_captured_time(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0)  # no generation: the payload had no usable fields
    assert hook.persist(cache, WINDOWS, T0 + 10 * MINUTE)  # a repaint without evidence
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0
    assert snapshot.status == LimitStatus.STALE


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"session_id": "s1"},
        {"cost": {"total_api_duration_ms": 5}},
        {"session_id": "", "cost": {"total_api_duration_ms": 5}},
        {"session_id": "s1", "cost": {"total_api_duration_ms": True}},
        {"session_id": "s1", "cost": {"total_api_duration_ms": -1}},
        {"session_id": "s1", "cost": {"total_api_duration_ms": float("nan")}},
        {"session_id": "s1", "cost": "not-an-object"},
        {"session_id": 7, "cost": {"total_api_duration_ms": 5}},
    ],
)
def test_invalid_or_missing_fields_give_no_generation(payload: dict[str, Any]) -> None:
    assert hook.api_generation(payload) is None


def test_the_generation_is_opaque_deterministic_and_moves_with_the_duration() -> None:
    first = {"session_id": "synthetic-session-SECRET", "cost": {"total_api_duration_ms": 1200}}
    again = {"session_id": "synthetic-session-SECRET", "cost": {"total_api_duration_ms": 1200}}
    later = {"session_id": "synthetic-session-SECRET", "cost": {"total_api_duration_ms": 1300}}
    other = {"session_id": "another-session", "cost": {"total_api_duration_ms": 1200}}
    digest = hook.api_generation(first)
    assert digest is not None and len(digest) == 32 and set(digest) <= set("0123456789abcdef")
    assert digest == hook.api_generation(again)
    assert digest != hook.api_generation(later)
    assert digest != hook.api_generation(other)
    assert "SECRET" not in digest and "synthetic-session" not in digest


def test_the_session_id_is_never_written_to_disk(tmp_path: Path) -> None:
    cache = tmp_path / "state/statusline-limits.json"
    payload = {
        "session_id": "synthetic-session-SECRET",
        "cost": {"total_api_duration_ms": 1200},
        "rate_limits": {
            "five_hour": {"used_percentage": 12.5, "resets_at": 4102444800},
            "seven_day": {"used_percentage": 30, "resets_at": 4102444800},
        },
    }
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK_PATH), "--cache-path", str(cache)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert (cache.parent / hook.SESSIONS_NAME).is_file()
    written = b"".join(p.read_bytes() for p in cache.parent.iterdir())
    assert b"synthetic-session" not in written and b"SECRET" not in written
    assert _read(cache, int(time.time() * 1000) + MINUTE) is not None  # still usable


# -- malformed and future watermarks -------------------------------------------------------------

DIGEST = "c" * 32


@pytest.mark.parametrize(
    "lock",
    [
        b"",
        b"abc",
        b"1700000000000",  # the legacy receipt-only format: no confirm field
        b"1700000000000:",
        b"1700000000000::",
        b"1700000000000:1700000000000",  # missing the generation separator
        f"1700000000000:1700000000000:{'C' * 32}".encode(),  # uppercase generation
        f"1700000000000:1700000000000:{'c' * 33}".encode(),
        f"1700000000000:1700000000000:{'c' * 32}\n".encode(),
        b"1700000000000:" + b"1" * 17 + b":" + DIGEST.encode(),  # over-long confirm field
        b"-5:-5:" + DIGEST.encode(),
        b"1700000000000:17x:" + DIGEST.encode(),
        b"x" * 65,
    ],
)
def test_a_malformed_watermark_falls_back_to_the_captured_time(cache: Path, lock: bytes) -> None:
    assert hook.persist(cache, WINDOWS, T0)
    _lock(cache).write_bytes(lock)
    _lock(cache).chmod(0o600)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0
    assert snapshot.status == LimitStatus.STALE


def test_a_future_confirmation_beyond_the_skew_is_ignored(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0)
    far = T0 + 60 * MINUTE
    _lock(cache).write_bytes(f"{T0}:{far}:{DIGEST}".encode())
    _lock(cache).chmod(0o600)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0


def test_a_confirmation_within_the_skew_allowance_is_used(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0)
    near = T0 + 20 * MINUTE + 30 * 1000  # 30 s ahead of the reader's clock
    _lock(cache).write_bytes(f"{T0}:{near}:{DIGEST}".encode())
    _lock(cache).chmod(0o600)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == near


def test_a_missing_lock_file_uses_the_captured_time(cache: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    _lock(cache).unlink()
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0


def test_a_symlinked_lock_is_ignored(cache: Path, tmp_path: Path) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    target = tmp_path / "elsewhere.lock"
    target.write_bytes(f"{T0}:{T0 + 19 * MINUTE}:{GEN_A}".encode())
    target.chmod(0o600)
    _lock(cache).unlink()
    os.symlink(target, _lock(cache))
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == T0


# -- the reader never writes and never locks -----------------------------------------------------


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_reader_never_writes_or_takes_the_lock(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    assert hook.persist(cache, WINDOWS, T0 + 10 * MINUTE, GEN_B, SES_A)
    before = _tree(cache.parent)

    import fcntl

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the reader must not lock")

    monkeypatch.setattr(fcntl, "flock", refuse)
    monkeypatch.setattr(fcntl, "lockf", refuse)
    for minute in (11, 15, 25):
        _read(cache, T0 + minute * MINUTE)
    assert _tree(cache.parent) == before


def test_the_reader_and_the_hook_agree_on_the_lock_file(cache: Path) -> None:
    assert reader_module.LOCK_NAME == hook.LOCK_NAME
    assert hook.persist(cache, WINDOWS, T0, GEN_A, SES_A)
    assert _lock(cache).exists()
    assert hook.persist(cache, WINDOWS, T0 + 12 * MINUTE, GEN_B, SES_A)
    assert _read(cache, T0 + 20 * MINUTE).observed_ms == T0 + 12 * MINUTE
