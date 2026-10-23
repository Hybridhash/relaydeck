"""Verify per-session usage merging, expiry, receipt fencing and atomic private publication."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage import hook_install
from relaydeck.usage.contracts import Budget, Cursor, LimitStatus
from relaydeck.usage.hook_install import install_hook
from relaydeck.usage.hook_resource import hook_bytes
from relaydeck.usage.hook_setup import HOOK_FILENAME, HOOK_SHA256, PREVIOUS_HOOK_SHA256
from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader
from relaydeck.usage.resources import statusline_limits_hook as hook

T0 = 1_700_000_000_000
MINUTE = 60 * 1000
GEN_A = "a" * 32
GEN_B = "b" * 32
GEN_C = "c" * 32
OLD_A = "1" * 32
OLD_B = "2" * 32
SES_A = "e" * 32
SES_B = "f" * 32
RESET_1 = T0 // 1000 + 3600
RESET_2 = T0 // 1000 + 2 * 3600
HOOK_PATH = Path(hook.__file__)


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    return directory / "statusline-limits.json"


def _windows(cache: Path) -> dict[str, Any]:
    return json.loads(cache.read_text())["windows"]


def _read(cache: Path, now: int) -> Any:
    snapshots, _cursor = JsonStatuslineLimitsReader(cache).read(
        cache.parent, "kind-a", Cursor(), Budget(), now
    )
    return snapshots[0] if snapshots else None


def _window(used: int | float, reset: int) -> dict[str, dict[str, int | float]]:
    return {"five_hour": {"used_percentage": used, "resets_at": reset}}


def _both(five: int, seven: int, reset: int) -> dict[str, dict[str, int | float]]:
    return {
        "five_hour": {"used_percentage": five, "resets_at": reset},
        "seven_day": {"used_percentage": seven, "resets_at": reset},
    }


def _seed(cache: Path, session: str, generation: str, at: int) -> None:
    assert not hook.persist(cache, {}, at, generation, session)


def _sessions(cache: Path) -> list[str]:
    path = cache.parent / hook.SESSIONS_NAME
    return path.read_text().splitlines() if path.exists() else []


def _confirm(cache: Path) -> str:
    return (cache.parent / hook.LOCK_NAME).read_text().split(":")[1]


def _run(cache: Path, payload: dict[str, Any]) -> None:
    result = subprocess.run(
        [sys.executable, "-B", str(HOOK_PATH), "--cache-path", str(cache)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0


# -- the merge rules -----------------------------------------------------------------


def test_the_latest_fresh_response_wins_in_either_order(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0 - 2 * MINUTE)
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(25, RESET_1), T0 + MINUTE, GEN_B, SES_B)
    assert _windows(cache) == _window(25, RESET_1)
    assert hook.persist(cache, _window(30, RESET_1), T0 + 2 * MINUTE, GEN_C, SES_A)
    assert _windows(cache) == _window(30, RESET_1)


def test_same_millisecond_fresh_responses_do_not_fence_each_other(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0 - 2 * MINUTE)
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(25, RESET_1), T0, GEN_B, SES_B)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert _windows(cache) == _window(30, RESET_1)


def test_a_new_reset_replaces_the_window_even_when_lower(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(5, RESET_2), T0 + MINUTE, GEN_B, SES_B)  # baseline, newer
    assert _windows(cache) == _window(5, RESET_2)
    assert json.loads(cache.read_text())["captured_ms"] == T0 + MINUTE


def test_a_baseline_with_an_older_reset_is_ignored(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_2), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(90, RESET_1), T0 + MINUTE, GEN_B, SES_B)
    assert _windows(cache) == _window(30, RESET_2)
    assert json.loads(cache.read_text())["captured_ms"] == T0


def test_a_fresh_payload_with_an_earlier_future_reset_replaces_the_row(cache: Path) -> None:
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_2), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(40, RESET_1), T0 + MINUTE, GEN_B, SES_B)
    assert _windows(cache) == _window(40, RESET_1)


def test_a_repaint_never_changes_a_row(cache: Path) -> None:
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(90, RESET_2), T0 + MINUTE, OLD_B, SES_B)
    assert _windows(cache) == _window(30, RESET_1)


def test_each_window_merges_independently_for_each_class(cache: Path) -> None:
    stored = {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_2},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_1},
    }
    incoming = {
        "five_hour": {"used_percentage": 90, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 12, "resets_at": RESET_1},
    }
    assert hook.persist(cache, stored, T0, GEN_A, SES_A)
    assert hook.persist(cache, incoming, T0 + MINUTE, GEN_B, SES_B)  # baseline: nothing changes
    assert _windows(cache) == stored
    assert hook.persist(cache, incoming, T0 + 2 * MINUTE, GEN_B, SES_B)  # repaint
    assert _windows(cache) == stored
    assert hook.persist(cache, incoming, T0 + 3 * MINUTE, GEN_C, SES_B)  # fresh: both replace
    assert _windows(cache) == incoming


def test_a_window_missing_from_a_payload_is_kept(cache: Path) -> None:
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _both(30, 10, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(40, RESET_1), T0 + MINUTE, GEN_B, SES_B)
    assert _windows(cache) == _both(40, 10, RESET_1)


@pytest.mark.parametrize(("fresh", "five"), [(True, 25), (False, 30)])
def test_a_window_missing_from_the_cache_is_added(cache: Path, fresh: bool, five: int) -> None:
    if fresh:
        _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _both(25, 10, RESET_1), T0 + MINUTE, GEN_B, SES_B)
    assert _windows(cache) == _both(five, 10, RESET_1)


def test_the_legacy_class_keeps_the_maximum(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0)
    assert hook.persist(cache, _window(25, RESET_1), T0 + MINUTE)
    assert _windows(cache) == _window(30, RESET_1)
    assert hook.persist(cache, _window(35, RESET_1), T0 + 2 * MINUTE)
    assert _windows(cache) == _window(35, RESET_1)
    assert hook.persist(cache, _window(5, RESET_2), T0 + 3 * MINUTE)
    assert _windows(cache) == _window(5, RESET_2)
    assert _sessions(cache) == []


# -- a manual reset lands ------------------------------------------------------------


def test_a_manual_reset_with_the_same_resets_at_lands(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(98, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(37, RESET_1), T0 + MINUTE, GEN_B, SES_A)
    assert _windows(cache) == _window(37, RESET_1)
    assert _confirm(cache) == str(T0 + MINUTE)


def test_an_idle_session_repainting_an_old_value_changes_nothing(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0 - 2 * MINUTE)
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(98, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(37, RESET_1), T0 + MINUTE, GEN_B, SES_A)
    assert hook.persist(cache, _window(98, RESET_1), T0 + 5 * MINUTE, OLD_B, SES_B)
    assert _windows(cache) == _window(37, RESET_1)
    assert _confirm(cache) == str(T0 + MINUTE)


def test_a_hundred_responses_later_an_old_repaint_is_still_ignored(cache: Path) -> None:
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    for number in range(100):
        generation = f"{number + 1:032x}"
        assert hook.persist(
            cache, _window(10 + number % 7, RESET_1), T0 + number, generation, SES_A
        )
    expected = _windows(cache)
    assert hook.persist(cache, _window(98, RESET_1), T0 + 200, OLD_B, SES_B)
    assert _windows(cache) == expected


def test_the_sixty_fifth_session_evicts_the_oldest_which_returns_as_a_baseline(
    cache: Path,
) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0)
    _seed(cache, SES_A, OLD_A, T0 + 1)
    for number in range(63):
        _seed(cache, f"{number + 1:032x}", OLD_B, T0 + 2 + number)
    assert len(_sessions(cache)) == 64
    _seed(cache, f"{99:032x}", OLD_B, T0 + 100)
    assert len(_sessions(cache)) == 64
    assert not any(line.startswith(SES_A) for line in _sessions(cache))
    assert hook.persist(cache, _window(98, RESET_1), T0 + 200, GEN_B, SES_A)
    assert _windows(cache) == _window(30, RESET_1)
    assert _sessions(cache)[-1] == f"{SES_A}:{GEN_B}"
    assert len(_sessions(cache)) == 64


def test_an_upgrade_needs_one_more_response_before_a_reset_shows(cache: Path) -> None:
    assert hook.persist(cache, _window(98, RESET_1), T0)  # the previous hook's cache, no map
    assert hook.persist(cache, _window(98, RESET_1), T0 + MINUTE, GEN_A, SES_B)
    assert hook.persist(cache, _window(37, RESET_1), T0 + 2 * MINUTE, GEN_A, SES_A)
    assert _windows(cache) == _window(98, RESET_1)
    assert hook.persist(cache, _window(37, RESET_1), T0 + 3 * MINUTE, GEN_B, SES_A)
    assert _windows(cache) == _window(37, RESET_1)


def test_a_resumed_session_whose_first_payload_carries_limits_is_a_baseline(cache: Path) -> None:
    assert hook.persist(cache, _window(98, RESET_1), T0)
    assert hook.persist(cache, _window(98, RESET_1), T0 + MINUTE, GEN_A, SES_A)
    assert _windows(cache) == _window(98, RESET_1)
    assert hook.persist(cache, _window(37, RESET_1), T0 + 2 * MINUTE, GEN_B, SES_A)
    assert _windows(cache) == _window(37, RESET_1)


def test_a_payload_without_limits_records_the_session_through_main(cache: Path) -> None:
    assert hook.persist(cache, _window(98, RESET_1), T0 // 2)
    session = {"session_id": "synthetic-session", "cost": {"total_api_duration_ms": 10}}
    _run(cache, session)
    assert len(_sessions(cache)) == 1
    assert _windows(cache) == _window(98, RESET_1)
    later = {**session, "cost": {"total_api_duration_ms": 20}}
    soon = int(time.time())
    _run(cache, {**later, "rate_limits": _window(37, soon + 3600)})
    assert _windows(cache) == _window(37, soon + 3600)


def test_a_generation_only_payload_writes_no_cache_and_keeps_the_confirm_time(
    cache: Path,
) -> None:
    assert not hook.persist(cache, {}, T0, GEN_A, SES_A)
    assert not cache.exists()
    assert _sessions(cache) == [f"{SES_A}:{GEN_A}"]
    assert _confirm(cache) == ""
    assert hook.persist(cache, _window(30, RESET_1), T0 + MINUTE, GEN_B, SES_A)
    assert _confirm(cache) == str(T0 + MINUTE)
    assert not hook.persist(cache, {}, T0 + 2 * MINUTE, GEN_C, SES_A)
    assert _confirm(cache) == str(T0 + MINUTE)
    assert _windows(cache) == _window(30, RESET_1)


def test_a_regressed_duration_is_a_new_generation() -> None:
    first = {"session_id": "s", "cost": {"total_api_duration_ms": 5000}}
    regressed = {"session_id": "s", "cost": {"total_api_duration_ms": 100}}
    known = {hook.session_key(first) or "": hook.api_generation(first) or ""}
    kind = hook.classify(known, hook.session_key(regressed), hook.api_generation(regressed))
    assert kind == "fresh"


def test_the_session_key_is_an_opaque_digest() -> None:
    key = hook.session_key({"session_id": "synthetic-session-SECRET"})
    assert key is not None and len(key) == 32 and set(key) <= set("0123456789abcdef")
    assert "SECRET" not in key
    assert key != hook.api_generation(
        {"session_id": "synthetic-session-SECRET", "cost": {"total_api_duration_ms": 1}}
    )
    for bad in ({}, {"session_id": ""}, {"session_id": 7}, {"session_id": "x" * 257}):
        assert hook.session_key(bad) is None


# -- expiry --------------------------------------------------------------------------


def test_a_fresh_payload_carrying_an_expired_row_drops_it_and_removes_the_window(
    cache: Path,
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    stored = {
        "five_hour": {"used_percentage": 30, "resets_at": RESET_1},
        "seven_day": {"used_percentage": 10, "resets_at": RESET_2},
    }
    assert hook.persist(cache, stored, T0, GEN_A, SES_A)
    later = RESET_1 * 1000 + MINUTE
    assert hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)
    assert _windows(cache) == {"seven_day": stored["seven_day"]}


@pytest.mark.parametrize("with_seven_day", [True, False])
def test_an_expired_fresh_row_removes_the_still_live_stored_window(
    cache: Path, with_seven_day: bool
) -> None:
    hour = 3600
    stored = {"five_hour": {"used_percentage": 98, "resets_at": RESET_2 + hour}}
    if with_seven_day:
        stored["seven_day"] = {"used_percentage": 10, "resets_at": RESET_2 + 10 * hour}
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, stored, T0, GEN_A, SES_A)
    now = (RESET_2 + hour) * 1000 - 3600 * 1000
    expired = _window(37, now // 1000 - 1800)
    assert hook.persist(cache, expired, now, GEN_B, SES_A)
    if with_seven_day:
        assert _windows(cache) == {"seven_day": stored["seven_day"]}
    else:
        assert not cache.exists()
    assert _confirm(cache) == str(T0)
    assert _sessions(cache) == [f"{SES_A}:{GEN_B}"]


@pytest.mark.parametrize("session_class", ["baseline", "repaint", "legacy"])
def test_an_expired_row_from_another_class_leaves_the_stored_window(
    cache: Path, session_class: str
) -> None:
    hour = 3600
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, _window(98, RESET_2 + hour), T0, GEN_A, SES_A)
    now = T0 + 3600 * 1000
    args = {"baseline": (GEN_C, "d" * 32), "repaint": (OLD_B, SES_B), "legacy": (None, None)}
    generation, session = args[session_class]
    expired = _window(37, now // 1000 - 1800)
    assert hook.persist(cache, expired, now, generation, session)
    assert _windows(cache) == _window(98, RESET_2 + hour)


def test_when_every_window_has_expired_the_cache_is_removed(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    later = RESET_1 * 1000 + MINUTE
    assert hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)
    assert not cache.exists()
    assert _read(cache, later) is None
    assert _confirm(cache) == str(T0)


def test_an_expired_incoming_row_is_never_written(cache: Path) -> None:
    late = RESET_1 * 1000 + MINUTE
    assert hook.persist(cache, _window(98, RESET_1), late, GEN_A, SES_A)
    assert not cache.exists()


# -- map hardening and publication ---------------------------------------------------------


def _bad_map(kind: str, directory: Path) -> None:
    path = directory / hook.SESSIONS_NAME
    good = f"{SES_A}:{OLD_A}\n".encode()
    elsewhere = directory.parent / "elsewhere"
    if kind == "symlink":
        elsewhere.write_bytes(good)
        elsewhere.chmod(0o600)
        os.symlink(elsewhere, path)
        return
    if kind == "hardlink":
        elsewhere.write_bytes(good)
        elsewhere.chmod(0o600)
        os.link(elsewhere, path)
        return
    if kind == "fifo":
        os.mkfifo(path, 0o600)
        return
    data = {
        "mode": good,
        "oversize": good * 70,
        "truncated": good[:-1],
        "partial": good[:40] + b"\n",
        "duplicate": good * 2,
        "uppercase": good.upper(),
        "garbage": b"not a map\n",
    }[kind]
    path.write_bytes(data)
    path.chmod(0o644 if kind == "mode" else 0o600)


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "hardlink",
        "fifo",
        "mode",
        "oversize",
        "truncated",
        "partial",
        "duplicate",
        "uppercase",
        "garbage",
    ],
)
def test_an_untrustworthy_map_counts_as_empty(cache: Path, kind: str) -> None:
    assert hook.persist(cache, _window(98, RESET_1), T0)
    _bad_map(kind, cache.parent)
    assert hook.read_sessions(cache.parent / hook.SESSIONS_NAME) == {}
    assert hook.persist(cache, _window(37, RESET_1), T0 + MINUTE, GEN_A, SES_A)  # a baseline
    assert _windows(cache) == _window(98, RESET_1)
    path = cache.parent / hook.SESSIONS_NAME
    assert stat.S_ISREG(path.lstat().st_mode) and stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert path.read_text() == f"{SES_A}:{GEN_A}\n"
    assert not list(cache.parent.glob(".statusline-*.tmp"))


def test_the_map_is_private_and_atomic(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0)
    path = cache.parent / hook.SESSIONS_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(cache.parent.glob(".statusline-*.tmp"))


def test_a_fresh_payload_moves_its_session_to_the_end_and_a_repaint_does_not(cache: Path) -> None:
    _seed(cache, SES_A, OLD_A, T0)
    _seed(cache, SES_B, OLD_B, T0 + 1)
    assert hook.persist(cache, _window(30, RESET_1), T0 + 2, OLD_A, SES_A)  # repaint
    assert _sessions(cache) == [f"{SES_A}:{OLD_A}", f"{SES_B}:{OLD_B}"]
    assert hook.persist(cache, _window(30, RESET_1), T0 + 3, GEN_A, SES_A)  # fresh
    assert _sessions(cache) == [f"{SES_B}:{OLD_B}", f"{SES_A}:{GEN_A}"]


def test_the_cache_is_published_before_the_receipt_and_the_map_last(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_write = hook.write_atomic
    real_receipt = hook._receipt

    def write(target: Path, data: bytes) -> None:
        order.append(target.name)
        real_write(target, data)

    def receipt(*args: Any) -> None:
        order.append("receipt")
        real_receipt(*args)

    monkeypatch.setattr(hook, "write_atomic", write)
    monkeypatch.setattr(hook, "_receipt", receipt)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert order == [cache.name, "receipt", hook.SESSIONS_NAME]


def _failing(monkeypatch: pytest.MonkeyPatch, step: str) -> None:
    real_write = hook.write_atomic

    def refuse(*args: Any) -> None:
        raise OSError("synthetic")

    if step == "receipt":
        monkeypatch.setattr(hook, "_receipt", refuse)
        return

    def write(target: Path, data: bytes) -> None:
        if target.name == step:
            raise OSError("synthetic")
        real_write(target, data)

    monkeypatch.setattr(hook, "write_atomic", write)


def test_a_failed_cache_write_changes_neither_map_nor_receipt(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    _failing(monkeypatch, cache.name)
    assert not hook.persist(cache, _window(37, RESET_1), T0, GEN_A, SES_A)
    monkeypatch.undo()
    assert not cache.exists()
    assert _sessions(cache) == [f"{SES_A}:{OLD_A}"]
    assert (cache.parent / hook.LOCK_NAME).read_text().startswith(f"{T0 - MINUTE}:")


def test_a_crash_after_the_cache_write_republishes_the_confirm_time_on_retry(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    _failing(monkeypatch, "receipt")
    assert not hook.persist(cache, _window(30, RESET_1), T0 + 14 * MINUTE, GEN_B, SES_A)
    monkeypatch.undo()
    assert _sessions(cache) == [f"{SES_A}:{GEN_A}"]
    assert _confirm(cache) == str(T0)
    assert hook.persist(cache, _window(30, RESET_1), T0 + 14 * MINUTE, GEN_B, SES_A)
    assert _confirm(cache) == str(T0 + 14 * MINUTE)
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.status == LimitStatus.FRESH and snapshot.observed_ms == T0 + 14 * MINUTE


def test_a_receipt_failure_before_a_deletion_keeps_the_cache_and_the_generation(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    later = RESET_1 * 1000 + MINUTE
    _failing(monkeypatch, "receipt")
    assert not hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)
    monkeypatch.undo()
    assert _windows(cache) == _window(30, RESET_1)
    assert _sessions(cache) == [f"{SES_A}:{GEN_A}"]
    assert (cache.parent / hook.LOCK_NAME).read_text().startswith(f"{T0}:")
    assert hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)
    assert not cache.exists() and _sessions(cache) == [f"{SES_A}:{GEN_B}"]
    assert _confirm(cache) == str(T0)


def test_a_failure_after_a_deletion_still_fences_a_delayed_older_payload(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    later = RESET_1 * 1000 + MINUTE

    def refuse(directory: Path) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(hook, "sync_directory", refuse)
    assert not hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)
    monkeypatch.undo()
    assert not cache.exists()
    assert _sessions(cache) == [f"{SES_A}:{GEN_A}"]
    assert (cache.parent / hook.LOCK_NAME).read_text().startswith(f"{later}:")
    assert not hook.persist(cache, _window(30, RESET_1), T0 + MINUTE, GEN_A, SES_A)  # delayed
    assert not cache.exists()
    assert _confirm(cache) == str(T0)
    assert hook.persist(cache, _window(98, RESET_1), later, GEN_B, SES_A)  # the retry
    assert not cache.exists() and _confirm(cache) == str(T0)


def test_a_failed_map_write_leaves_the_generation_unconsumed_and_the_receipt_newer(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    _failing(monkeypatch, hook.SESSIONS_NAME)
    assert not hook.persist(cache, _window(37, RESET_1), T0, GEN_A, SES_A)
    monkeypatch.undo()
    assert _sessions(cache) == [f"{SES_A}:{OLD_A}"]
    assert not hook.persist(cache, _window(98, RESET_1), T0 - 1, GEN_B, SES_B)  # delayed, older
    assert hook.persist(cache, _window(37, RESET_1), T0 + 1, GEN_A, SES_A)  # still fresh
    assert _confirm(cache) == str(T0 + 1)
    assert _windows(cache) == _window(37, RESET_1)
    assert _read(cache, T0 + 2 * MINUTE).status == LimitStatus.FRESH


def test_a_bookkeeping_only_retry_never_moves_the_confirm_time(
    cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(cache, SES_A, OLD_A, T0 - MINUTE)
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    _failing(monkeypatch, hook.SESSIONS_NAME)
    assert not hook.persist(cache, {}, T0 + MINUTE, GEN_B, SES_A)
    monkeypatch.undo()
    assert not hook.persist(cache, {}, T0 + 2 * MINUTE, GEN_B, SES_A)
    assert _confirm(cache) == str(T0)
    assert _sessions(cache) == [f"{SES_A}:{GEN_B}"]


def test_an_older_receipt_is_rejected_before_the_map_is_touched(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    before = _sessions(cache)
    assert not hook.persist(cache, _window(40, RESET_1), T0 - MINUTE, GEN_B, SES_B)
    assert not hook.persist(cache, {}, T0 - MINUTE, GEN_B, SES_B)
    assert _sessions(cache) == before


def test_no_file_in_the_state_directory_holds_a_raw_session_id(cache: Path) -> None:
    payload = {
        "session_id": "synthetic-session-SECRET",
        "cost": {"total_api_duration_ms": 5},
        "rate_limits": _window(30, RESET_1 + 10**9),
    }
    _run(cache, {k: v for k, v in payload.items() if k != "rate_limits"})
    _run(cache, {**payload, "cost": {"total_api_duration_ms": 6}})
    written = b"".join(p.read_bytes() for p in cache.parent.iterdir() if p.is_file())
    assert b"synthetic-session" not in written and b"SECRET" not in written


# -- freshness and fencing still hold across sessions ----------------------------------


def test_a_cross_session_repaint_with_a_new_generation_stays_fresh(cache: Path) -> None:
    windows = _window(30, RESET_1)
    _seed(cache, SES_B, OLD_B, T0 - MINUTE)
    assert hook.persist(cache, windows, T0, GEN_A, SES_A)
    later = T0 + 10 * MINUTE
    assert hook.persist(cache, windows, later, GEN_B, SES_B)  # another session, same numbers
    assert json.loads(cache.read_text())["captured_ms"] == T0
    snapshot = _read(cache, T0 + 20 * MINUTE)
    assert snapshot.observed_ms == later
    assert snapshot.status == LimitStatus.FRESH


def test_a_merged_no_change_still_fences_an_older_different_payload(cache: Path) -> None:
    assert hook.persist(cache, _window(30, RESET_1), T0, GEN_A, SES_A)
    assert hook.persist(cache, _window(25, RESET_1), T0 + 5 * MINUTE, GEN_B, SES_B)  # merged away
    assert not hook.persist(cache, _window(40, RESET_1), T0 + 2 * MINUTE, GEN_C, SES_B)
    assert _windows(cache) == _window(30, RESET_1)


# -- the hook upgrade ------------------------------------------------------------------


def test_the_allow_list_upgrades_the_hook_this_build_replaces() -> None:
    assert PREVIOUS_HOOK_SHA256 == (
        "eece0979aa2ccd21be47121fd2ba2a958a7a2caf8ccf4c8a3b8b24c7995c0c64",
        "f5632268feff5fa6ee8ec65e97049bbbd30f3f42f8000d977267fced84f0e5e7",
        "81833a623b5e14b8b0cf242d87ffdbe3fafb01ceee87e06845bc946fcde4914c",
        "1cf6b549e22c8d0bc1efaf245da1630dbb242ce35c6acc85225b2909eef4b9a6",
        "bc6f77c25219ec3b759df89de39b39960ee3c35b3393b8ad39b3131eb56e59eb",
    )
    assert HOOK_SHA256 not in PREVIOUS_HOOK_SHA256


def test_an_upgrade_from_a_previous_hash_replaces_the_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path.resolve()
    root.chmod(0o700)
    settings = root / "settings.json"
    settings.write_text(json.dumps({"unrelated": True}))
    settings.chmod(0o600)
    cache = root / "state/cache.json"
    hooks = root / "hooks"
    python = root / "tool env/python"
    python.parent.mkdir(mode=0o700)
    python.symlink_to(Path(sys.executable))
    assert (
        install_hook(settings, cache, confirmed=True, hooks_dir=hooks, interpreter=python).state
        == "installed"
    )
    planted = b"# synthetic previous RelayDeck hook\n"
    digest = hashlib.sha256(planted).hexdigest()
    monkeypatch.setattr(hook_install, "PREVIOUS_HOOK_SHA256", (digest, *PREVIOUS_HOOK_SHA256))
    script = hooks / HOOK_FILENAME
    script.write_bytes(planted)
    receipt = hooks / ("limits-" + hashlib.sha256(bytes(settings)).hexdigest() + ".json")
    body = json.loads(receipt.read_text())
    body["hash"] = digest
    receipt.write_text(json.dumps(body))
    assert (
        install_hook(settings, cache, confirmed=True, hooks_dir=hooks, interpreter=python).state
        == "installed"
    )
    assert script.read_bytes() == hook_bytes()
    assert json.loads(receipt.read_text())["hash"] == HOOK_SHA256
    assert json.loads(settings.read_text())["unrelated"] is True
