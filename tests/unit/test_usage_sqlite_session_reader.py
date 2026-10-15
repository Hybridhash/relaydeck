"""cumulative SQLite session usage reader. Synthetic databases built here;
real table/column names, invented rows only."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from relaydeck.usage.contracts import (
    Basis,
    Coverage,
    Cursor,
    RowScope,
    UsageQuery,
)
from relaydeck.usage.readers import READERS
from relaydeck.usage.readers.sqlite_session_usage import FORMAT_ID, READER
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.session_db import (
    NOW_S,
    SESSIONS_ONLY_SCHEMA,
    USAGE_ONLY_SCHEMA,
    make_db,
    make_home,
    run,
    session_row,
    usage_row,
)
from tests.support.usage.store import NOW, SOURCE


def test_first_pass_emits_full_totals_complete(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE and batch.skipped == 0 and not batch.more
    (event,) = batch.events
    assert event.session_key == "s1" and event.model == "model-a"
    assert event.basis == Basis.CUMULATIVE_DELTA
    assert (event.counters.input_uncached, event.counters.output) == (10, 5)
    assert (event.counters.cache_read, event.counters.cache_write_unknown) == (2, 1)
    assert event.counters.reasoning == 0
    assert event.occurred_ms == int((NOW_S + 100) * 1000)


def test_quiet_second_pass_emits_nothing(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)
    second = run(root, first.next_cursor)
    assert second.events == () and second.coverage == Coverage.COMPLETE


def test_increment_emits_delta_with_new_key(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 25, last_seen = ?", (NOW_S + 200,))
    db.commit()
    db.close()
    second = run(root, first.next_cursor)
    (event,) = second.events
    assert event.counters.input_uncached == 15
    assert event.event_key != first.events[0].event_key
    assert event.occurred_ms == int((NOW_S + 200) * 1000)


def test_retry_from_same_cursor_is_identical(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    cursor = Cursor()
    assert run(root, cursor).events == run(root, cursor).events


def test_reset_rebases_with_gap_then_counts_from_there(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 4, last_seen = ?", (NOW_S + 200,))
    db.commit()
    db.close()
    reset = run(root, first.next_cursor)
    assert reset.events == () and reset.coverage == Coverage.PARTIAL and reset.skipped == 1
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 9, last_seen = ?", (NOW_S + 300,))
    db.commit()
    db.close()
    (event,) = run(root, reset.next_cursor).events
    assert event.counters.input_uncached == 5


def test_event_key_shape(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    (event,) = run(root).events
    kind, dbtag, pk, seq = event.event_key.split(":")
    assert (kind, len(dbtag), len(pk), seq) == ("smu1", 16, 48, "0")


def test_zero_first_sight_stores_nothing(tmp_path: Path) -> None:
    rows = [usage_row(counters=(0, 0, 0, 0, 0))]
    root = make_home(tmp_path, main=rows, main_sessions=[session_row(counters=(0, 0, 0, 0, 0))])
    first = run(root)
    assert first.events == () and first.coverage == Coverage.COMPLETE
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET input_tokens = 7, last_seen = ?", (NOW_S + 200,))
    db.commit()
    db.close()
    (event,) = run(root, first.next_cursor).events
    assert event.counters.input_uncached == 7


def test_null_counters_rules(tmp_path: Path) -> None:
    nulled = usage_row(counters=(None, None, None, None, None))
    root = make_home(tmp_path, main=[nulled], main_sessions=[session_row()])
    batch = run(root)
    assert batch.events == () and batch.coverage == Coverage.PARTIAL and batch.skipped == 1
    quiet = usage_row(api_call_count=0, counters=(None, None, None, None, None))
    root = make_home(tmp_path / "q", main=[quiet], main_sessions=[session_row()])
    silent = run(root)
    assert silent.events == () and silent.coverage == Coverage.COMPLETE


def test_bad_times_are_skipped(tmp_path: Path) -> None:
    rows = [
        usage_row(session="s1", first_seen=NOW_S + 500, last_seen=NOW_S + 100),
        usage_row(session="s2", last_seen=-5),
        usage_row(session="s3", last_seen="soon"),
        usage_row(session="s4"),
    ]
    root = make_home(tmp_path, main=rows, main_sessions=[session_row()])
    batch = run(root)
    assert batch.coverage == Coverage.PARTIAL and batch.skipped == 3
    assert [e.session_key for e in batch.events] == ["s4"]


def test_non_string_pk_part_and_negative_counter_skipped(tmp_path: Path) -> None:
    # TEXT affinity normalizes ints, so only a BLOB survives as a non-string part.
    rows = [
        usage_row(session="s1", model=b"\x00blob"),
        usage_row(session="s2", counters=(-1, 0, 0, 0, 0)),
    ]
    root = make_home(tmp_path, main=rows, main_sessions=[session_row()])
    batch = run(root)
    assert batch.events == () and batch.coverage == Coverage.PARTIAL and batch.skipped == 2


def test_reasoning_exceeding_output_rebases_with_gap(tmp_path: Path) -> None:
    rows = [usage_row(counters=(10, 5, 0, 0, 9))]
    root = make_home(tmp_path, main=rows, main_sessions=[session_row()])
    batch = run(root)
    assert batch.events == () and batch.coverage == Coverage.PARTIAL and batch.skipped == 1
    steady = run(root, batch.next_cursor)
    assert steady.events == () and steady.coverage == Coverage.COMPLETE


def test_fallback_mode_without_usage_table(tmp_path: Path) -> None:
    root = tmp_path / "home"
    make_db(root / "state.db", sessions=[session_row(model=None)], schema=SESSIONS_ONLY_SCHEMA)
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE
    (event,) = batch.events
    assert event.event_key.startswith("ses1:") and event.model is None
    assert event.counters.input_uncached == 10
    assert event.occurred_ms == int((NOW_S - 3600) * 1000)


def test_mixed_session_detected_partial_without_fallback(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[usage_row(session="s1")],
        main_sessions=[session_row("s1"), session_row("s2")],
    )
    batch = run(root)
    assert batch.coverage == Coverage.PARTIAL and batch.skipped == 1
    assert [e.session_key for e in batch.events] == ["s1"]


def test_missing_both_tables_is_partial(tmp_path: Path) -> None:
    root = tmp_path / "home"
    make_db(root / "state.db", schema="CREATE TABLE other(x);")
    batch = run(root)
    assert batch.events == () and batch.coverage == Coverage.PARTIAL and batch.skipped == 1


def test_missing_sessions_table_is_usage_only(tmp_path: Path) -> None:
    root = tmp_path / "home"
    make_db(root / "state.db", usage=[usage_row()], schema=USAGE_ONLY_SCHEMA)
    batch = run(root)
    assert batch.coverage == Coverage.COMPLETE
    (event,) = batch.events
    assert event.cwd_digest is None and event.parent_session_key is None


def test_no_database_anywhere_is_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    batch = run(root)
    assert batch.coverage == Coverage.UNAVAILABLE and batch.retry_blocked
    assert batch.events == () and batch.next_cursor == Cursor()


def test_cost_columns_ignored(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    first = run(root)
    db = sqlite3.connect(root / "state.db")
    db.execute("UPDATE session_model_usage SET estimated_cost_usd = 99, actual_cost_usd = 88")
    db.commit()
    db.close()
    second = run(root, first.next_cursor)
    assert second.events == () and second.coverage == Coverage.COMPLETE


def test_format_registered_with_generic_name() -> None:
    assert READERS[FORMAT_ID] is READER
    assert FORMAT_ID == "sqlite-session-usage-v1"
    assert READER.__class__.__module__.endswith(".sqlite_session_usage")


def test_only_known_tables_queried(tmp_path: Path, monkeypatch) -> None:
    seen: list[str] = []
    real_connect = sqlite3.connect
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])

    def traced(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_trace_callback(seen.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced)
    run(root)
    assert seen, "expected traced SQL"
    for statement in seen:
        for table in re.findall(r"FROM\s+(\w+)", statement, re.IGNORECASE):
            assert table in ("session_model_usage", "sessions", "sqlite_master"), statement


def test_store_round_trip_sums_deltas(tmp_path: Path) -> None:
    root = make_home(tmp_path, main=[usage_row()], main_sessions=[session_row()])
    with SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW) as store:
        store.cursor(SOURCE)
        first = run(root)
        store.apply("kind-a", first, (), source_key=SOURCE)
        db = sqlite3.connect(root / "state.db")
        db.execute(
            "UPDATE session_model_usage SET input_tokens = 30, last_seen = ?", (NOW_S + 200,)
        )
        db.commit()
        db.close()
        store.cursor(SOURCE)
        second = run(root, first.next_cursor)
        store.apply("kind-a", second, (), source_key=SOURCE)
        report = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        totals = sum(
            r.counters.input_uncached for r in report.rows if r.scope == RowScope.NOT_LINKED
        )
        assert totals == 30
