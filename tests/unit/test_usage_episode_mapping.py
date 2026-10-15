import asyncio
import json

from relaydeck.connector.herdr import BootstrapResult
from relaydeck.connector.source import MAX_USAGE_EPISODES, HerdrSource
from relaydeck.usage.contracts import Batch, Coverage, UsageQuery, UsageSource
from relaydeck.usage.store import SqliteUsageStore
from tests.support.usage.mapping import Adapter, pane, source_fixture
from tests.support.usage.store import NOW, event


def test_snapshot_live_move_return_null_and_durable_late_mapping(tmp_path):
    source, adapter, now = source_fixture(tmp_path)
    first = source.usage_mappings()[0]
    assert first.session_key == "session-private-marker" and first.workspace_id == "space-a"
    # Raw identities never enter the public inventory snapshot.
    assert "session-private-marker" not in json.dumps(source.snapshot_document("h1"))
    now[0] += 100
    source._consume_live_line("pane.updated", {"pane": pane("space-b")})
    now[0] += 100
    source._consume_live_line("pane.moved", {"pane_id": "p1", "workspace_id": "space-a"})
    rows = source.usage_mappings()
    assert [r.workspace_id for r in rows] == ["space-a", "space-b", "space-a"]
    assert len({r.interval_id for r in rows}) == 3
    now[0] += 100
    adapter.document["snapshot"]["panes"][0].update(pane(session=None))
    asyncio.run(source.refresh())
    closed = source.usage_mappings()
    now[0] += 1000
    assert source.usage_mappings() == closed
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as store:
        events = tuple(
            event(str(i), t=NOW + t, session=first.session_key)
            for i, t in enumerate((50, 150, 250))
        )
        store.apply(
            "kind-a",
            Batch(events, store.cursor(first.source_key), Coverage.COMPLETE, 0),
            (),
            source_key=first.source_key,
        )
        assert (
            store.query(UsageQuery("h1", "2026-10-11", "2026-10-11"), now[0]).rows[0].scope
            == "not_linked"
        )
        cursor = store.cursor(first.source_key)
        store.apply(
            "kind-a",
            Batch((), cursor, Coverage.COMPLETE, cursor.revision),
            closed,
            source_key=first.source_key,
        )
    with SqliteUsageStore(tmp_path / "state/usage.db", "UTC", clock=lambda: now[0]) as reopened:
        report = reopened.report_snapshot(UsageQuery("h1", "2026-10-11", "2026-10-11"), now[0])
        spaces = {
            r.workspace_id: r.counters.input_uncached for r in report.rows if r.scope == "workspace"
        }
        assert spaces == {"space-a": 20, "space-b": 10}


def test_null_wrong_agent_and_bounded_episode_memory(tmp_path):
    source, _, now = source_fixture(tmp_path)
    original = len(source.usage_mappings())
    source._consume_live_line(
        "pane.created", {"pane": {**pane(), "pane_id": "p2", "agent_session": None}}
    )
    wrong = pane()
    wrong["agent_session"]["agent"] = "unconfigured-kind"
    source._consume_live_line("pane.updated", {"pane": wrong})
    assert len(source.usage_mappings()) == original
    for i in range(MAX_USAGE_EPISODES + 2):
        now[0] += 1
        source._consume_live_line("pane.updated", {"pane": pane(f"space-{i}")})
    assert len(source.usage_mappings()) == MAX_USAGE_EPISODES
    assert len(source._usage_active) <= MAX_USAGE_EPISODES


def test_capacity_never_evicts_an_uncommitted_early_episode(tmp_path):
    source, _, now = source_fixture(tmp_path)
    earliest = source.usage_mappings()[0]
    for i in range(MAX_USAGE_EPISODES + 2):
        now[0] += 1
        source._consume_live_line("pane.updated", {"pane": pane(f"space-{i}")})
    assert any(row.interval_id == earliest.interval_id for row in source.usage_mappings())
    assert len(source.usage_mappings()) == MAX_USAGE_EPISODES


def test_buffered_live_identity_observation_is_captured(tmp_path):
    async def check():
        adapter = Adapter()
        result = await adapter.bootstrap(())

        async def boot(_):
            return BootstrapResult(
                result.snapshot, [], [("pane.updated", {"pane": pane("space-b")})], None
            )

        adapter.bootstrap = boot
        source = HerdrSource(adapter, clock=lambda: NOW)
        source.configure_usage((UsageSource("h1", "kind-a", "format-a", tmp_path.resolve()),))

        async def no_sub():
            pass

        source._ensure_pane_subscriptions = no_sub
        await source.bootstrap()
        assert [r.workspace_id for r in source.usage_mappings()] == ["space-a", "space-b"]

    asyncio.run(check())


def test_tab_move_pane_move_and_parent_close_end_observed_episodes(tmp_path):
    source, _, now = source_fixture(tmp_path)
    now[0] += 100
    source._consume_live_line("tab.moved", {"tab_id": "t1", "workspace_id": "space-b"})
    assert [m.workspace_id for m in source.usage_mappings()] == ["space-a", "space-b"]
    now[0] += 100
    source._consume_live_line(
        "pane.moved",
        {
            "previous_pane_id": "p1",
            "previous_tab_id": "t1",
            "previous_workspace_id": "space-b",
            "pane": {**pane("space-c"), "pane_id": "p2", "tab_id": "t2"},
        },
    )
    assert set(source._usage_active) == {"p2"}
    assert [m.workspace_id for m in source.usage_mappings()] == ["space-a", "space-b", "space-c"]
    source._consume_live_line("tab.closed", {"tab_id": "t2", "workspace_id": "space-c"})
    assert not source._usage_active
    now[0] += 100
    source._consume_live_line("pane.updated", {"pane": {**pane("space-d"), "pane_id": "p2"}})
    source._consume_live_line("workspace.closed", {"workspace_id": "space-d"})
    assert not source._usage_active
