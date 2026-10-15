import threading
from dataclasses import replace

import pytest

from relaydeck.companion.runtime import CompanionRuntime
from relaydeck.connector.source import MAX_USAGE_EPISODES
from relaydeck.usage.collector import UsageCollector
from relaydeck.usage.contracts import (
    Batch,
    Coverage,
    MappingGap,
    UsageQuery,
    UsageSource,
    UsageUnavailable,
)
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.collector import FakeReader
from tests.support.usage.mapping import pane, source_fixture
from tests.support.usage.store import NOW, SOURCE, event, mapping


def move(source, now, space, *, pane_id="p1"):
    now[0] += 1
    source._usage_event("pane.updated", {"pane": {**pane(space), "pane_id": pane_id}})


def fill(source, now):
    for i in range(MAX_USAGE_EPISODES - 1):
        move(source, now, f"space-{i}")


def collector(source, store, now, reader=None):
    reader = reader or FakeReader()
    return UsageCollector(
        store,
        {reader.format_id: reader},
        source._usage_sources,
        source.usage_mappings,
        clock=lambda: now[0],
        mapping_gaps=source.usage_mapping_gaps,
        acknowledge_mappings=source.acknowledge_usage,
    )


def summary(store, now):
    return store.report_snapshot(UsageQuery("h1", "2026-10-11", "2026-10-11"), now[0])


def test_over_capacity_preserves_early_history_and_persists_uncertainty_across_restart(
    tmp_path, caplog
):
    source, _, now = source_fixture(tmp_path)
    fill(source, now)
    for i in range(5):
        move(source, now, f"dropped-{i}")
    gaps = source.usage_mapping_gaps()
    assert len(gaps) == 1 and gaps[0].last_ms is None
    assert len(source.usage_mappings()) == MAX_USAGE_EPISODES
    reader = FakeReader()
    reader.events = (
        event("early", t=NOW, session="session-private-marker"),
        event("uncertain", t=now[0], session="session-private-marker"),
    )
    path = tmp_path / "ledger/usage.db"
    with SqliteUsageStore(path, "UTC", clock=lambda: now[0]) as store:
        worker = collector(source, store, now, reader)
        assert worker.run_once(now[0]) == 2
        result = summary(store, now)
        assert {
            r.workspace_id: r.counters.input_uncached for r in result.rows if r.scope == "workspace"
        } == {"space-a": 10}
        assert next(r for r in result.rows if r.scope == "not_linked").counters.input_uncached == 10
        assert result.coverage[0].coverage == Coverage.UNVERIFIED
        assert store.unmapped_sessions("h1", "2026-10-11", "2026-10-11", now[0]) == {"kind-a": 1}
    with SqliteUsageStore(path, "UTC", clock=lambda: now[0]) as reopened:
        # A fresh process has no in-memory gap. A normal successful apply must
        # not erase the ledger's uncertainty or guess the dropped space.
        worker = UsageCollector(
            reopened,
            {reader.format_id: reader},
            source._usage_sources,
            lambda: (),
            clock=lambda: now[0],
        )
        worker.run_once(now[0])
        result = summary(reopened, now)
        assert result.coverage[0].coverage == Coverage.UNVERIFIED
        assert next(r for r in result.rows if r.scope == "not_linked").counters.input_uncached == 10
    assert "usage_mapping_gap reason=capacity" in caplog.text
    assert "session-private-marker" not in caplog.text and str(tmp_path) not in caplog.text


def test_clean_snapshot_closes_gap_only_after_durable_handoff(tmp_path):
    source, _, now = source_fixture(tmp_path)
    fill(source, now)
    move(source, now, "not-recorded")
    reader = FakeReader()
    reader.events = (event("gap", t=now[0], session="session-private-marker"),)
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:
        worker = collector(source, store, now, reader)
        worker.run_once(now[0])
        now[0] += 100
        source._usage_snapshot({"panes": [pane("space-clean")]})
        gap = source.usage_mapping_gaps()[0]
        assert gap.last_ms == now[0]
        # Closed history is still dirty until the store acknowledges this version.
        assert len(source.usage_mappings()) == MAX_USAGE_EPISODES
        worker.run_once(now[0])
        assert source.usage_mapping_gaps() == ()
        now[0] += 1
        reader.events = (event("clean", t=now[0], session="session-private-marker"),)
        worker.run_once(now[0])
        result = summary(store, now)
        assert (
            next(r for r in result.rows if r.workspace_id == "space-clean").counters.input_uncached
            == 10
        )
        assert next(r for r in result.rows if r.scope == "not_linked").counters.input_uncached == 10
        assert result.coverage[0].coverage == Coverage.UNVERIFIED


