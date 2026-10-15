"""unconfigured sources retire instead of lingering.

A source removed from config (or moved to another format) keeps its history
but leaves coverage reporting and kind aggregation. Synthetic store only."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from relaydeck.companion.config import CompanionConfig, UsageConfig, UsageSourceConfig
from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.usage.contracts import (
    Basis,
    Batch,
    Counters,
    Coverage,
    KindCoverage,
    LimitSnapshot,
    LimitStatus,
    UsageEvent,
    UsageQuery,
    UsageReport,
)
from relaydeck.usage.store import SqliteUsageStore

HOST = "h1"
ZONE = "UTC"
NOW = int(datetime(2026, 10, 11, 12, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000)
OLD = f"{HOST}/kind-a/format-old"
NEW = f"{HOST}/kind-a/format-new"
LIMITS_FORMAT = "limits-fmt"
DAY = "2026-10-11"


def _store(tmp_path: Path) -> SqliteUsageStore:
    return SqliteUsageStore(tmp_path / "state" / "usage.db", ZONE, clock=lambda: NOW)


def _apply(
    store: SqliteUsageStore,
    source: str,
    events: tuple[UsageEvent, ...] = (),
    coverage: Coverage = Coverage.COMPLETE,
) -> None:
    cursor = store.cursor(source)
    store.apply(
        "kind-a",
        Batch(events, cursor, coverage, cursor.revision, ()),
        (),
        source_key=source,
    )


def _event(session: str = "s1", key: str = "e1") -> UsageEvent:
    return UsageEvent(session, key, NOW, "model-a", Counters(10, output=2), Basis.PER_CALL)


def _cov(report: UsageReport, kind: str) -> KindCoverage | None:
    found = [c for c in report.coverage if c.kind == kind]
    assert len(found) <= 1
    return found[0] if found else None


def _flags(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    try:
        return {
            "coverage": db.execute(
                "SELECT retired FROM coverage WHERE source=?", (OLD,)
            ).fetchone()[0],
            "cursors": db.execute("SELECT retired FROM cursors WHERE source=?", (OLD,)).fetchone()[
                0
            ],
        }
    finally:
        db.close()


def test_format_switch_hides_the_old_unavailable_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, OLD, (), Coverage.UNAVAILABLE)
    _apply(store, NEW, (_event(),), Coverage.COMPLETE)
    query = UsageQuery(HOST, DAY, DAY)
    assert _cov(store.query(query, NOW), "kind-a").coverage == Coverage.UNAVAILABLE
    flipped = store.retire_missing_sources(HOST, [NEW], [])
    assert flipped == 2  # the old coverage row and its cursor row
    assert _flags(tmp_path / "state" / "usage.db") == {"coverage": 1, "cursors": 1}
    assert _cov(store.query(query, NOW), "kind-a").coverage == Coverage.COMPLETE


def test_removed_source_keeps_events_but_loses_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, OLD, (_event(),), Coverage.UNAVAILABLE)
    assert store.retire_missing_sources(HOST, [NEW], []) == 2
    report = store.query(UsageQuery(HOST, DAY, DAY), NOW)
    assert [row.counters.total for row in report.rows] == [12]  # history stays
    # The retired UNAVAILABLE row no longer drags the kind down; the entry now
    # reflects only the stored (verified) events.
    assert _cov(report, "kind-a").coverage == Coverage.COMPLETE
    assert _cov(report, "kind-a").observed_ms is None


def test_readding_resumes_cleanly_from_the_kept_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, OLD, (_event(),), Coverage.COMPLETE)
    before = store.cursor(OLD)
    assert before.revision == 1
    store.retire_missing_sources(HOST, [NEW], [])
    assert store.retire_missing_sources(HOST, [OLD, NEW], []) == 2  # un-retired
    assert _flags(tmp_path / "state" / "usage.db") == {"coverage": 0, "cursors": 0}
    resumed = store.cursor(OLD)
    assert resumed.revision == 1  # the cursor survived retirement
    _apply(store, OLD, (_event("s1", "e2"),), Coverage.COMPLETE)
    assert store.cursor(OLD).revision == 2


def test_old_limits_rows_are_excluded_then_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_limits(
        "kind-a",
        f"{HOST}/kind-a/{LIMITS_FORMAT}",
        (LimitSnapshot("kind-a", "q", NOW, (), LimitStatus.FRESH, LIMITS_FORMAT, None, None),),
        NOW,
    )
    assert len(store.query(UsageQuery(HOST, DAY, DAY), NOW).limits) == 1
    store.retire_missing_sources(HOST, [NEW], [])
    assert store.query(UsageQuery(HOST, DAY, DAY), NOW).limits == ()
    store.retire_missing_sources(HOST, [NEW], [f"{HOST}/kind-a/{LIMITS_FORMAT}"])
    assert len(store.query(UsageQuery(HOST, DAY, DAY), NOW).limits) == 1


def test_other_hosts_rows_are_never_touched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, OLD, (_event(),), Coverage.UNAVAILABLE)
    foreign = "h2/kind-a/format-old"
    cursor = store.cursor(foreign)
    store.apply(
        "kind-a",
        Batch((), cursor, Coverage.UNAVAILABLE, cursor.revision, ()),
        (),
        source_key=foreign,
    )
    store.retire_missing_sources(HOST, [NEW], [])
    db = sqlite3.connect(tmp_path / "state" / "usage.db")
    try:
        assert db.execute("SELECT retired FROM coverage WHERE source=?", (OLD,)).fetchone()[0] == 1
        assert (
            db.execute("SELECT retired FROM coverage WHERE source=?", (foreign,)).fetchone()[0] == 0
        )
    finally:
        db.close()


def test_runtime_prepare_usage_retires_on_startup(tmp_path: Path) -> None:
    state = tmp_path / "state"
    store = SqliteUsageStore(state / "usage.db", ZONE, clock=lambda: NOW)
    _apply(store, OLD, (), Coverage.UNAVAILABLE)
    _apply(store, NEW, (_event(),), Coverage.COMPLETE)
    store.close()
    roots = tmp_path / "roots"
    (roots / "new").mkdir(parents=True)
    config = CompanionConfig(
        "http://127.0.0.1/",
        tmp_path / "cookie.secret",
        tmp_path / "cursor.secret",
        state,
        tmp_path / "connector.json",
        str(tmp_path / "herdr.sock"),
        host_id=HOST,
        usage=UsageConfig(
            "UTC",
            (UsageSourceConfig("kind-a", "format-new", roots / "new", "Kind A", None, None, None),),
            (),
            90,
            (),
        ),
    )
    runtime = CompanionRuntime(config)
    runtime.gateway = SimpleNamespace(now_ms=lambda: NOW, usage=None)
    runtime._prepare_usage()
    try:
        assert runtime.usage_store is not None
        report = runtime.usage_store.query(UsageQuery(HOST, DAY, DAY), NOW)
        assert _cov(report, "kind-a").coverage == Coverage.COMPLETE
    finally:
        if runtime.usage_store is not None:
            runtime.usage_store.close()
