import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from relaydeck.usage.contracts import (
    MAX_CURSOR_BYTES,
    MAX_FILE_CONTEXT_BYTES,
    MAX_QUERY_ROWS,
    ApplyResult,
    Batch,
    Counters,
    Coverage,
    Cursor,
    FileCursor,
    LimitSnapshot,
    LimitStatus,
    LimitWindow,
    RowScope,
    UsageQuery,
    UsageUnavailable,
    encoded_cursor_bytes,
)
from relaydeck.usage.pricing import load_price_table
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, SOURCE, event, mapping, report, write


def test_atomic_cas_retry_replacement_absence_restart(tmp_path: Path) -> None:
    path = tmp_path / "state/usage.db"
    store = SqliteUsageStore(path, "UTC", clock=lambda: NOW)
    batch = write(store, (event(),))
    store.apply("kind-a", batch, (mapping(),), source_key=SOURCE)
    assert store.cursor(SOURCE).revision == 1
    with pytest.raises(UsageUnavailable, match="stale_batch"):
        store.apply(
            "kind-a",
            replace(batch, events=(event("changed"),)),
            (mapping("w2"),),
            source_key=SOURCE,
        )
    assert len(report(store).rows) == 2
    write(store, (event(t=NOW - 86400_000, model="model-b"),))
    write(store, ())
    rows = report(store, "2026-10-10").rows
    assert len(rows) == 2 and all(r.day == "2026-10-10" and r.model == "model-b" for r in rows)
    assert all(r.counters.total == 12 for r in rows)
    # Even after newer commits, an exact earlier retry cannot replay old values.
    store.apply("kind-a", batch, (mapping(),), source_key=SOURCE)
    assert store.cursor(SOURCE).revision == 3
    assert report(store, "2026-10-10").rows == rows
    store.close()
    store = SqliteUsageStore(path, "UTC", clock=lambda: NOW)
    assert store.cursor(SOURCE).revision == 3
    assert report(store, "2026-10-10").rows == rows
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert store._db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    for suffix in ("-wal", "-shm"):
        assert Path(str(path) + suffix).stat().st_mode & 0o777 == 0o600
    store.close()


def test_episodes_ties_gaps_and_exclusive_session_count(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        maps = (
            mapping("w1", "a", NOW + 100, NOW + 200),
            mapping("w2", "b", NOW + 300, NOW + 400),
            mapping("w1", "c", NOW + 500, NOW + 600),
        )
        write(
            store,
            tuple(event(str(i), t=NOW + t) for i, t in enumerate([0, 150, 250, 350, 550, 700])),
            maps,
        )
        rows = {r.workspace_id: r for r in report(store).rows}
        assert rows["w1"].counters.total == 60 and rows["w2"].counters.total == 12
        assert rows[None].counters.total == 72 and rows[None].sessions == 1
        ties = (
            mapping("w1", "t-z", NOW, NOW + 800),
            mapping("w2", "t-b", NOW + 20, NOW + 900),
            mapping("w1", "t-a", NOW + 20, NOW + 900),
        )
        write(store, (event("overlap", t=NOW + 750),), ties)
        rows = {r.workspace_id: r for r in report(store).rows}
        # every event the w1+w2 ties both contain is ambiguous (only t=0 is w1-only).
        assert rows["w1"].counters.total == 12 and rows[None].counters.total == 12
        assert (
            next(r for r in report(store).rows if r.scope == RowScope.NOT_LINKED).counters.total
            == 72
        )
        assert all(r.workspace_id in (None, "w1") for r in report(store, space_id="w1").rows)


def test_coverage_unmapped_and_unverified_are_not_confirmed_totals(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(), event("u", session="unmapped"), event("x", verified=False)))
        value = report(store)
        assert all(row.counters.total == 12 for row in value.rows)
        assert value.coverage[0].coverage == Coverage.UNVERIFIED
        assert value.coverage[0].unmapped_sessions == 1