def test_failed_apply_never_acknowledges_or_evicts_dirty_history(tmp_path, monkeypatch):
    source, _, now = source_fixture(tmp_path)
    fill(source, now)
    original = source.usage_mappings()[0]
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:
        apply = store.apply

        def fail(*args, **kwargs):
            raise OSError("private-marker")

        monkeypatch.setattr(store, "apply", fail)
        worker = collector(source, store, now)
        worker.run_once(now[0])
        move(source, now, "overflow")
        assert any(m.interval_id == original.interval_id for m in source.usage_mappings())
        assert source.usage_mapping_gaps()
        assert all(not e.persisted for e in source._usage_episodes.values())
        monkeypatch.setattr(store, "apply", apply)
        now[0] += 60_000
        worker.run_once(now[0])
        move(source, now, "accepted-after-commit")
        assert any(m.workspace_id == "accepted-after-commit" for m in source.usage_mappings())


def test_observation_during_delayed_apply_keeps_final_last_seen_dirty(tmp_path, monkeypatch):
    source, _, now = source_fixture(tmp_path)
    fill(source, now)
    active = source.usage_mappings()[-1]
    entered, unblock = threading.Event(), threading.Event()
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:
        apply = store.apply

        def delayed(*args, **kwargs):
            entered.set()
            assert unblock.wait(2)
            return apply(*args, **kwargs)

        monkeypatch.setattr(store, "apply", delayed)
        worker = collector(source, store, now)
        thread = threading.Thread(target=worker.run_once, args=(now[0],))
        thread.start()
        try:
            assert entered.wait(2)
            now[0] += 100
            source._usage_event("pane.closed", {"pane_id": "p1"})
            final_seen = now[0]
            unblock.set()
        finally:
            unblock.set()
            thread.join(2)
        assert not thread.is_alive()
        for i in range(MAX_USAGE_EPISODES + 2):
            move(source, now, f"new-{i}")
        final = next(m for m in source.usage_mappings() if m.interval_id == active.interval_id)
        assert final.last_seen_ms == final_seen > active.last_seen_ms
        monkeypatch.setattr(store, "apply", apply)
        worker.run_once(now[0])
        assert store._db.execute(
            "SELECT last FROM mappings WHERE interval=?", (active.interval_id,)
        ).fetchone() == (final_seen,)


def test_active_episodes_survive_eviction_even_when_durable(tmp_path):
    source, _, now = source_fixture(tmp_path)
    active = source.usage_mappings()[0]
    for i in range(MAX_USAGE_EPISODES - 1):
        move(source, now, f"space-{i}", pane_id="p2")
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:
        collector(source, store, now).run_once(now[0])
        move(source, now, "next", pane_id="p2")
        assert any(m.interval_id == active.interval_id for m in source.usage_mappings())
        assert source._usage_active["p1"] == active.interval_id
        assert any(m.workspace_id == "next" for m in source.usage_mappings())
        assert source.usage_mapping_gaps() == ()


def test_all_configured_sources_must_acknowledge_before_eviction(tmp_path):
    source, _, now = source_fixture(tmp_path)
    original_source = source._usage_sources[0]
    other = UsageSource("h1", "kind-a", "format-b", tmp_path.resolve())
    source.configure_usage((original_source, other))
    fill(source, now)
    rows = source.usage_mappings()
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:

        def commit(configured):
            cursor = store.cursor(configured.source_key)
            store.apply(
                "kind-a",
                Batch((), cursor, Coverage.COMPLETE, cursor.revision),
                rows,
                source_key=configured.source_key,
            )
            source.acknowledge_usage(configured.source_key, rows, ())

        commit(original_source)
        move(source, now, "blocked")
        assert any(m.interval_id == rows[0].interval_id for m in source.usage_mappings())
        assert source.usage_mapping_gaps()
        commit(other)
        move(source, now, "accepted")
        assert not any(m.interval_id == rows[0].interval_id for m in source.usage_mappings())
        assert any(m.workspace_id == "accepted" for m in source.usage_mappings())


