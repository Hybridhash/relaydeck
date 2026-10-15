from pathlib import Path

from relaydeck.usage.contracts import RowScope
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, event, mapping, report, write


def test_verified_not_linked_moves_once_on_late_mapping(tmp_path: Path) -> None:
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(), event("second"), event(session="unverified", verified=False)), ())
        value = report(store)
        assert len(value.rows) == 1
        row = value.rows[0]
        assert row.scope == RowScope.NOT_LINKED and row.workspace_id is None
        assert row.counters.total == 24 and row.sessions == 1
        assert value.coverage[0].unmapped_sessions == 1
        assert not report(store, space_id="w1").rows
        write(store, (), (mapping(),))
        value = report(store)
        assert {r.scope for r in value.rows} == {RowScope.WORKSPACE, RowScope.OVERALL}
        assert all(r.counters.total == 24 for r in value.rows)
        assert value.coverage[0].unmapped_sessions == 0


def test_scopes_paginate_without_collision(tmp_path: Path, monkeypatch: object) -> None:
    from pytest import MonkeyPatch

    assert isinstance(monkeypatch, MonkeyPatch)
    monkeypatch.setattr("relaydeck.usage.store.MAX_QUERY_ROWS", 1)
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: NOW) as store:
        write(store, (event(), event(session="unlinked")))
        rows = []
        value = report(store)
        rows.extend(value.rows)
        while value.next_cursor:
            value = report(store, cursor=value.next_cursor)
            rows.extend(value.rows)
        assert len(rows) == 3
        assert {r.scope for r in rows} == set(RowScope)
        assert all((r.workspace_id is not None) == (r.scope == RowScope.WORKSPACE) for r in rows)