def test_dst_local_day_and_horizon_no_resurrection(tmp_path: Path) -> None:
    zone = ZoneInfo("America/New_York")
    now = int(datetime(2026, 11, 2, tzinfo=zone).timestamp() * 1000)
    times = [
        int(datetime(2026, 10, 31, 23, 59, tzinfo=zone).timestamp() * 1000),
        int(datetime(2026, 11, 1, tzinfo=zone).timestamp() * 1000),
    ] + [
        int(datetime(2026, 11, 1, 1, 30, fold=fold, tzinfo=zone).timestamp() * 1000)
        for fold in (0, 1)
    ]
    with SqliteUsageStore(tmp_path / "usage.db", "America/New_York", clock=lambda: now) as store:
        write(
            store,
            tuple(event(str(i), t=t) for i, t in enumerate(times)),
            (mapping(first=times[0], last=times[1]),),
        )
        rows = store.query(UsageQuery("h1", "2026-11-01", "2026-11-01"), now).rows
        assert len(rows) == 2 and all(row.counters.total == 36 for row in rows)
        assert all(
            row.counters.total == 12
            for row in store.query(UsageQuery("h1", "2026-10-31", "2026-10-31"), now).rows
        )
        old = event("old", t=now - 100 * 86400_000)
        write(store, (old,), ())
        assert not store.query(UsageQuery("h1", "2026-07-25", "2026-07-25"), now).rows
        store.prune(now + 100 * 86400_000)
        write(store, (old, event(t=times[0])), ())
        assert not store.query(UsageQuery("h1", "2026-11-01", "2026-11-01"), now).rows


def test_limits_latest_per_window_expiry_and_staleness(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        windows = (
            LimitWindow("short", 75, resets_at_ms=NOW - 1),
            LimitWindow("long", 30, resets_at_ms=NOW + 1000),
        )
        write(store, (), limits=(LimitSnapshot("kind-a", "q", NOW, windows, LimitStatus.EXPIRED),))
        write(
            store,
            (),
            limits=(LimitSnapshot("kind-a", "q", NOW - 1000, (LimitWindow("long", 90),)),),
        )
        limits = report(store).limits[0]
        assert limits.status == LimitStatus.EXPIRED
        assert {w.window_key: (w.used_percent, w.status) for w in limits.windows} == {
            "short": (75, LimitStatus.EXPIRED),
            "long": (30, LimitStatus.FRESH),
        }
        write(
            store,
            (),
            limits=(LimitSnapshot("kind-a", "q2", NOW - 16 * 60_000, (LimitWindow("x", 40),)),),
        )
        # Age alone is not staleness: the client judges it against the tool's own activity.
        assert (
            next(s for s in report(store).limits if s.quota_key == "q2").status == LimitStatus.FRESH
        )


def test_kind_activity_is_the_latest_event_per_kind(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        assert store.kind_activity("h1") == {}
        write(store, (event("a"), event("b")))
        activity = store.kind_activity("h1")
        assert activity == {"kind-a": NOW}
        assert store.kind_activity("other-host") == {}


def test_cost_unknown_bucket_and_model_poison_only_its_row(tmp_path: Path) -> None:
    prices = load_price_table(Path(__file__).parents[1] / "fixtures/usage/prices.toml")
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW, prices=prices) as store:
        write(store, (event(),))
        assert all(row.cost_micro_usd == 42 for row in report(store).rows)
        write(store, (replace(event("cache"), counters=Counters(cache_read=1)),))
        assert all(row.cost_micro_usd is None for row in report(store).rows)
        assert report(store).price_table_date == "2026-10-01"


def test_query_validation_and_keyset_paging(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, tuple(event(str(i), model=f"model-{i}") for i in range(600)))
        query = UsageQuery("h1", "2026-10-11", "2026-10-11")
        rows = []
        while True:
            page = store.query(query, NOW)
            assert len(page.rows) <= MAX_QUERY_ROWS
            rows.extend(page.rows)
            if not page.next_cursor:
                break
            query = replace(query, cursor=page.next_cursor)
        assert len(rows) == 1200 and len({(r.workspace_id, r.model) for r in rows}) == 1200
        for invalid in [
            UsageQuery("h1", "20261011", "2026-10-11"),
            UsageQuery("h1", "2026-10-12", "2026-10-11"),
            UsageQuery("h1", "2026-01-01", "2026-10-11"),
            replace(query, cursor="private-marker"),
        ]:
            with pytest.raises(UsageUnavailable, match="^invalid_query$"):
                store.query(invalid, NOW)


def test_bounded_prune_and_symlink_refusal(tmp_path: Path) -> None:
    now = [NOW]
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        for index in range(2):
            write(store, tuple(event(f"{index}-{i}") for i in range(3000)))
        now[0] += 100 * 86400_000
        first = store.prune(now[0])
        assert 0 < first <= 5000
        assert 0 < store.prune(now[0]) <= 5000
        assert not store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), now[0]).rows
    link = tmp_path / "link.db"
    link.symlink_to(tmp_path / "state/usage.db")
    with pytest.raises(UsageUnavailable, match="^unavailable$"):
        SqliteUsageStore(link, "UTC")


