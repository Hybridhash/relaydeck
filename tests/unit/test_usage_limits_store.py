"""Apply_limits replacement semantics and ledger isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from relaydeck.usage.contracts import (
    LimitError,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    UsageQuery,
    UsageUnavailable,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, SOURCE, event, mapping, write

HOST = "h1"
KIND = "kind-a"
KEY = f"{HOST}/{KIND}/fake-limits-stdio-v1"
DAY = "2026-10-11"


def window(key: str = "primary", used: float = 10.0) -> LimitWindow:
    return LimitWindow(key, used, 300, None, LimitStatus.FRESH)


def snapshot(
    quota: str,
    observed: int,
    windows: tuple[LimitWindow, ...] | None = None,
    account: str | None = "account-a",
    source: str = "fake",
    error: str | None = None,
) -> LimitSnapshot:
    panes = (window(),) if windows is None else windows
    return LimitSnapshot(KIND, quota, observed, panes, LimitStatus.FRESH, source, account, error)


def limits_of(store: SqliteUsageStore) -> tuple[LimitSnapshot, ...]:
    return store.query(UsageQuery(HOST, DAY, DAY), NOW).limits


def test_replacement_removes_absent_windows(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        first = LimitSnapshot(
            KIND,
            "quota-a",
            NOW,
            (window("one"), window("two")),
            LimitStatus.FRESH,
            "",
            "account-a",
            None,
        )
        store.apply_limits(KIND, KEY, (first,), NOW)
        assert [w.window_key for w in limits_of(store)[0].windows] == ["one", "two"]
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 1, (window("one"),)),), NOW + 1)
        remaining = limits_of(store)
        assert len(remaining) == 1
        assert [w.window_key for w in remaining[0].windows] == ["one"]
        assert remaining[0].observed_ms == NOW + 1


def test_empty_reading_replaces_the_old_set(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW),), NOW)
        assert len(limits_of(store)[0].windows) == 1
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 1, ()),), NOW + 1)
        remaining = limits_of(store)
        assert len(remaining) == 1
        assert remaining[0].windows == ()
        assert remaining[0].status == LimitStatus.NOT_REPORTED


def test_no_snapshots_writes_nothing(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW),), NOW)
        store.apply_limits(KIND, KEY, (), NOW + 1)
        assert len(limits_of(store)) == 1


def _stored_accounts(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {row[0] for row in db.execute("SELECT DISTINCT account FROM limits")}
    finally:
        db.close()


def test_last_good_kept_only_for_the_same_account(tmp_path: Path) -> None:
    """The display shows only the active account (the newest reading's). A switched
    account hides the previous one's reading; its rows stay stored, apart."""
    path = tmp_path / "usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, account="account-a"),), NOW)
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 1, account="account-b"),), NOW + 1)
        shown = limits_of(store)
        assert {s.account_ref for s in shown} == {"account-b"}
        assert _stored_accounts(path) == {"account-a", "account-b"}  # history is kept
        # A new reading for B replaces B; A is still not shown as current.
        store.apply_limits(
            KIND, KEY, (snapshot("quota-a", NOW + 2, (window("solo"),), "account-b"),), NOW + 2
        )
        by_account = {s.account_ref: s for s in limits_of(store)}
        assert set(by_account) == {"account-b"}
        assert [w.window_key for w in by_account["account-b"].windows] == ["solo"]
        # Going back to A makes A active again, with its own newest reading.
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 3, account="account-a"),), NOW + 3)
        assert {s.account_ref for s in limits_of(store)} == {"account-a"}


def test_failure_row_keeps_identified_windows(tmp_path: Path) -> None:
    """A transient failure recorded after a good reading leaves the good
    windows current (aged), never replaced by the windowless error."""
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, account="account-a"),), NOW)
        suppressed = store.record_limits_failure(
            KIND, KEY, LimitError.UNAVAILABLE, NOW + 10, sources=("fake",)
        )
        assert suppressed == 0
        shown = limits_of(store)
        assert len(shown) == 1
        assert shown[0].account_ref == "account-a"
        assert [w.window_key for w in shown[0].windows] == ["primary"]


def test_a_failure_leaves_a_historical_unknown_reading_historical(tmp_path: Path) -> None:
    """A transient failure must not refresh an older identityless reading into the newest row,
    which would hide the active named reading."""
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW - 10, account=None),), NOW - 10)
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, account="account-a"),), NOW)
        suppressed = store.record_limits_failure(
            KIND, KEY, LimitError.UNAVAILABLE, NOW + 10, sources=("fake",)
        )
        assert suppressed == 0
        shown = limits_of(store)
        assert [s.account_ref for s in shown] == ["account-a"]
        assert [w.window_key for w in shown[0].windows] == ["primary"]


