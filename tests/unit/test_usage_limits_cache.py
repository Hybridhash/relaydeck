import json
from pathlib import Path

import pytest

from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    LIMIT_STALE_MS,
    Budget,
    Cursor,
    LimitStatus,
    RowScope,
    SessionMapping,
    UsageQuery,
    UsageSource,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW, event


def cache_file(tmp_path: Path, *, captured: int = NOW) -> Path:
    path = tmp_path / "cache/statusline-limits.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "captured_ms": captured,
                "windows": {
                    "five_hour": {"used_percentage": 12.5, "resets_at": (NOW + 1000) // 1000},
                    "seven_day": {"used_percentage": 30, "resets_at": (NOW + 86400000) // 1000},
                },
            }
        )
    )
    path.chmod(0o600)
    return path


def test_limits_fresh_stale_expiry_missing_and_cursor(tmp_path: Path) -> None:
    from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader

    path = cache_file(tmp_path)
    reader = JsonStatuslineLimitsReader(path)
    rows, cursor = reader.read(tmp_path, "kind-a", Cursor(), Budget(), NOW)
    assert len(rows) == 1 and rows[0].kind == ""
    assert rows[0].windows[0].resets_at_ms == NOW + 1000
    assert all(w.status == LimitStatus.FRESH for w in rows[0].windows)
    assert cursor.state and not cursor.files
    again, _ = reader.read(tmp_path, "kind-a", cursor, Budget(), NOW + LIMIT_STALE_MS + 1)
    assert again[0].observed_ms == NOW
    assert [w.status for w in again[0].windows] == [LimitStatus.EXPIRED, LimitStatus.STALE]
    assert again[0].status == LimitStatus.EXPIRED
    path.write_text(
        json.dumps(
            {
                "captured_ms": NOW,
                "windows": {
                    "seven_day": {"used_percentage": 30, "resets_at": (NOW + 86400000) // 1000},
                },
            }
        )
    )
    only, _ = reader.read(tmp_path, "kind-a", cursor, Budget(), NOW)
    assert len(only[0].windows) == 1 and only[0].windows[0].window_key == "seven_day"
    path.unlink()
    assert reader.read(tmp_path, "kind-a", cursor, Budget(), NOW) == ((), cursor)


@pytest.mark.parametrize("bad", ["symlink", "oversize", "other_fields", "future", "mode", "bool"])
def test_limits_rejects_unsafe_cache(tmp_path: Path, bad: str) -> None:
    from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader

    path = cache_file(tmp_path)
    if bad == "symlink":
        moved = tmp_path / "target"
        path.rename(moved)
        path.symlink_to(moved)
    elif bad == "oversize":
        path.write_bytes(b"x" * 65537)
    elif bad == "mode":
        path.chmod(0o644)
    else:
        value = json.loads(path.read_text())
        if bad == "other_fields":
            value["private-marker"] = "secret"
        elif bad == "future":
            value["captured_ms"] = NOW + 1
        else:
            value["windows"]["five_hour"]["used_percentage"] = True
        path.write_text(json.dumps(value))
    reader = JsonStatuslineLimitsReader(path)
    assert reader.read(tmp_path, "kind-a", Cursor(), Budget(), NOW) == ((), Cursor())


def test_collector_limits_each_pass_own_cursor(tmp_path: Path) -> None:
    from relaydeck.usage.limits.json_statusline_limits import JsonStatuslineLimitsReader

    path = cache_file(tmp_path)
    reader = FakeReader()
    reader.events = (event(session="unlinked"), event(session="unverified", verified=False))
    source = UsageSource("h1", "kind-a", reader.format_id, tmp_path, "json-statusline-limits-v1")
    with SqliteUsageStore(tmp_path / "state/u.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (),
            lambda: NOW,
            limits_readers={"json-statusline-limits-v1": JsonStatuslineLimitsReader(path)},
        )
        worker.run_once(NOW)
        value = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        assert (
            worker.coverage["kind-a"].unmapped_sessions == value.coverage[0].unmapped_sessions == 1
        )
        assert [r.scope for r in value.rows] == [RowScope.NOT_LINKED]
        assert value.limits[0].kind == "kind-a" and len(value.limits[0].windows) == 2
        assert store.cursor(source.source_key).state == {}
        limits_key = "h1/kind-a/limits:format-a"
        assert store.cursor(limits_key).state
        reader.error = True
        worker.run_once(NOW + 1)
        worker.run_once(NOW + LIMIT_STALE_MS + 1)
        assert store.cursor(limits_key).revision == 3
        value = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW + LIMIT_STALE_MS + 1)
        assert value.limits[0].observed_ms == NOW
        assert value.limits[0].status == LimitStatus.EXPIRED
        assert value.coverage[0].observed_ms == NOW  # hook pass is not transcript freshness


def test_sessions_count_only_verified_unmapped(tmp_path: Path) -> None:
    reader = FakeReader()
    reader.events = tuple(event(session=f"s{i}") for i in range(77)) + (
        event("other-call", session="s76"),
        event(session="unverified", verified=False),
    )
    source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
    mappings = tuple(
        SessionMapping(source.source_key, source.kind, f"s{i}", f"i{i}", "h1", "w1", NOW, NOW)
        for i in range(3)
    )
    with SqliteUsageStore(tmp_path / "state/u.db", "UTC", clock=lambda: NOW) as store:
        worker = UsageCollector(
            store, {reader.format_id: reader}, (source,), lambda: mappings, lambda: NOW
        )
        worker.run_once(NOW)
        value = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        assert (
            value.coverage[0].unmapped_sessions == worker.coverage["kind-a"].unmapped_sessions == 74
        )
        unlinked = next(row for row in value.rows if row.scope == RowScope.NOT_LINKED)
        assert unlinked.sessions == 74 and unlinked.counters.total == 75 * 12
        linked = next(row for row in value.rows if row.scope == RowScope.OVERALL)
        assert linked.sessions == 3 and linked.counters.total == 36