def test_failed_batch_rolls_back_all_records_and_cursor(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        invalid = Batch((event(), event("")), store.cursor(SOURCE), Coverage.COMPLETE, 0)
        with pytest.raises(UsageUnavailable):
            store.apply("kind-a", invalid, (mapping(),), source_key=SOURCE)
        assert store.cursor(SOURCE).revision == 0
        assert not report(store).rows and not report(store).coverage
        assert all(
            store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            for table in ("events", "receipts", "mappings", "limits")
        )
        write(store, (event(),))
        assert store.cursor(SOURCE).revision == 1


def test_mapping_extension_and_exact_retention_boundary(tmp_path: Path) -> None:
    earliest = datetime.fromtimestamp(NOW / 1000, ZoneInfo("UTC")) - timedelta(days=89)
    cutoff = int(earliest.timestamp() * 1000)
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event("in", t=cutoff), event("out", t=cutoff - 1)))
        rows = store.query(UsageQuery("h1", earliest.date().isoformat(), "2026-10-11"), NOW).rows
        assert len(rows) == 2 and all(row.counters.total == 12 for row in rows)
        write(store, (), (mapping(last=NOW + 9000),))
        write(store, (), (mapping(last=NOW + 2000),))
        assert store._db.execute("SELECT last FROM mappings").fetchone()[0] == NOW + 9000
        before = store.cursor(SOURCE)
        with pytest.raises(UsageUnavailable):
            write(store, (event("rolled-back"),), (mapping("w2"),))
        assert store.cursor(SOURCE) == before


def test_cursor_bounds_and_content_free_path_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from relaydeck.usage.contracts import MAX_CURSOR_STATE_BYTES, MAX_CURSOR_STATE_ENTRIES

    path = tmp_path / "state/usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        for state in [
            {str(i): "1" for i in range(MAX_CURSOR_STATE_ENTRIES + 1)},
            {"x": "x" * (MAX_CURSOR_STATE_BYTES + 1)},
        ]:
            batch = Batch((), Cursor(state=state), Coverage.COMPLETE, 0)
            with pytest.raises(UsageUnavailable):
                store.apply("kind-a", batch, (), source_key=SOURCE)
            assert store.cursor(SOURCE).revision == 0
        for file in (FileCursor(1, 2, 1, 2, NOW, "invalid"), FileCursor(1, 2, 3, 2, NOW, "0" * 64)):
            with pytest.raises(UsageUnavailable, match="^unavailable$"):
                store.apply(
                    "kind-a",
                    Batch((), Cursor({"f1": file}), Coverage.COMPLETE, 0),
                    (),
                    source_key=SOURCE,
                )
            assert store.cursor(SOURCE).revision == 0
    original = Path.lstat

    def foreign(p: Path) -> os.stat_result:
        result = original(p)
        if p == path:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", foreign)
    with pytest.raises(UsageUnavailable, match="^unavailable$"):
        SqliteUsageStore(path, "UTC")
    monkeypatch.setattr(Path, "lstat", original)
    alias = tmp_path / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(UsageUnavailable, match="^unavailable$"):
        SqliteUsageStore(alias / "new.db", "UTC")


