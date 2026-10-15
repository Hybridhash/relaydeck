"""linking metadata, profiles, collisions, budgets, and live-database safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from relaydeck.usage.contracts import Coverage, Cursor
from relaydeck.usage.readers.sqlite_session_usage import FORMAT_ID
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.session_db import (
    NOW_S,
    make_db,
    make_home,
    run,
    session_row,
    usage_row,
)
from tests.support.usage.store import NOW, SOURCE


def test_cwd_digest_on_events(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    batch = run(root, digest=lambda folder: f"d:{folder}")
    (event,) = batch.events
    assert event.cwd_digest == "d:/synthetic/work/alpha"


def test_no_digest_callable_gives_none(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    (event,) = run(root).events
    assert event.cwd_digest is None


def test_digest_exception_keeps_event_without_digest(tmp_path: Path) -> None:
    def explode(folder: str) -> str:
        raise RuntimeError("synthetic")

    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    batch = run(root, digest=explode)
    (event,) = batch.events
    assert event.cwd_digest is None and batch.coverage == Coverage.COMPLETE


def test_parent_passthrough(tmp_path: Path) -> None:
    rows = [session_row("child", parent_session_id="parent")]
    root = make_home(tmp_path, main=[usage_row(session="child")], main_sessions=rows)
    (event,) = run(root).events
    assert event.parent_session_key == "parent"


def test_profiles_fan_out(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[usage_row(session="m")],
        main_sessions=[session_row("m")],
        profiles={
            "alpha": ([usage_row(session="a")], [session_row("a")]),
            "beta": ([usage_row(session="b")], [session_row("b")]),
        },
    )
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE
    assert sorted(e.session_key for e in batch.events) == ["a", "b", "m"]


def test_profile_without_db_ok(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    (root / "profiles" / "empty").mkdir(parents=True)
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE and len(batch.events) == 1


def test_broken_db_partial_others_fine(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[usage_row(session="m")],
        main_sessions=[session_row("m")],
        profiles={"good": ([usage_row(session="g")], [session_row("g")])},
    )
    bad = root / "profiles" / "bad"
    bad.mkdir(parents=True)
    (bad / "state.db").write_bytes(b"not a database at all")
    batch = run(root)
    assert batch.coverage == Coverage.PARTIAL and batch.skipped >= 1
    assert sorted(e.session_key for e in batch.events) == ["g", "m"]


def _colliding_home(tmp_path: Path) -> Path:
    return make_home(
        tmp_path,
        profiles={
            "alpha": ([usage_row(session="shared")], [session_row("shared")]),
            "beta": ([usage_row(session="shared")], [session_row("shared")]),
        },
    )


def test_collision_namespaces_non_first_both_counted(tmp_path: Path) -> None:
    root = _colliding_home(tmp_path)
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE
    keys = [e.session_key for e in batch.events]
    assert "shared" in keys
    namespaced = [k for k in keys if k != "shared"]
    assert len(namespaced) == 1 and namespaced[0].endswith("/shared")
    assert sum(e.counters.input_uncached for e in batch.events) == 20
    assert batch.next_cursor.state["col-n"] == "1"
    assert batch.events[0].event_key != batch.events[1].event_key


def test_collision_three_dbs_one_bare_two_namespaced(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        profiles={
            name: ([usage_row(session="x")], [session_row("x")])
            for name in ("alpha", "beta", "gamma")
        },
    )
    batch = run(root)
    keys = [e.session_key for e in batch.events]
    assert keys.count("x") == 1
    assert sum(k.endswith("/x") for k in keys) == 2
    assert sum(e.counters.input_uncached for e in batch.events) == 30
    assert batch.next_cursor.state["col-n"] == "1"


def test_collision_stable_across_passes(tmp_path: Path) -> None:
    root = _colliding_home(tmp_path)
    first = run(root)
    second = run(root, first.next_cursor)
    assert second.events == () and second.coverage == Coverage.COMPLETE
    assert second.next_cursor.state["col-n"] == "1"


def test_collision_store_totals_exact(tmp_path: Path) -> None:
    from relaydeck.usage.contracts import RowScope, UsageQuery

    root = _colliding_home(tmp_path)
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.cursor(SOURCE)
        store.apply("kind-a", run(root), (), source_key=SOURCE)
        report = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        totals = sum(
            r.counters.input_uncached for r in report.rows if r.scope == RowScope.NOT_LINKED
        )
        assert totals == 20


def test_late_collision_keeps_veteran_bare(tmp_path: Path) -> None:
    root = make_home(tmp_path, profiles={"beta": ([usage_row(session="x")], [session_row("x")])})
    first = run(root)
    assert [e.session_key for e in first.events] == ["x"]
    make_db(
        root / "profiles" / "alpha" / "state.db",
        usage=[usage_row(session="x")],
        sessions=[session_row("x")],
    )
    second = run(root, first.next_cursor)
    keys = [e.session_key for e in second.events]
    assert len(keys) == 1 and keys[0].endswith("/x") and keys[0] != "x"
    assert second.events[0].counters.input_uncached == 10
    db = sqlite3.connect(root / "profiles" / "beta" / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 15, last_seen = ?", (NOW_S + 400,))
    db.commit()
    db.close()
    third = run(root, second.next_cursor)
    assert [e.session_key for e in third.events] == ["x"]
    assert third.events[0].counters.input_uncached == 5


def test_decoupling_rebares_survivor_keeps_count(tmp_path: Path) -> None:
    root = _colliding_home(tmp_path)
    first = run(root)
    bare = [e.session_key for e in first.events if e.session_key == "shared"]
    assert bare == ["shared"]
    (root / "profiles" / "alpha" / "state.db").unlink()
    second = run(root, first.next_cursor)
    assert second.events == ()
    assert second.next_cursor.state["col-n"] == "1"
    db = sqlite3.connect(root / "profiles" / "beta" / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 14, last_seen = ?", (NOW_S + 400,))
    db.commit()
    db.close()
    third = run(root, second.next_cursor)
    assert [e.session_key for e in third.events] == ["shared"]


def test_identical_rows_never_share_keys(tmp_path: Path) -> None:
    root = _colliding_home(tmp_path)
    (first, second) = run(root).events
    assert first.event_key != second.event_key
    assert first.event_key.split(":")[1] != second.event_key.split(":")[1]


def test_budget_cut_resumes_without_dupes(tmp_path: Path) -> None:
    rows = [usage_row(session=f"s{i}") for i in range(3)]
    srows = [session_row(f"s{i}") for i in range(3)]
    root = make_home(tmp_path, main=rows, main_sessions=srows)
    cursor: Cursor = Cursor()
    seen: list = []
    for _ in range(5):
        batch = run(root, cursor, max_events=1)
        seen.extend(batch.events)
        cursor = batch.next_cursor
        if not batch.more:
            break
    assert len(seen) == 3
    assert len({(e.session_key, e.event_key) for e in seen}) == 3
    assert sum(e.counters.input_uncached for e in seen) == 30


def test_wal_change_rereads(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    db = sqlite3.connect(root / "state.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("UPDATE session_model_usage SET input_tokens = 12, last_seen = ?", (NOW_S + 200,))
    db.commit()
    db.close()
    first = run(root)
    assert [e.counters.input_uncached for e in first.events] == [12]
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 15, last_seen = ?", (NOW_S + 300,))
    db.commit()
    db.close()
    second = run(root, first.next_cursor)
    assert [e.counters.input_uncached for e in second.events] == [3]


def test_unchanged_db_skips_row_reads(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    real_connect = sqlite3.connect
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)

    def traced(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(seen.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    second = run(root, first.next_cursor)
    assert second.events == ()
    assert seen, "expected id-scan SQL"
    assert not any("input_tokens" in statement for statement in seen)


def test_replaced_db_self_heals(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)
    (root / "state.db").unlink()
    make_db(root / "state.db", usage=[usage_row()], sessions=[session_row()])
    same = run(root, first.next_cursor)
    assert same.events == ()  # same totals: nothing new
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 4, last_seen = ?", (NOW_S + 200,))
    db.commit()
    db.close()
    reset = run(root, same.next_cursor)
    assert reset.events == () and reset.coverage == Coverage.PARTIAL


def test_reader_creates_no_sidecars(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    writer = sqlite3.connect(root / "state.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("UPDATE session_model_usage SET input_tokens = 11")
    writer.commit()
    try:
        before = {(p.relative_to(root), p.stat().st_size) for p in root.rglob("*") if p.is_file()}
        assert any(str(rel).endswith("-wal") for rel, _ in before)
        batch = run(root)
        assert [e.counters.input_uncached for e in batch.events] == [11]
        after = {(p.relative_to(root), p.stat().st_size) for p in root.rglob("*") if p.is_file()}
        assert before == after
    finally:
        writer.close()


def test_symlinked_db_is_visible_partial(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[usage_row(session="m")],
        main_sessions=[session_row("m")],
        profiles={"p": ([usage_row(session="p")], [session_row("p")])},
    )
    real = tmp_path / "real.db"
    (root / "state.db").rename(real)
    (root / "state.db").symlink_to(real)
    batch = run(root)
    assert batch.coverage == Coverage.PARTIAL and batch.skipped >= 1
    assert [e.session_key for e in batch.events] == ["p"]


def test_col_n_recorded_zero_when_clean(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    batch = run(root)
    assert batch.next_cursor.state["col-n"] == "0"
    assert batch.next_cursor.state["col"] == "{}"


def test_digest_reaches_events_via_collector_budget(tmp_path: Path) -> None:
    from relaydeck.usage.collector import UsageCollector
    from relaydeck.usage.contracts import UsageSource
    from relaydeck.usage.readers import READERS

    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        source = UsageSource("h1", "kind-a", FORMAT_ID, root)
        worker = UsageCollector(
            store,
            READERS,
            (source,),
            lambda: (),
            clock=lambda: NOW,
            digest=lambda folder: "dd",
        )
        worker.run_once(NOW)
        found = store._db.execute("SELECT digest FROM session_cwd").fetchall()
        assert found == [("dd",)]
