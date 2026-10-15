"""Rank-1 ambiguity. One session id reported by two panes in overlapping
time stays not_linked; same-space overlap still links; moves keep their fallback."""

from __future__ import annotations

from pathlib import Path

from relaydeck.usage.contracts import (
    Batch,
    Coverage,
    MappingGap,
    RowScope,
    SessionMapping,
    UsageEvent,
    UsageQuery,
    UsageRow,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.store import NOW, SOURCE, event, mapping


def _store(tmp_path: Path) -> SqliteUsageStore:
    return SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)


def _write(
    store: SqliteUsageStore,
    events: tuple[UsageEvent, ...],
    maps: tuple[SessionMapping, ...] = (),
) -> None:
    cursor = store.cursor(SOURCE)
    store.apply(
        "kind-a",
        Batch(events, cursor, Coverage.COMPLETE, cursor.revision),
        maps,
        source_key=SOURCE,
    )


def _rows(store: SqliteUsageStore) -> tuple[UsageRow, ...]:
    return store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows


def test_overlapping_episodes_in_two_spaces_stay_not_linked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (event(t=NOW + 100),),
            (
                mapping("w1", "a", NOW, NOW + 200),
                mapping("w2", "b", NOW, NOW + 200),
            ),
        )
        rows = _rows(store)
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]
        assert rows[0].counters.total == 12


def test_overlapping_episodes_in_one_space_still_link(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (event(t=NOW + 100),),
            (
                mapping("w1", "a", NOW, NOW + 200),
                mapping("w1", "b", NOW + 50, NOW + 250),
            ),
        )
        rows = {row.scope: row for row in _rows(store)}
        assert rows[RowScope.WORKSPACE].workspace_id == "w1"
        assert rows[RowScope.WORKSPACE].linked_sessions == 1
        assert rows[RowScope.WORKSPACE].inferred_sessions == 0


def test_non_overlapping_moves_keep_the_fallback(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (
                event("before", t=NOW + 50),
                event("between", t=NOW + 250),
                event("after", t=NOW + 650),
            ),
            (
                mapping("w1", "a", NOW + 100, NOW + 200),
                mapping("w2", "b", NOW + 300, NOW + 400),
            ),
        )
        rows = {(row.scope, row.workspace_id): row for row in _rows(store)}
        # Before the first episode -> the first; in a gap -> the most recently ended.
        assert rows[(RowScope.WORKSPACE, "w1")].counters.total == 24
        assert rows[(RowScope.WORKSPACE, "w2")].counters.total == 12


def test_gaps_still_override_containing_episodes(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        cursor = store.cursor(SOURCE)
        batch = Batch(
            (event(t=NOW + 100),),
            cursor,
            Coverage.COMPLETE,
            cursor.revision,
            mapping_gaps=(MappingGap(SOURCE, "g1", NOW, NOW + 200),),
        )
        store.apply("kind-a", batch, (mapping("w1", "a", NOW, NOW + 200),), source_key=SOURCE)
        rows = _rows(store)
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_partial_overlap_is_ambiguous_only_where_it_overlaps(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (event("only-a", t=NOW + 50), event("both", t=NOW + 150)),
            (
                mapping("w1", "a", NOW, NOW + 200),
                mapping("w2", "b", NOW + 100, NOW + 300),
            ),
        )
        rows = {(row.scope, row.workspace_id): row for row in _rows(store)}
        assert rows[(RowScope.WORKSPACE, "w1")].counters.total == 12
        assert rows[(RowScope.NOT_LINKED, None)].counters.total == 12