def test_host_isolation_and_bound_cursor(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, tuple(event(str(i), model=f"model-{i}") for i in range(300)))
        other_source = "h2/kind-a/format-a"
        other_mapping = replace(mapping(), source_key=other_source, host_id="h2")
        store.apply(
            "kind-a",
            Batch((event(),), Cursor(), Coverage.COMPLETE, 0),
            (other_mapping,),
            source_key=other_source,
        )
        assert len(store.query(UsageQuery("h2", "2026-10-11", "2026-10-11"), NOW).rows) == 2
        page = report(store)
        assert page.next_cursor
        with pytest.raises(UsageUnavailable, match="^invalid_query$"):
            store.query(UsageQuery("h2", "2026-10-11", "2026-10-11", cursor=page.next_cursor), NOW)


def test_keyed_correction_crossing_retention_is_atomic(tmp_path: Path) -> None:
    cutoff = NOW - 89 * 86400_000
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(t=cutoff),))
        assert all(row.counters.total == 12 for row in report(store, "2026-07-14").rows)
        cursor = store.cursor(SOURCE)
        correction = Batch((event(t=cutoff - 1),), cursor, Coverage.COMPLETE, cursor.revision)
        with pytest.raises(UsageUnavailable, match="^unavailable$"):
            store.apply("kind-a", correction, (mapping("changed"),), source_key=SOURCE)
        assert store.cursor(SOURCE) == cursor
        assert report(store, "2026-07-14").rows
        store.apply("kind-a", correction, (mapping(),), source_key=SOURCE)
        assert not report(store, "2026-07-14").rows
        assert store._db.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        assert store.cursor(SOURCE).revision == cursor.revision + 1
        store.apply("kind-a", correction, (mapping(),), source_key=SOURCE)
        assert store.cursor(SOURCE).revision == cursor.revision + 1


def test_limits_equal_quota_ids_are_host_isolated_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        for host, observed, used in (("h1", NOW, 25), ("h2", NOW + 1, 70), ("h1", NOW + 2, 30)):
            source = f"{host}/kind-a/format-a"
            cursor = store.cursor(source)
            snapshot = LimitSnapshot(
                "kind-a", "same-quota", observed, (LimitWindow("same-window", used),)
            )
            store.apply(
                "kind-a",
                Batch((), cursor, Coverage.COMPLETE, cursor.revision, (snapshot,)),
                (),
                source_key=source,
            )
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        for host, used in (("h1", 30), ("h2", 70)):
            limits = store.query(UsageQuery(host, "2026-10-11", "2026-10-11"), NOW + 2).limits
            assert len(limits) == 1
            assert limits[0].quota_key == "same-quota"
            assert limits[0].windows[0].used_percent == used


