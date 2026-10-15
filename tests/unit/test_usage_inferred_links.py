"""The inferred rule. Unique digest + kind + time overlap links and is
labelled; anything else stays not_linked; rank-1 is never overridden."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    Batch,
    Coverage,
    MappingGap,
    PaneCwdObservation,
    RowScope,
    UsageEvent,
    UsageQuery,
    UsageRow,
    UsageSource,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.store import NOW, SOURCE, event, mapping

SOURCE_B = "h1/kind-b/format-a"
DIGEST_A = "a1" * 16
DIGEST_B = "b2" * 16


def _store(tmp_path: Path) -> SqliteUsageStore:
    return SqliteUsageStore(tmp_path / "usage.db", "UTC", clock=lambda: NOW)


def _digest_event(key: str = "e1", digest: str = DIGEST_A, **kw) -> UsageEvent:
    return replace(event(key, **kw), cwd_digest=digest)


def _obs(
    space: str = "w1",
    interval: str = "c1",
    first: int = NOW - 100,
    last: int = NOW + 100,
    digest: str = DIGEST_A,
    kind: str = "kind-a",
    source: str = SOURCE,
    gen: int = 0,
) -> PaneCwdObservation:
    return PaneCwdObservation(source, kind, interval, "h1", digest, space, gen, first, last)


def _write(
    store: SqliteUsageStore,
    events: tuple[UsageEvent, ...],
    maps=(),
    cwd: tuple[PaneCwdObservation, ...] = (),
    kind: str = "kind-a",
    source: str = SOURCE,
    gaps: tuple[MappingGap, ...] = (),
) -> None:
    cursor = store.cursor(source)
    store.apply(
        kind,
        Batch(events, cursor, Coverage.COMPLETE, cursor.revision, mapping_gaps=gaps),
        maps,
        source_key=source,
        cwd=cwd,
    )


def _workspace(rows: tuple[UsageRow, ...], space: str) -> UsageRow:
    return next(r for r in rows if r.scope == RowScope.WORKSPACE and r.workspace_id == space)


def test_unique_match_links_and_is_labelled_inferred(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (_digest_event(t=NOW + 100),), cwd=(_obs(),))
        report = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW)
        row = _workspace(report.rows, "w1")
        assert (row.linked_sessions, row.inferred_sessions, row.sessions) == (0, 1, 1)
        (coverage,) = report.coverage
        assert (coverage.linked_sessions, coverage.inferred_sessions) == (0, 1)
        assert coverage.unmapped_sessions == 0


def test_shared_cwd_in_two_spaces_stays_not_linked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            cwd=(_obs("w1", "c1"), _obs("w2", "c2")),
        )
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_two_panes_in_one_space_link_to_it(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            cwd=(_obs("w1", "c1"), _obs("w1", "c2")),
        )
        row = _workspace(store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows, "w1")
        assert (row.linked_sessions, row.inferred_sessions) == (0, 1)


def test_kind_mismatch_stays_not_linked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (_digest_event(t=NOW + 100),))
        _write(
            store,
            (),
            cwd=(_obs(kind="kind-b", source=SOURCE_B),),
            kind="kind-b",
            source=SOURCE_B,
        )
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_gap_overrides_a_digest_match(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            cwd=(_obs(),),
            gaps=(MappingGap(SOURCE, "g1", NOW, NOW + 200),),
        )
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_inferred_flag_off_stays_not_linked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (_digest_event(t=NOW + 100),), cwd=(_obs(),))
        query = UsageQuery("h1", "2026-10-11", "2026-10-11")
        rows = store.query(query, NOW, inferred=False).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]
        (coverage,) = store.query(query, NOW, inferred=False).coverage
        assert coverage.inferred_sessions == 0 and coverage.unmapped_sessions == 1


def test_session_window_before_any_observation_stays_unlinked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (_digest_event(t=NOW + 100),), cwd=(_obs(first=NOW + 1000, last=NOW + 1100),))
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_overlap_boundary_is_inclusive(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event("touch", t=NOW + 100), _digest_event("miss", t=NOW + 300, session="s2")),
            cwd=(_obs(first=NOW, last=NOW + 100), _obs("w1", "c2", NOW + 400, NOW + 500)),
        )
        rows = {
            (row.scope, row.workspace_id): row
            for row in store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        }
        assert rows[(RowScope.WORKSPACE, "w1")].inferred_sessions == 1
        assert rows[(RowScope.NOT_LINKED, None)].counters.total == 12


def test_no_digest_stays_unlinked(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (event(t=NOW + 100),), cwd=(_obs(),))
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_rank1_is_never_overridden_by_a_digest_match(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            maps=(mapping("w1", "a", NOW, NOW + 200),),
            cwd=(_obs("w2", "c1"),),
        )
        row = _workspace(store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows, "w1")
        assert (row.linked_sessions, row.inferred_sessions) == (1, 0)


def test_ambiguous_rank1_never_falls_through_to_inferred(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (_digest_event(t=NOW + 100),),
            maps=(
                mapping("w1", "a", NOW, NOW + 200),
                mapping("w2", "b", NOW, NOW + 200),
            ),
            cwd=(_obs("w1", "c1"),),
        )
        rows = store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows
        assert [row.scope for row in rows] == [RowScope.NOT_LINKED]


def test_mixed_row_counts_exact_and_inferred(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(
            store,
            (
                _digest_event("e1", session="s1", t=NOW + 100),
                _digest_event("e2", DIGEST_B, session="s2", t=NOW + 100),
            ),
            maps=(mapping("w1", "a", NOW, NOW + 200, session="s1"),),
            cwd=(_obs("w1", "c1", digest=DIGEST_B),),
        )
        row = _workspace(store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), NOW).rows, "w1")
        assert (row.linked_sessions, row.inferred_sessions, row.sessions) == (1, 1, 2)


def test_unmapped_sessions_excludes_inferred(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _write(store, (_digest_event(t=NOW + 100),), cwd=(_obs(),))
        assert store.unmapped_sessions("h1", "2026-10-11", "2026-10-11", NOW) == {}
        assert store.unmapped_sessions("h1", "2026-10-11", "2026-10-11", NOW, inferred=False) == {
            "kind-a": 1
        }


def test_collector_carries_digest_and_applies_cwd(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        reader = FakeReader()
        reader.events = (event(),)
        source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
        acked: list = []
        worker = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (),
            clock=lambda: NOW,
            digest=lambda folder: "dd",
            cwd_observations=lambda: (_obs(),),
            acknowledge_cwd=lambda key, rows: acked.append((key, rows)),
        )
        worker.run_once(NOW)
        assert reader.budgets and reader.budgets[0].digest is not None
        assert reader.budgets[0].digest("anything") == "dd"
        assert acked == [(SOURCE, (_obs(),))]
        found = store._db.execute("SELECT digest,workspace FROM pane_cwd").fetchall()
        assert found == [(DIGEST_A, "w1")]


def test_collector_inferred_flag_reaches_unmapped_counts(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        reader = FakeReader()
        reader.events = (_digest_event(t=NOW + 100),)
        source = UsageSource("h1", "kind-a", reader.format_id, tmp_path)
        obs = (_obs(),)
        on = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (),
            clock=lambda: NOW,
            cwd_observations=lambda: obs,
            acknowledge_cwd=lambda key, rows: None,
            inferred_linking=True,
        )
        on.run_once(NOW)
        assert on.coverage["kind-a"].unmapped_sessions == 0
        off = UsageCollector(
            store,
            {reader.format_id: reader},
            (source,),
            lambda: (),
            clock=lambda: NOW,
            cwd_observations=lambda: obs,
            acknowledge_cwd=lambda key, rows: None,
            inferred_linking=False,
        )
        off.run_once(NOW)
        assert off.coverage["kind-a"].unmapped_sessions == 1