def test_expired_window_shows_as_reset_after_a_failure(tmp_path: Path) -> None:
    """The kept window whose reset has passed reads as ended, never the
    stale percentage, even when a newer failure arrives."""
    ended = LimitWindow("primary", 99.0, 300, NOW - 1, LimitStatus.FRESH)
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, (ended,), "account-a"),), NOW)
        store.record_limits_failure(KIND, KEY, LimitError.UNAVAILABLE, NOW + 10, sources=("fake",))
        shown = limits_of(store)
        assert len(shown) == 1
        assert shown[0].account_ref == "account-a"
        assert shown[0].windows[0].status == LimitStatus.EXPIRED


def test_unknown_account_never_touches_named_accounts(tmp_path: Path) -> None:
    """An unknown identity is its own account: it never overwrites a named account's rows, and it
    never leaves the older named account's percentage on screen as current."""
    path = tmp_path / "usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, account="account-a"),), NOW)
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 1, None, None),), NOW + 1)
        assert {s.account_ref for s in limits_of(store)} == {None}
        assert _stored_accounts(path) == {"account-a", ""}


def test_legacy_and_plane_rows_coexist(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(),), (mapping(),), (LimitSnapshot(KIND, "quota-a", NOW),))
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW, account="account-a"),), NOW)
        found = limits_of(store)
        assert {s.account_ref for s in found} == {None, "account-a"}


def test_apply_limits_leaves_the_ledger_untouched(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(),))
        assert store.cursor(SOURCE).revision == 1
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW),), NOW)
        assert store.cursor(SOURCE).revision == 1
        assert store.query(UsageQuery(HOST, DAY, DAY), NOW).rows[0].counters.total == 12


def test_source_and_error_round_trip(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(
            KIND,
            KEY,
            (snapshot("quota-a", NOW, source="fake", error="offline"),),
            NOW,
        )
        found = limits_of(store)[0]
        assert found.source == "fake"
        assert found.error == "offline"


def test_apply_limits_rejects_bad_readings_atomically(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW),), NOW)
        bad_percent = LimitSnapshot(KIND, "quota-b", NOW, (window("x", 101.0),))
        good = snapshot("quota-a", NOW + 1, (window("solo"),))
        with pytest.raises(UsageUnavailable):
            store.apply_limits(KIND, KEY, (good, bad_percent), NOW + 1)
        # The whole reading failed: quota-a still has its old window.
        assert [w.window_key for w in limits_of(store)[0].windows] == ["primary"]
        with pytest.raises(UsageUnavailable):
            store.apply_limits(KIND, "broken-key", (good,), NOW + 1)
        with pytest.raises(UsageUnavailable):
            store.apply_limits("kind-b", KEY, (good,), NOW + 1)
        with pytest.raises(UsageUnavailable):
            store.apply_limits(KIND, KEY, [good], NOW + 1)  # type: ignore[list-item]
        with pytest.raises(UsageUnavailable):
            store.apply_limits(KIND, KEY, (snapshot("quota-c", NOW, error="bogus"),), NOW)
        with pytest.raises(UsageUnavailable):
            store.apply_limits(KIND, KEY, (snapshot("quota-c", NOW),), -1)


def test_ancient_readings_are_skipped(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW - 200 * 86400_000),), NOW)
        assert limits_of(store) == ()


def test_migration_keeps_four_part_rows(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    db = sqlite3.connect(path)
    try:
        db.execute("""CREATE TABLE limits(host TEXT,kind TEXT,quota TEXT,window TEXT,
            observed INTEGER,body TEXT,status TEXT,PRIMARY KEY(host,kind,quota,window))""")
        db.execute(
            "INSERT INTO limits VALUES(?,?,?,?,?,?,?)",
            ("h1", "kind-a", "quota-a", "primary", NOW, None, "fresh"),
        )
        db.commit()
    finally:
        db.close()
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        found = limits_of(store)
        assert len(found) == 1
        assert found[0].account_ref is None
        assert found[0].source == ""
        assert found[0].error is None
        store.apply_limits(KIND, KEY, (snapshot("quota-a", NOW + 1),), NOW + 1)
        assert len(limits_of(store)) == 2  # legacy '' row plus the plane row