def test_parent_session_round_trip_does_not_infer_attribution(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        write(
            store,
            (
                event(session="parent"),
                replace(event("child", session="child"), parent_session_key="parent"),
            ),
            (mapping(session="parent"),),
        )
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store._db.execute(
            "SELECT session,parent_session_key FROM events ORDER BY session"
        ).fetchall() == [("child", "parent"), ("parent", None)]
        value = report(store)
        assert all(row.counters.total == 12 for row in value.rows)
        assert value.coverage[0].unmapped_sessions == 1
        write(
            store, (replace(event("child", session="child"), parent_session_key="new-parent"),), ()
        )
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store._db.execute(
            "SELECT parent_session_key FROM events WHERE session='child'"
        ).fetchone() == ("new-parent",)


def test_existing_usage_schema_migrates_without_losing_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    # Actual pre- table shapes, including the old host-less limits key.
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE events(host TEXT,kind TEXT,source TEXT,session TEXT,event TEXT,
              occurred INTEGER,day TEXT,model TEXT,counters TEXT,verified INTEGER,
              PRIMARY KEY(host,kind,source,session,event));
            CREATE TABLE limits(kind TEXT,quota TEXT,window TEXT,host TEXT,observed INTEGER,
              body TEXT,status TEXT,PRIMARY KEY(kind,quota,window));
            CREATE TABLE cursors(source TEXT PRIMARY KEY,body TEXT NOT NULL,
              revision INTEGER NOT NULL,updated INTEGER NOT NULL);
            INSERT INTO limits VALUES('kind-a','same-quota','', 'h1',0,NULL,'not_reported');
        """)
        db.execute("UPDATE limits SET observed=?", (NOW,))
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "h1",
                "kind-a",
                SOURCE,
                "s1",
                "legacy",
                NOW,
                "2026-10-11",
                "model-a",
                json.dumps(asdict(event().counters)),
                1,
            ),
        )
        db.execute(
            "INSERT INTO cursors VALUES(?,?,?,?)", (SOURCE, '{"files":{},"state":{}}', 7, NOW)
        )
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE).revision == 7
        write(store, (event(),))
        assert all(row.counters.total == 24 for row in report(store).rows)
        assert report(store).limits[0].quota_key == "same-quota"
        source = "h2/kind-a/format-a"
        store.apply(
            "kind-a",
            Batch(
                (), Cursor(), Coverage.COMPLETE, 0, (LimitSnapshot("kind-a", "same-quota", NOW),)
            ),
            (),
            source_key=source,
        )
        assert len(report(store).limits) == 1
        assert len(store.query(UsageQuery("h2", "2026-10-11", "2026-10-11"), NOW).limits) == 1
        assert store._db.execute("SELECT parent_session_key FROM events").fetchall() == [
            (None,),
            (None,),
        ]
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE).revision == 8
        assert all(row.counters.total == 24 for row in report(store).rows)
        assert len(store._db.execute("SELECT * FROM limits").fetchall()) == 2


def test_apply_result_retries_replays_and_net_event_changes(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        first = Batch((event(),), Cursor(), Coverage.COMPLETE, 0)
        assert store.apply("kind-a", first, (mapping(),), source_key=SOURCE) == ApplyResult(
            False, 1
        )
        assert store.apply("kind-a", first, (mapping(),), source_key=SOURCE) == ApplyResult()
        replay = replace(first, next_cursor=store.cursor(SOURCE), expected_revision=1)
        assert store.apply("kind-a", replay, (mapping(),), source_key=SOURCE) == ApplyResult()
        changes = store._db.total_changes
        assert store.apply("kind-a", first, (mapping(),), source_key=SOURCE) == ApplyResult()
        assert store._db.total_changes == changes
        cursor = store.cursor(SOURCE)
        transient = replace(event(), counters=Counters(100))
        net_replay = Batch((transient, event()), cursor, Coverage.COMPLETE, cursor.revision)
        assert store.apply("kind-a", net_replay, (), source_key=SOURCE) == ApplyResult()
        assert all(row.counters.total == 12 for row in report(store).rows)
        cursor = store.cursor(SOURCE)
        duplicates = Batch((event("new"), event("new")), cursor, Coverage.COMPLETE, cursor.revision)
        assert store.apply("kind-a", duplicates, (), source_key=SOURCE) == ApplyResult(False, 1)


@pytest.mark.parametrize("field", ["counters", "day", "model", "parent", "verified", "time"])
def test_apply_counts_a_changed_key_once(tmp_path: Path, field: str) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(),))
        changed = {
            "counters": replace(event(), counters=Counters(20)),
            "day": event(t=NOW - 86400_000),
            "model": event(model="model-b"),
            "parent": replace(event(), parent_session_key="parent"),
            "verified": event(verified=False),
            "time": event(t=NOW + 1),
        }[field]
        cursor = store.cursor(SOURCE)
        batch = Batch((changed, changed), cursor, Coverage.COMPLETE, cursor.revision)
        assert store.apply("kind-a", batch, (), source_key=SOURCE) == ApplyResult(False, 1)
        cursor = store.cursor(SOURCE)
        assert (
            store.apply(
                "kind-a",
                replace(batch, next_cursor=cursor, expected_revision=cursor.revision),
                (),
                source_key=SOURCE,
            )
            == ApplyResult()
        )


def test_apply_result_counts_retention_removal_once(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(),))
        cursor = store.cursor(SOURCE)
        old = event(t=NOW - 100 * 86400_000)
        batch = Batch((old, old), cursor, Coverage.COMPLETE, cursor.revision)
        assert store.apply("kind-a", batch, (), source_key=SOURCE) == ApplyResult(False, 1)
        cursor = store.cursor(SOURCE)
        assert (
            store.apply(
                "kind-a",
                replace(batch, next_cursor=cursor, expected_revision=cursor.revision),
                (),
                source_key=SOURCE,
            )
            == ApplyResult()
        )


def test_whole_cursor_overflow_rejected_without_any_write(tmp_path: Path) -> None:
    file = FileCursor(1, 2, 1, 2, NOW, "0" * 64)
    oversized = Cursor({f"f{i:04d}" + "\u1000" * 250: file for i in range(6000)}, {"position": "1"})
    assert encoded_cursor_bytes(oversized) > MAX_CURSOR_BYTES
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(),))
        before = store.cursor(SOURCE)
        changes = store._db.total_changes
        batch = Batch(
            (replace(event(), counters=Counters(999)),),
            oversized,
            Coverage.COMPLETE,
            before.revision,
        )
        with pytest.raises(UsageUnavailable, match="^unavailable$"):
            store.apply("kind-a", batch, (), source_key=SOURCE)
        assert store._db.total_changes == changes
        assert store.cursor(SOURCE) == before
        assert all(row.counters.total == 12 for row in report(store).rows)


def test_file_cursor_diff_writes_only_changed_and_removed_rows(tmp_path: Path) -> None:
    files = {f"f{i:04d}": FileCursor(1, i + 1, 1, 2, NOW, "0" * 64) for i in range(1200)}
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        first = Batch((), Cursor(files, {"position": "1"}), Coverage.COMPLETE, 0)
        assert store.apply("kind-a", first, (), source_key=SOURCE) == ApplyResult(True, 0)
        store._db.executescript("""
            CREATE TABLE file_write_audit(file_key TEXT,operation TEXT);
            CREATE TRIGGER file_updated AFTER UPDATE ON cursor_files BEGIN
              INSERT INTO file_write_audit VALUES(new.file_key,'update'); END;
            CREATE TRIGGER file_inserted AFTER INSERT ON cursor_files BEGIN
              INSERT INTO file_write_audit VALUES(new.file_key,'insert'); END;
            CREATE TRIGGER file_deleted AFTER DELETE ON cursor_files BEGIN
              INSERT INTO file_write_audit VALUES(old.file_key,'delete'); END;
        """)
        changed = dict(files)
        changed["f0000"] = replace(changed["f0000"], offset=2)
        del changed["f0001"]
        batch = Batch((), Cursor(changed, {"position": "1"}), Coverage.COMPLETE, 1)
        assert store.apply("kind-a", batch, (), source_key=SOURCE) == ApplyResult(True, 0)
        assert store._db.execute("SELECT * FROM file_write_audit ORDER BY file_key").fetchall() == [
            ("f0000", "update"),
            ("f0001", "delete"),
        ]
        cursor = store.cursor(SOURCE)
        assert cursor.files == changed and cursor.state == {"position": "1"}
        replay = replace(batch, next_cursor=cursor, expected_revision=cursor.revision)
        assert store.apply("kind-a", replay, (), source_key=SOURCE) == ApplyResult()
        assert store._db.execute("SELECT COUNT(*) FROM file_write_audit").fetchone() == (2,)


def test_cursor_blob_migration_keeps_files_state_and_receipts(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    original = Cursor({"f1": FileCursor(1, 2, 1, 2, NOW, "0" * 64)}, {"position": "1"}, 7)
    other_source = "h2/kind-a/format-a"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE cursors(source TEXT PRIMARY KEY,body TEXT NOT NULL,
              revision INTEGER NOT NULL,updated INTEGER NOT NULL);
            CREATE TABLE receipts(source TEXT,revision INTEGER,digest TEXT,updated INTEGER,
              PRIMARY KEY(source,revision));
        """)
        body = json.dumps(
            {
                "files": {
                    k: {name: value for name, value in asdict(v).items() if name != "context"}
                    for k, v in original.files.items()
                },
                "state": dict(original.state),
            }
        )
        db.executemany(
            "INSERT INTO cursors VALUES(?,?,?,?)",
            [(source, body, 7, NOW) for source in (SOURCE, other_source)],
        )
        legacy_cursor_body = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
        legacy_digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": "kind-a",
                    "batch": {
                        "events": [],
                        "cursor": legacy_cursor_body,
                        "coverage": "complete",
                        "limits": [],
                        "skipped": 0,
                    },
                    "mappings": [],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (SOURCE, 6, legacy_digest, NOW))
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE) == store.cursor(other_source) == original
        assert store._db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (2,)
        assert json.loads(
            store._db.execute("SELECT body FROM cursors WHERE source=?", (SOURCE,)).fetchone()[0]
        ) == dict(original.state)
        assert store._db.execute("SELECT digest FROM receipts").fetchone() == (legacy_digest,)
        before = store._db.total_changes
        legacy_retry = Batch((), replace(original, revision=6), Coverage.COMPLETE, 6)
        assert store.apply("kind-a", legacy_retry, (), source_key=SOURCE) == ApplyResult()
        assert store._db.total_changes == before and store.cursor(SOURCE) == original
        files = {"f1": replace(original.files["f1"], offset=2)}
        batch = Batch((), Cursor(files, original.state), Coverage.COMPLETE, 7)
        assert store.apply("kind-a", batch, (), source_key=SOURCE) == ApplyResult(True, 0)
        assert store.cursor(other_source) == original
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE).files == files and store.cursor(SOURCE).revision == 8
        assert store.cursor(other_source) == original
        assert store._db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (2,)


