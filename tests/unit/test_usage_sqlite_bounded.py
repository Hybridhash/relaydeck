"""Every SQL read in the SQLite usage readers respects row/byte/deadline budgets.

Large or growing live databases must not exhaust memory or stall collection: rows are
fetched in bounded chunks with resumable keyset cursors, a progress handler aborts
statements at the budget deadline, retrieved text is capped (over-long fields are skipped
and counted), and the session index is capped at its allowed row count plus one.
Exceeding a budget gives more=True / PARTIAL with a resumable cursor, never
partial-as-complete and never a crash. Synthetic databases only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from relaydeck.usage.contracts import Batch, Budget, Coverage, Cursor
from relaydeck.usage.readers import _session_index as index_mod
from relaydeck.usage.readers._session_index import load_session_index
from relaydeck.usage.readers.sqlite_session_usage import READER, _Reader
from tests.support.usage.session_db import (
    SESSIONS_ONLY_SCHEMA,
    make_db,
    make_home,
    session_row,
    usage_row,
)

NOW_S = 1_769_000_000.0


def _usage_sid(n: int) -> dict[str, Any]:
    return usage_row(
        session=f"s{n:05d}",
        counters=(10 + n % 7, 5, 2, 1, 0),
        first_seen=NOW_S - 3600,
        last_seen=NOW_S + n,
    )


def _sig(batch: Batch) -> set[tuple[Any, ...]]:
    return {
        (
            e.session_key,
            e.event_key,
            e.occurred_ms,
            e.model,
            (
                e.counters.input_uncached,
                e.counters.output,
                e.counters.cache_read,
                e.counters.cache_write_unknown,
                e.counters.reasoning,
            ),
            e.parent_session_key,
            e.cwd_digest,
        )
        for e in batch.events
    }


def _read_all(
    root: Path, budget: Budget, limit: int = 30
) -> tuple[set[tuple[Any, ...]], Batch, int]:
    seen: set[tuple[Any, ...]] = set()
    cursor = Cursor()
    passes = 0
    while True:
        batch = READER.read(root, cursor, budget)
        passes += 1
        seen |= _sig(batch)
        if not batch.more or passes >= limit:
            return seen, batch, passes
        cursor = batch.next_cursor


def _make_index(root: Path, rows: list[tuple[Any, Any, Any]]) -> Path:
    path = root / "session-index.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE sessions(session_id TEXT PRIMARY KEY, model_id TEXT,"
        " workspace_root TEXT, title TEXT NOT NULL DEFAULT '')"
    )
    db.executemany("INSERT INTO sessions(session_id, model_id, workspace_root) VALUES(?,?,?)", rows)
    db.commit()
    db.close()
    return path


# -- many rows: stop at budget, resume to identical totals --------------------------------


def test_many_usage_rows_stop_and_resume_to_identical_totals(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[_usage_sid(n) for n in range(3000)],
        main_sessions=[session_row(f"s{n:05d}") for n in range(10)],
    )
    (whole,) = (READER.read(root, Cursor(), Budget()),)
    assert whole.coverage == Coverage.COMPLETE and not whole.more
    resumed, last, passes = _read_all(root, Budget(max_bytes=100_000))
    assert passes > 1
    assert last.coverage == Coverage.COMPLETE and not last.more
    assert resumed == _sig(whole)


def test_fallback_rows_resume_through_keyset_to_identical_totals(tmp_path: Path) -> None:
    root = tmp_path / "home"
    make_db(
        root / "state.db",
        schema=SESSIONS_ONLY_SCHEMA,
        sessions=[session_row(f"s{n:05d}") for n in range(500)],
    )
    whole = READER.read(root, Cursor(), Budget())
    assert whole.coverage == Coverage.COMPLETE and not whole.more
    # The maps scan charges bytes too (500 sessions): the budget must clear it
    # yet stop the row loop, so several resumable passes result.
    resumed, last, passes = _read_all(root, Budget(max_bytes=150_000))
    assert passes > 1
    assert last.coverage == Coverage.COMPLETE and not last.more
    assert resumed == _sig(whole)


# -- deadline aborts ---------------------------------------------------------------------


def test_progress_handler_aborts_a_slow_query(tmp_path: Path) -> None:
    path = make_db(tmp_path / "state.db", usage=[usage_row()], sessions=[session_row()])
    reader = _Reader(Cursor(), Budget(deadline_ms=0))
    db = reader._connect(path)
    assert db is not None
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c LIMIT 2000000)"
                " SELECT COUNT(*) FROM c"
            ).fetchone()
    finally:
        db.close()


def test_aborted_pass_is_partial_with_resume_and_completes_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_home(
        tmp_path,
        main=[_usage_sid(n) for n in range(5000)],
        main_sessions=[session_row(f"s{n:05d}") for n in range(10)],
    )
    whole_sig, whole_last, _ = _read_all(root, Budget())
    assert whole_last.coverage == Coverage.COMPLETE and not whole_last.more
    import time as time_mod

    real = time_mod.monotonic
    calls = 0

    def flipping() -> float:
        nonlocal calls
        calls += 1
        return 1_000.0 if calls <= 500 else 1_000_000.0

    monkeypatch.setattr(time_mod, "monotonic", flipping)
    first = READER.read(root, Cursor(), Budget())
    assert first.more is True  # aborted mid-pass, cleanly: no raise, resumable
    assert first.coverage == Coverage.PARTIAL
    monkeypatch.setattr(time_mod, "monotonic", real)
    seen = _sig(first)
    cursor, passes = first.next_cursor, 1
    while True:
        batch = READER.read(root, cursor, Budget())
        passes += 1
        seen |= _sig(batch)
        if not batch.more or passes > 10:
            break
        cursor = batch.next_cursor
    assert seen == whole_sig


# -- oversized fields are skipped and counted -----------------------------------------------


def test_oversized_identity_fields_are_skipped_and_counted(tmp_path: Path) -> None:
    big = "x" * 9000
    root = make_home(
        tmp_path,
        main=[
            _usage_sid(1),
            _usage_sid(2),
            usage_row(session=big),
            usage_row(session="s-big-model", model=big),
        ],
        main_sessions=[session_row("s00001"), session_row("s00002")],
    )
    batch = READER.read(root, Cursor(), Budget())
    assert {e.session_key for e in batch.events} == {"s00001", "s00002"}
    assert batch.skipped == 3  # giant id skipped twice (holder scan + row), giant model once
    assert batch.coverage == Coverage.PARTIAL


def test_oversized_enrichment_folder_is_skipped_and_counted(tmp_path: Path) -> None:
    root = make_home(
        tmp_path,
        main=[_usage_sid(1)],
        main_sessions=[session_row("s00001", cwd="y" * 200_000)],
    )
    batch = READER.read(root, Cursor(), Budget())
    (event,) = batch.events
    assert event.cwd_digest is None
    assert batch.skipped == 1
    assert batch.coverage == Coverage.PARTIAL


def test_oversized_fallback_id_is_skipped_and_counted(tmp_path: Path) -> None:
    root = tmp_path / "home"
    make_db(
        root / "state.db",
        schema=SESSIONS_ONLY_SCHEMA,
        sessions=[session_row("s-ok"), session_row("z" * 9000)],
    )
    batch = READER.read(root, Cursor(), Budget())
    assert [e.session_key for e in batch.events] == ["s-ok"]
    assert batch.skipped == 3  # holder scan + maps + fallback row
    assert batch.coverage == Coverage.PARTIAL


# -- holder scan obeys the same limits ------------------------------------------------------


def test_holder_scan_cap_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import relaydeck.usage.readers.sqlite_session_usage as usage_mod

    monkeypatch.setattr(usage_mod, "_MAX_IDS_PER_DB", 4)
    root = make_home(
        tmp_path,
        main=[_usage_sid(n) for n in range(10)],
        main_sessions=[session_row(f"s{n:05d}") for n in range(10)],
    )
    batch = READER.read(root, Cursor(), Budget())
    assert len(batch.events) == 10  # one db: no collision possible, all rows still emit
    assert batch.skipped == 1
    assert batch.coverage == Coverage.PARTIAL


def test_holder_scan_cut_defers_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_home(
        tmp_path,
        main=[_usage_sid(n) for n in range(50)],
        main_sessions=[session_row(f"s{n:05d}") for n in range(50)],
    )
    whole = READER.read(root, Cursor(), Budget())
    assert whole.coverage == Coverage.COMPLETE
    import time as time_mod

    real = time_mod.monotonic
    calls = 0

    def flipping() -> float:
        nonlocal calls
        calls += 1
        return 1_000.0 if calls <= 2 else 1_000_000.0

    monkeypatch.setattr(time_mod, "monotonic", flipping)
    first = READER.read(root, Cursor(), Budget())
    assert first.events == () and first.more is True
    assert first.coverage == Coverage.PARTIAL
    monkeypatch.setattr(time_mod, "monotonic", real)
    resumed, last, _ = _read_all(root, Budget(), limit=10)
    assert resumed == _sig(whole) and last.coverage == Coverage.COMPLETE


# -- session index caps ----------------------------------------------------------------------


def test_index_over_rowed_returns_none_at_cap_plus_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "MAX_INDEX_ROWS", 3)
    root = tmp_path / "data"
    root.mkdir()
    _make_index(root, [(f"s{n}", "m", "/w") for n in range(4)])
    assert load_session_index(root) == (None, 0)


def test_index_at_exactly_the_cap_still_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_mod, "MAX_INDEX_ROWS", 3)
    root = tmp_path / "data"
    root.mkdir()
    _make_index(root, [(f"s{n}", "m", "/w") for n in range(3)])
    index, used = load_session_index(root)
    assert index is not None and used > 0
    assert index.model("s0") == "m" and index.workspace("s2") == "/w"


def test_index_oversized_fields_keep_unbounded_semantics(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _make_index(
        root,
        [
            ("s-ok", "m-ok", "/w-ok"),
            ("s-big-model", "m" * 9000, "/w"),
            ("s-big-root", "m", "/w" * 9000),
            ("s" * 9000, "m", "/w"),
        ],
    )
    index, _ = load_session_index(root)
    assert index is not None
    assert index.model("s-ok") == "m-ok" and index.workspace("s-ok") == "/w-ok"
    assert index.model("s-big-model") is None  # over-long model: no model, row kept
    assert index.workspace("s-big-root") is None  # over-long root: no folder, row kept
    assert index.model("s" * 9000) is None  # over-long id: row skipped


def test_index_deadline_aborts_cleanly(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _make_index(root, [(f"s{n}", "m", "/w") for n in range(10)])
    assert load_session_index(root, deadline_ms=0) == (None, 0)
    index, _ = load_session_index(root)  # no deadline: unchanged behaviour
    assert index is not None and index.model("s0") == "m"


def test_index_handler_aborts_mid_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    _make_index(root, [(f"s{n}", "m", "/w") for n in range(5000)])
    import time as time_mod

    calls = 0

    def flipping() -> float:
        nonlocal calls
        calls += 1
        return 1_000.0 if calls <= 50 else 1_000_000.0

    monkeypatch.setattr(time_mod, "monotonic", flipping)
    assert load_session_index(root, deadline_ms=750) == (None, 0)
