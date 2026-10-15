"""Cadence, backoff, refresh, expiry, subprocess sandbox.

Fake providers and a fake clock drive LimitsScheduler.run_once directly, so no
timing flakes. Subprocess tests use fixed system binaries (never an agent
CLI) with distinctive arguments.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from relaydeck.usage.contracts import (
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageQuery,
)
from relaydeck.usage.limits_plane.contracts import (
    FetchContext,
    ProviderResult,
    ProviderSpec,
)
from relaydeck.usage.limits_plane.scheduler import (
    BACKOFF_CAP_MS,
    MAX_FETCH_BYTES,
    THROTTLE_FLOOR_MS,
    LimitsScheduler,
    run_bounded_command,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW

HOST = "h1"
KIND = "kind-a"
FORMAT = "fake-limits-stdio-v1"
KEY = f"{HOST}/{KIND}/{FORMAT}"
DAY = "2026-10-11"


class FakeClock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class ScriptedProvider:
    """Replays results in order, then repeats the last one."""

    def __init__(self, results: list[ProviderResult], format_id: str = FORMAT) -> None:
        self.format_id = format_id
        self.results = results
        self.calls: list[FetchContext] = []

    def fetch(self, ctx: FetchContext) -> ProviderResult:
        self.calls.append(ctx)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def window(
    key: str = "primary",
    used: float = 12.5,
    minutes: int | None = 300,
    resets: int | None = None,
) -> LimitWindow:
    return LimitWindow(key, used, minutes, resets, LimitStatus.FRESH)


def snapshot(
    observed: int,
    windows: tuple[LimitWindow, ...] = (),
    quota: str = "quota-a",
    account: str | None = "account-a",
    source: str = "fake",
    kind: str = KIND,
) -> LimitSnapshot:
    return LimitSnapshot(kind, quota, observed, windows, LimitStatus.FRESH, source, account, None)


def ok_result(observed: int, resets: int | None = None, kind: str = KIND) -> ProviderResult:
    return ProviderResult((snapshot(observed, (window(resets=resets),), kind=kind),))


def make_scheduler(
    tmp_path: Path,
    clock: FakeClock,
    results: list[ProviderResult],
    *,
    key: str = KEY,
    kind: str = KIND,
    fmt: str = FORMAT,
    interval_ms: int = 300_000,
    timeout_s: float = 5.0,
) -> tuple[LimitsScheduler, ScriptedProvider, SqliteUsageStore]:
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=clock)
    provider = ScriptedProvider(results)
    spec = ProviderSpec(key, kind, fmt, interval_ms, timeout_s)
    scheduler = LimitsScheduler(
        store, {key: spec}, {key: provider}, clock=clock, jitter=lambda: 0.5
    )
    return scheduler, provider, store


def limits_of(store: SqliteUsageStore) -> tuple[LimitSnapshot, ...]:
    return store.query(UsageQuery(HOST, DAY, DAY), NOW).limits


def test_min_interval_spacing(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    scheduler, provider, store = make_scheduler(tmp_path, clock, [ok_result(NOW)])
    try:
        assert scheduler.run_once(NOW) == 1
        assert scheduler.run_once(NOW + 299_999) == 0
        assert scheduler.run_once(NOW + 300_000) == 1
        assert len(provider.calls) == 2
        assert limits_of(store)[0].observed_ms == NOW
    finally:
        store.close()


def test_backoff_grows_and_caps_at_thirty_minutes(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    failing = ProviderResult((), LimitError.TIMEOUT)
    scheduler, _, store = make_scheduler(tmp_path, clock, [failing])
    try:
        assert BACKOFF_CAP_MS == 30 * 60_000
        now = NOW
        for expected_s in (60, 120, 240, 480, 960, 1800, 1800, 1800):
            assert scheduler.run_once(now) == 1
            assert scheduler.run_once(now + expected_s * 1000 - 1) == 0
            now += expected_s * 1000
        assert scheduler.run_once(now) == 1
        assert limits_of(store) == ()  # failures never write
        assert scheduler.status()[KIND] == {
            "state": "error",
            "checked_ms": now,
            "error": "timeout",
        }
    finally:
        store.close()


def test_success_clears_backoff(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [ProviderResult((), LimitError.OFFLINE), ok_result(NOW + 61_000)]
    )
    try:
        assert scheduler.run_once(NOW) == 1  # fails, backoff 60 s
        assert scheduler.run_once(NOW + 61_000) == 1  # recovers
        assert scheduler.run_once(NOW + 61_000 + 299_999) == 0  # back on min_interval
        assert scheduler.run_once(NOW + 61_000 + 300_000) == 1
        assert scheduler.status()[KIND]["state"] == "ok"
    finally:
        store.close()


def test_request_refresh_honours_the_throttle(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    scheduler, provider, store = make_scheduler(tmp_path, clock, [ok_result(NOW)])
    try:
        assert scheduler.run_once(NOW) == 1
        scheduler.request_refresh(KEY)
        assert scheduler.run_once(NOW + 1_000) == 0  # floor not reached
        assert scheduler.run_once(NOW + THROTTLE_FLOOR_MS + 1) == 1  # due now
        assert len(provider.calls) == 2
        scheduler.request_refresh("h1/kind-a/unknown-format")  # ignored, no crash
        assert scheduler.run_once(NOW + THROTTLE_FLOOR_MS + 2) == 0
    finally:
        store.close()


def test_expired_window_forces_refetch_inside_throttle(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    scheduler, _, store = make_scheduler(tmp_path, clock, [ok_result(NOW, NOW + 120_000)])
    try:
        assert scheduler.run_once(NOW) == 1
        assert scheduler.run_once(NOW + 61_000) == 0  # fresh: min_interval rules
        assert scheduler.run_once(NOW + 121_000) == 1  # reset passed: refetch early
    finally:
        store.close()


def test_expiry_beats_backoff_but_not_the_floor(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    failing = ProviderResult((), LimitError.UNAVAILABLE)
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [ok_result(NOW, NOW + 1_000), failing, failing, failing]
    )
    try:
        assert scheduler.run_once(NOW) == 1  # success with a short window
        assert scheduler.run_once(NOW + 61_000) == 1  # expired: refetch, fails (60 s)
        # Backoff says +120 s, but the expired window forces a retry at the floor.
        assert scheduler.run_once(NOW + 121_000 - 1) == 0
        assert scheduler.run_once(NOW + 121_000) == 1
        assert scheduler.status()[KIND]["error"] == "unavailable"
    finally:
        store.close()


def test_error_with_snapshots_stores_nothing(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    mixed = ProviderResult((snapshot(NOW, (window(),)),), LimitError.PARSE)
    scheduler, _, store = make_scheduler(tmp_path, clock, [mixed])
    try:
        assert scheduler.run_once(NOW) == 1
        assert limits_of(store) == ()
        assert scheduler.status()[KIND] == {
            "state": "error",
            "checked_ms": NOW,
            "error": "parse",
        }
        assert scheduler.run_once(NOW + 1_000) == 0  # backoff armed
    finally:
        store.close()


def test_unexpected_provider_exception_maps_unavailable(tmp_path: Path) -> None:
    class Exploding:
        format_id = FORMAT

        def fetch(self, ctx: FetchContext) -> ProviderResult:
            raise RuntimeError("boom")

    clock = FakeClock(NOW)
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=clock)
    spec = ProviderSpec(KEY, KIND, FORMAT, 300_000, 5.0)
    scheduler = LimitsScheduler(
        store, {KEY: spec}, {KEY: Exploding()}, clock=clock, jitter=lambda: 0.5
    )
    try:
        assert scheduler.run_once(NOW) == 1
        assert scheduler.status()[KIND]["error"] == "unavailable"
        assert limits_of(store) == ()
    finally:
        store.close()


def test_status_aggregates_kinds_and_omits_unchecked(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=clock)
    specs = {
        "h1/kind-a/first-v1": ProviderSpec("h1/kind-a/first-v1", "kind-a", "first-v1", 60_000, 1.0),
        "h1/kind-a/second-v1": ProviderSpec(
            "h1/kind-a/second-v1", "kind-a", "second-v1", 60_000, 1.0
        ),
        "h1/kind-b/third-v1": ProviderSpec("h1/kind-b/third-v1", "kind-b", "third-v1", 60_000, 1.0),
    }
    providers = {
        "h1/kind-a/first-v1": ScriptedProvider([ok_result(NOW)], "first-v1"),
        "h1/kind-a/second-v1": ScriptedProvider(
            [ProviderResult((), LimitError.AUTH_REQUIRED)], "second-v1"
        ),
        "h1/kind-b/third-v1": ScriptedProvider([ok_result(NOW, kind="kind-b")], "third-v1"),
    }
    scheduler = LimitsScheduler(store, specs, providers, clock=clock, jitter=lambda: 0.5)
    try:
        assert scheduler.status() == {}
        assert scheduler.run_once(NOW) == 3
        assert scheduler.status() == {
            "kind-a": {"state": "error", "checked_ms": NOW, "error": "auth_required"},
            "kind-b": {"state": "ok", "checked_ms": NOW, "error": None},
        }
    finally:
        store.close()


def test_wiring_mismatch_rejected(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=clock)
    try:
        spec = ProviderSpec(KEY, KIND, FORMAT, 60_000, 1.0)
        provider = ScriptedProvider([ok_result(NOW)])
        with pytest.raises(ValueError):
            LimitsScheduler(store, {KEY: spec}, {}, clock=clock)
        other = ProviderSpec(KEY, KIND, "other-v1", 60_000, 1.0)
        with pytest.raises(ValueError):
            LimitsScheduler(store, {KEY: other}, {KEY: provider}, clock=clock)
    finally:
        store.close()


def test_start_stop_lifecycle(tmp_path: Path) -> None:
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC")
    provider = ScriptedProvider([ok_result(int(time.time() * 1000))])
    spec = ProviderSpec(KEY, KIND, FORMAT, 60_000, 5.0)
    scheduler = LimitsScheduler(store, {KEY: spec}, {KEY: provider})
    try:
        scheduler.start()
        scheduler.start()  # idempotent
        deadline = time.monotonic() + 5
        while KIND not in scheduler.status() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert KIND in scheduler.status()
        assert len(provider.calls) >= 1
    finally:
        scheduler.stop()
        scheduler.join()
        store.close()
    assert not (scheduler._thread is not None and scheduler._thread.is_alive())


def test_run_bounded_command_success_and_no_shell() -> None:
    assert run_bounded_command(("/bin/echo", "hello"), timeout_s=5) == b"hello\n"
    assert run_bounded_command(("/bin/echo", "a;b $(x) `y`"), timeout_s=5) == b"a;b $(x) `y`\n"


def test_run_bounded_command_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        run_bounded_command("echo hi", timeout_s=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        run_bounded_command(("relative/echo",), timeout_s=5)
    with pytest.raises(ValueError):
        run_bounded_command((), timeout_s=5)
    with pytest.raises(ValueError):
        run_bounded_command(("/bin/echo",), timeout_s=0)
    with pytest.raises(ValueError):
        run_bounded_command(("/bin/echo",), timeout_s=5, extra_env={"bad=key": "x"})


def test_run_bounded_command_minimal_env_and_stderr_dropped() -> None:
    out = run_bounded_command(("/usr/bin/env",), timeout_s=5, extra_env={"EXTRA_ENV_PROBE": "yes"})
    seen = dict(line.split("=", 1) for line in out.decode().splitlines() if "=" in line)
    assert seen["EXTRA_ENV_PROBE"] == "yes"
    assert set(seen) <= {"HOME", "PATH", "LANG", "EXTRA_ENV_PROBE"}
    err = run_bounded_command(
        (sys.executable, "-c", "import sys; sys.stderr.write('noise'); print('out')"),
        timeout_s=10,
    )
    assert err == b"out\n"


def test_run_bounded_command_truncates_stdout() -> None:
    out = run_bounded_command(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 2097152)"),
        timeout_s=10,
    )
    assert len(out) == MAX_FETCH_BYTES


def test_timeout_kills_the_child() -> None:
    before = subprocess.run(["pgrep", "-f", "sleep 31"], capture_output=True, text=True).stdout
    assert before == ""
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_bounded_command(("/bin/sleep", "31"), timeout_s=0.2)
    assert time.monotonic() - started < 10
    after = subprocess.run(["pgrep", "-f", "sleep 31"], capture_output=True, text=True).stdout
    assert after == ""  # no orphan: the group kill reaped it


def test_fetch_timeout_recorded_as_error(tmp_path: Path) -> None:
    class SlowProvider:
        format_id = FORMAT

        def fetch(self, ctx: FetchContext) -> ProviderResult:
            ctx.run(("/bin/sleep", "30"), 0.2, None)
            raise AssertionError("unreachable")

    clock = FakeClock(NOW)
    store = SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=clock)
    spec = ProviderSpec(KEY, KIND, FORMAT, 300_000, 5.0)
    scheduler = LimitsScheduler(
        store, {KEY: spec}, {KEY: SlowProvider()}, clock=clock, jitter=lambda: 0.5
    )
    try:
        assert scheduler.run_once(NOW) == 1
        assert scheduler.status()[KIND]["error"] == "timeout"
        assert limits_of(store) == ()
    finally:
        store.close()


# Last-good is reused only for a positively identified account ---------------


def _shown(store: SqliteUsageStore) -> tuple[LimitSnapshot, ...]:
    return limits_of(store)


def test_an_error_after_success_with_an_unknown_identity_shows_no_last_good(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    good = ProviderResult((snapshot(NOW, (window(used=62.0),), account=None),))
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [good, ProviderResult(error=LimitError.TIMEOUT)]
    )
    try:
        scheduler.run_once(NOW)
        assert _shown(store)[0].windows[0].used_percent == 62.0
        clock.now = NOW + 400_000
        scheduler.run_once(clock.now)
        reading = _shown(store)[0]
        assert reading.windows == ()  # the old percentage is gone
        assert reading.error == "timeout"
        assert reading.status == LimitStatus.NOT_REPORTED
    finally:
        store.close()


def test_an_account_switch_never_shows_the_old_percentage(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    first = ProviderResult((snapshot(NOW, (window(used=88.0),), account="account-a"),))
    second = ProviderResult((snapshot(NOW + 400_000, (window(used=7.0),), account="account-b"),))
    scheduler, _, store = make_scheduler(tmp_path, clock, [first, second])
    try:
        scheduler.run_once(NOW)
        clock.now = NOW + 400_000
        scheduler.run_once(clock.now)
        shown = _shown(store)
        assert [s.account_ref for s in shown] == ["account-b"]
        assert [w.used_percent for s in shown for w in s.windows] == [7.0]
    finally:
        store.close()


def test_the_same_account_with_a_transient_error_keeps_last_good_aged(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    good = ProviderResult((snapshot(NOW, (window(used=40.0),), account="account-a"),))
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [good, ProviderResult(error=LimitError.OFFLINE)]
    )
    try:
        scheduler.run_once(NOW)
        clock.now = NOW + 400_000
        scheduler.run_once(clock.now)
        reading = _shown(store)[0]
        assert reading.account_ref == "account-a"
        assert reading.windows[0].used_percent == 40.0
        assert reading.observed_ms == NOW  # still the old observation: it ages
    finally:
        store.close()


def test_an_auth_failure_suppresses_even_an_identified_account(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    good = ProviderResult((snapshot(NOW, (window(used=40.0),), account="account-a"),))
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [good, ProviderResult(error=LimitError.AUTH_REQUIRED)]
    )
    try:
        scheduler.run_once(NOW)
        clock.now = NOW + 400_000
        scheduler.run_once(clock.now)
        reading = _shown(store)[0]
        assert reading.windows == ()
        assert reading.error == "auth_required"
    finally:
        store.close()


def test_the_status_line_source_is_not_touched_by_a_plane_failure(tmp_path: Path) -> None:
    """A status-line reading has no account and source "": a provider block's failure never
    removes it, and it is still returned with its own freshness."""
    clock = FakeClock(NOW)
    scheduler, _, store = make_scheduler(
        tmp_path, clock, [ProviderResult(error=LimitError.TIMEOUT)]
    )
    try:
        hook = LimitSnapshot(KIND, "hook-quota", NOW, (window(used=33.0),), LimitStatus.FRESH)
        from tests.support.usage.store import event, mapping, write

        write(store, (event(),), (mapping(),), (hook,))
        scheduler.run_once(NOW)
        found = [s for s in _shown(store) if s.source == ""]
        assert len(found) == 1 and found[0].windows[0].used_percent == 33.0
    finally:
        store.close()