def test_prune_file_checkpoints_is_bounded_and_leaves_no_orphans(tmp_path: Path) -> None:
    files = {f"f{i:04d}": FileCursor(1, i + 1, 1, 2, NOW, "0" * 64) for i in range(6000)}
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.apply("kind-a", Batch((), Cursor(files), Coverage.COMPLETE, 0), (), source_key=SOURCE)
        assert store._db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (6000,)
        later = NOW + 100 * 86400_000
        assert store.prune(later) <= 5000
        assert store._db.execute("SELECT COUNT(*) FROM cursors").fetchone() == (1,)
        assert store.prune(later) <= 5000
        assert store._db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (0,)
        assert store.cursor(SOURCE) == Cursor()


def test_file_context_round_trip_diff_and_utf8_limit(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    file = FileCursor(1, 2, 1, 2, NOW, "0" * 64)
    context = "\u1000" * (MAX_FILE_CONTEXT_BYTES // 3) + "x"
    assert len(context.encode()) == MAX_FILE_CONTEXT_BYTES
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        store.apply(
            "kind-a", Batch((), Cursor({"f1": file}), Coverage.COMPLETE, 0), (), source_key=SOURCE
        )
        with_context = replace(file, context=context)
        batch = Batch((), Cursor({"f1": with_context}), Coverage.COMPLETE, 1)
        assert store.apply("kind-a", batch, (), source_key=SOURCE) == ApplyResult(True, 0)
        cursor = store.cursor(SOURCE)
        assert cursor.files["f1"] == with_context
        assert (
            store.apply(
                "kind-a",
                replace(batch, next_cursor=cursor, expected_revision=2),
                (),
                source_key=SOURCE,
            )
            == ApplyResult()
        )
        before = store.cursor(SOURCE)
        changes = store._db.total_changes
        oversized = replace(with_context, context=context + "x")
        with pytest.raises(UsageUnavailable, match="^unavailable$"):
            store.apply(
                "kind-a",
                Batch((event(),), Cursor({"f1": oversized}), Coverage.COMPLETE, 3),
                (),
                source_key=SOURCE,
            )
        assert store._db.total_changes == changes and store.cursor(SOURCE) == before
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE) == before


def test_context_column_migration_defaults_existing_rows_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO meta VALUES('cursor_files_version','1');
            CREATE TABLE cursors(source TEXT PRIMARY KEY,body TEXT NOT NULL,
              revision INTEGER NOT NULL,updated INTEGER NOT NULL);
            CREATE TABLE cursor_files(source TEXT,file_key TEXT,dev INTEGER,ino INTEGER,
              offset INTEGER,size INTEGER,mtime_ns INTEGER,head_digest TEXT,
              PRIMARY KEY(source,file_key));
        """)
        db.execute("INSERT INTO cursors VALUES(?,?,?,?)", (SOURCE, "{}", 1, NOW))
        db.execute(
            "INSERT INTO cursor_files VALUES(?,?,?,?,?,?,?,?)",
            (SOURCE, "f1", 1, 2, 1, 2, NOW, "0" * 64),
        )
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store.cursor(SOURCE).files["f1"] == FileCursor(1, 2, 1, 2, NOW, "0" * 64)
        assert store._db.execute("SELECT context FROM cursor_files").fetchone() == ("",)


def test_cursor_migration_failure_rolls_back_all_file_copies(tmp_path: Path) -> None:
    path = tmp_path / "usage.db"
    body = json.dumps(
        {"files": {"f1": asdict(FileCursor(1, 2, 1, 2, NOW, "0" * 64))}, "state": {"v": "1"}}
    )
    bad = json.dumps({"files": {"f1": {"offset": 1}}, "state": {}})
    other = "h2/kind-a/format-a"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE cursors(source TEXT PRIMARY KEY,body TEXT,"
            "revision INTEGER,updated INTEGER)"
        )
        db.executemany(
            "INSERT INTO cursors VALUES(?,?,?,?)", [(SOURCE, body, 7, NOW), (other, bad, 3, NOW)]
        )
    with pytest.raises(UsageUnavailable, match="^unavailable$"):
        SqliteUsageStore(path, "UTC", clock=lambda: NOW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT body FROM cursors WHERE source=?", (SOURCE,)).fetchone() == (
            body,
        )
        assert db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (0,)
        assert (
            db.execute("SELECT value FROM meta WHERE key='cursor_files_version'").fetchone() is None
        )
        db.execute("UPDATE cursors SET body=? WHERE source=?", (body, other))
    with SqliteUsageStore(path, "UTC", clock=lambda: NOW) as store:
        assert store._db.execute("SELECT COUNT(*) FROM cursor_files").fetchone() == (2,)
        assert store.cursor(SOURCE).revision == 7 and store.cursor(other).revision == 3
