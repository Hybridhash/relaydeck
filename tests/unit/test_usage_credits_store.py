"""the meter plane in the store and on the wire. Synthetic events only."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from relaydeck.gateway.usage import UsageService
from relaydeck.usage.contracts import (
    MAX_CREDITS_MILLI,
    Basis,
    Batch,
    Counters,
    Coverage,
    UsageEvent,
    UsageQuery,
    UsageUnavailable,
)
from relaydeck.usage.store import SqliteUsageStore

SOURCE = "h1/kind-a/format-a"
ZONE = "UTC"
NOW = int(datetime(2026, 10, 11, 12, tzinfo=ZoneInfo(ZONE)).timestamp() * 1000)


def _store(tmp_path: Path) -> SqliteUsageStore:
    return SqliteUsageStore(tmp_path / "state/usage.db", ZONE, clock=lambda: NOW)


def _token(key: str = "e1", session: str = "s1") -> UsageEvent:
    return UsageEvent(session, key, NOW, "model-a", Counters(10, output=2), Basis.PER_CALL)


def _credit(key: str, milli: int, session: str = "s2") -> UsageEvent:
    return UsageEvent(
        session,
        key,
        NOW,
        "model-a",
        Counters(),
        Basis.PER_TURN,
        meter="credits",
        credits_milli=milli,
    )


def _apply(store: SqliteUsageStore, events: tuple[UsageEvent, ...]) -> None:
    cursor = store.cursor(SOURCE)
    store.apply(
        "kind-a",
        Batch(events, cursor, Coverage.COMPLETE, cursor.revision, ()),
        (),
        source_key=SOURCE,
    )


def test_rows_group_by_meter_with_unpriced_credit_sums(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, (_token(), _credit("c1", 1500), _credit("c2", 2500, session="s3")))
    rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
    assert {(row.scope.value, row.meter) for row in rows} == {
        ("not_linked", "tokens"),
        ("not_linked", "credits"),
    }
    token_row = next(row for row in rows if row.meter == "tokens")
    credit_row = next(row for row in rows if row.meter == "credits")
    assert token_row.counters.total == 12 and token_row.credits_milli == 0
    assert credit_row.credits_milli == 4000
    assert credit_row.counters.total == 0
    assert credit_row.cost_micro_usd is None  # never priced, never converted
    assert credit_row.priced_cost_micro_usd == 0
    assert credit_row.priced_tokens == 0 and credit_row.unpriced_tokens == 0
    assert credit_row.sessions == 2


def test_keyset_pagination_crosses_the_meter_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, (_token(), _credit("c1", 1500)))
    query = UsageQuery("h1", "2026-10-11", "2026-10-11")
    seen: list[tuple[str, str]] = []
    while True:
        page = store.query(query, NOW, _row_limit=1)
        seen.extend((row.scope.value, row.meter) for row in page.rows)
        if page.next_cursor is None:
            break
        query = UsageQuery("h1", "2026-10-11", "2026-10-11", cursor=page.next_cursor)
    assert sorted(seen) == [("not_linked", "credits"), ("not_linked", "tokens")]


def test_rewrite_replacement_counts_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _apply(store, (_credit("c1", 1500),))
    _apply(store, (_credit("c1", 1500), _credit("c2", 2500)))  # same key re-read
    (row,) = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
    assert row.credits_milli == 4000


def test_mixed_meter_events_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bad = [
        UsageEvent(
            "s", "e", NOW, None, Counters(), Basis.PER_CALL, meter="tokens", credits_milli=1
        ),
        UsageEvent(
            "s", "e", NOW, None, Counters(1), Basis.PER_CALL, meter="credits", credits_milli=1
        ),
        UsageEvent(
            "s", "e", NOW, None, Counters(), Basis.PER_CALL, meter="credits", credits_milli=-1
        ),
        UsageEvent(
            "s",
            "e",
            NOW,
            None,
            Counters(),
            Basis.PER_CALL,
            meter="credits",
            credits_milli=MAX_CREDITS_MILLI + 1,
        ),
        UsageEvent("s", "e", NOW, None, Counters(), Basis.PER_CALL, meter="kwh", credits_milli=0),
    ]
    for event in bad:
        with pytest.raises(UsageUnavailable):
            _apply(store, (event,))


def test_legacy_events_table_migrates_to_token_rows(tmp_path: Path) -> None:
    path = tmp_path / "state/usage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE events(host TEXT,kind TEXT,source TEXT,session TEXT,event TEXT,"
        "occurred INTEGER,day TEXT,model TEXT,counters TEXT,verified INTEGER,"
        "PRIMARY KEY(host,kind,source,session,event))"
    )
    db.execute(
        "INSERT INTO events VALUES('h1','kind-a',?,?,?,?,'2026-10-11','model-a',?,?)",
        (
            SOURCE,
            "s1",
            "e1",
            NOW,
            '{"input_uncached":10,"cache_read":0,'
            '"cache_write_short":0,"cache_write_long":0,"cache_write_unknown":0,'
            '"output":2,"reasoning":0}',
            1,
        ),
    )
    db.commit()
    db.close()
    store = SqliteUsageStore(path, ZONE, clock=lambda: NOW)
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(events)")}
    assert {"meter", "credits_milli", "parent_session_key"} <= columns
    (row,) = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
    assert (row.meter, row.credits_milli, row.counters.total) == ("tokens", 0, 12)


def _service(tmp_path: Path) -> UsageService:
    store = _store(tmp_path)
    config = SimpleNamespace(
        sources=(
            SimpleNamespace(
                kind="kind-a",
                format="json-session-credits-v1",
                display_name="Kind A",
                limits_settings=None,
                limits_cache=None,
            ),
        ),
        timezone=ZONE,
        inferred_linking=True,
    )
    service = UsageService(store, config, "h1", tmp_path / "state", lambda: NOW)
    service._checks = {
        ("kind-a", "json-session-credits-v1"): SimpleNamespace(check=lambda now: None)
    }
    return service


def test_kind_metadata_reports_credits_not_tokens(tmp_path: Path) -> None:
    (entry,) = _service(tmp_path)._metadata(NOW)
    assert entry["credits_reported"] is True
    assert entry["tokens_reported"] is False


def test_wire_rows_carry_meter_and_milli_credits(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.store is not None
    _apply(service.store, (_credit("c1", 1500),))
    body = service._report(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
    (row,) = body["rows"]
    assert (row["meter"], row["credits_milli"]) == ("credits", 1500)
    assert row["cost_micro_usd"] is None