@pytest.mark.parametrize(
    "change",
    [
        {"source_key": "h2/kind-a/format-a"},
        {"first_ms": -1},
        {"first_ms": True},
        {"last_ms": NOW - 1},
        {"last_ms": False},
        {"gap_id": ""},
    ],
)
def test_invalid_gap_rolls_back_events_and_cursor(tmp_path, change):
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: NOW) as store:
        cursor = store.cursor(SOURCE)
        gap = replace(MappingGap(SOURCE, "g1", NOW), **change)
        batch = Batch((event(),), cursor, Coverage.COMPLETE, cursor.revision, mapping_gaps=(gap,))
        with pytest.raises(UsageUnavailable):
            store.apply("kind-a", batch, (mapping(),), source_key=SOURCE)
        assert store.cursor(SOURCE).revision == cursor.revision
        assert summary(store, [NOW]).rows == ()


def test_runtime_acknowledges_the_exact_committed_mapping_and_gap_version(tmp_path):
    source, _, now = source_fixture(tmp_path)
    fill(source, now)
    move(source, now, "overflow")
    runtime = object.__new__(CompanionRuntime)
    runtime.source = source
    rows, gaps = runtime._usage_mappings(), runtime._usage_mapping_gaps()
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: now[0]) as store:
        cursor = store.cursor(SOURCE)
        store.apply(
            "kind-a",
            Batch((), cursor, Coverage.UNVERIFIED, cursor.revision, mapping_gaps=gaps),
            rows,
            source_key=SOURCE,
        )
        runtime._acknowledge_usage(SOURCE, rows, gaps)
        move(source, now, "room-after-handoff")
        assert any(m.workspace_id == "room-after-handoff" for m in runtime._usage_mappings())
        # Open uncertainty survives acknowledging a pass and is source-scoped.
        assert runtime._usage_mapping_gaps()[0] == gaps[0]


def test_closed_gap_is_pruned_but_unresolved_gap_stays_explicit(tmp_path):
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: NOW) as store:
        cursor = store.cursor(SOURCE)
        gaps = (MappingGap(SOURCE, "closed", NOW, NOW + 1), MappingGap(SOURCE, "open", NOW))
        store.apply(
            "kind-a",
            Batch((), cursor, Coverage.COMPLETE, cursor.revision, mapping_gaps=gaps),
            (),
            source_key=SOURCE,
        )
        store.prune(NOW + 100 * 24 * 3600_000)
        assert store._db.execute("SELECT gap FROM mapping_gaps").fetchall() == [("open",)]


def test_gap_is_part_of_the_atomic_receipt_and_cannot_modify_an_applied_retry(tmp_path):
    with SqliteUsageStore(tmp_path / "ledger/usage.db", "UTC", clock=lambda: NOW) as store:
        cursor = store.cursor(SOURCE)
        ordinary = Batch((event(),), cursor, Coverage.COMPLETE, cursor.revision)
        store.apply("kind-a", ordinary, (mapping(),), source_key=SOURCE)
        gap = MappingGap(SOURCE, "g1", NOW)
        with pytest.raises(UsageUnavailable, match="stale_batch"):
            store.apply(
                "kind-a", replace(ordinary, mapping_gaps=(gap,)), (mapping(),), source_key=SOURCE
            )
        cursor = store.cursor(SOURCE)
        batch = Batch((), cursor, Coverage.COMPLETE, cursor.revision, mapping_gaps=(gap,))
        store.apply("kind-a", batch, (mapping(),), source_key=SOURCE)
        assert store.apply("kind-a", batch, (mapping(),), source_key=SOURCE).events_changed == 0
        assert summary(store, [NOW]).coverage[0].coverage == Coverage.UNVERIFIED
