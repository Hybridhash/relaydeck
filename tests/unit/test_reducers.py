"""domain reducer contracts."""

from __future__ import annotations

import copy
import random
from typing import Any

from relaydeck.domain import reducers
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    CommandRecord,
    CommandStatus,
    ConnectionState,
    EventType,
    MetadataEvent,
    Pane,
    Snapshot,
    Tab,
    Workspace,
    scope_key,
)


def _snapshot() -> Snapshot:
    return Snapshot(
        host_id="h1",
        host_epoch="e1",
        cursor="c0",
        workspaces=(Workspace(id="w1", host_id="h1", label="demo"),),
        tabs=(Tab(id="w1:t1", host_id="h1", workspace_id="w1", label="Agent"),),
        panes=(Pane(id="w1:p1", host_id="h1", workspace_id="w1", tab_id="w1:t1"),),
        agents=(
            Agent(
                terminal_id="term-1",
                host_id="h1",
                workspace_id="w1",
                tab_id="w1:t1",
                pane_id="w1:p1",
                agent="tool-a",
                status=AgentStatus.WORKING,
                revision=1,
            ),
        ),
        taken_ms=1000,
    )


def _event(seq: int, **kw: Any) -> MetadataEvent:
    base: dict[str, Any] = {
        "event_id": f"e{seq}",
        "host_id": "h1",
        "host_epoch": "e1",
        "sequence": seq,
        "cursor": f"c{seq}",
        "type": EventType.AGENT_UPSERTED,
        "resource_id": "term-1",
        "resource_revision": seq,
        "observed_ms": 1000 + seq,
        "payload": {"status": "working"},
    }
    base.update(kw)
    return MetadataEvent(**base)


def test_snapshot_replaces_coherently_and_keeps_selection() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    assert state.connection.cursor == "c0"
    assert state.inventory.agents[scope_key("h1", "term-1")].status == AgentStatus.WORKING
    assert state.stale is False
    state = reducers.select_session(state, "h1", scope_key("h1", "term-1"), "agent")
    gen = state.selection.generation
    state = reducers.apply_snapshot(state, _snapshot())
    assert state.selection.session_key == scope_key("h1", "term-1")
    assert state.selection.generation == gen
    # Selection for a vanished resource is dropped, not dangling.
    state = reducers.select_session(state, "h1", scope_key("h1", "gone"), "agent")
    state = reducers.apply_snapshot(state, _snapshot())
    assert state.selection.session_key == ""


def test_snapshot_does_not_mutate_input() -> None:
    state = AppState()
    frozen = copy.deepcopy(state)
    reducers.apply_snapshot(state, _snapshot())
    assert state == frozen


def test_ordered_events_apply_and_duplicates_idempotent() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state, action = reducers.apply_event(
        state, _event(1, resource_revision=2, payload={"status": "blocked"})
    )
    assert action == "applied"
    assert state.inventory.agents[scope_key("h1", "term-1")].status == AgentStatus.BLOCKED
    before = copy.deepcopy(state)
    state, action = reducers.apply_event(
        state, _event(1, resource_revision=2, payload={"status": "working"})
    )
    assert action == "duplicate"
    # Duplicate with older-or-equal revision cannot regress state.
    assert state.inventory.agents[scope_key("h1", "term-1")].status == AgentStatus.BLOCKED
    assert state.connection.last_sequence == 1
    assert state == before


def test_gap_forces_resync_without_mutation() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state, _ = reducers.apply_event(state, _event(1))
    before = copy.deepcopy(state)
    state, action = reducers.apply_event(state, _event(3))
    assert action == "gap"
    assert state.needs_resync is True
    assert state.connection.last_sequence == 1
    assert state.inventory == before.inventory


def test_stale_epoch_forces_resync() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state, action = reducers.apply_event(state, _event(1, host_epoch="e2"))
    assert action == "stale_epoch"
    assert state.needs_resync is True


def test_wrong_host_ignored() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state, action = reducers.apply_event(state, _event(1, host_id="h2"))
    assert action == "ignored_type"
    assert state.needs_resync is False


def test_removal_clears_selection() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state = reducers.select_session(state, "h1", scope_key("h1", "term-1"), "agent")
    state, action = reducers.apply_event(
        state, _event(1, type=EventType.RESOURCE_REMOVED, resource_id="term-1")
    )
    assert action == "applied"
    assert "term-1" not in state.inventory.agents
    assert state.selection.session_key == ""


def test_command_lifecycle_and_unknown_sticky() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    record = CommandRecord(
        command_id="cmd-1",
        host_id="h1",
        kind="agent.prompt",
        target={"agent_id": "term-1"},
        payload_digest="d1",
    )
    state, result = reducers.submit_command(state, record)
    assert result == "submitted"
    state, result = reducers.submit_command(state, record)
    assert result == "duplicate"
    changed = CommandRecord(
        command_id="cmd-1",
        host_id="h1",
        kind="agent.prompt",
        target={"agent_id": "term-1"},
        payload_digest="d2",
    )
    state, result = reducers.submit_command(state, changed)
    assert result == "conflict"
    state, _ = reducers.apply_event(
        state,
        _event(
            1,
            type=EventType.COMMAND_UPDATED,
            resource_id="cmd-1",
            payload={"status": "unknown"},
        ),
    )
    assert state.commands["cmd-1"].status == CommandStatus.UNKNOWN


def test_revoked_disables_writes() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state = reducers.revoke(state)
    assert state.revoked is True
    assert state.stale is True
    record = CommandRecord(
        command_id="cmd-9", host_id="h1", kind="agent.prompt", target={"agent_id": "t"}
    )
    state, result = reducers.submit_command(state, record)
    assert result == "rejected_revoked"
    assert "cmd-9" not in state.commands


def test_connection_transitions_mark_stale() -> None:
    state = AppState()
    state = reducers.set_connection(state, ConnectionState.LIVE)
    assert state.stale is False
    state = reducers.set_connection(state, ConnectionState.RECONNECTING)
    assert state.stale is True
    state = reducers.set_connection(state, ConnectionState.LIVE)
    assert state.stale is False
    state = reducers.set_connection(state, ConnectionState.ACCESS_REVOKED)
    assert state.revoked is True


def test_collections_bounded() -> None:
    agents = tuple(
        Agent(
            terminal_id=f"t{i}",
            host_id="h1",
            workspace_id="w",
            tab_id="t",
            pane_id="p",
            revision=1,
        )
        for i in range(reducers.MAX_AGENTS + 50)
    )
    snap = Snapshot(host_id="h1", host_epoch="e1", cursor="c", agents=agents)
    state = reducers.apply_snapshot(AppState(), snap)
    assert len(state.inventory.agents) == reducers.MAX_AGENTS
    assert state.inventory.truncated is True


def test_seeded_reorder_converges() -> None:
    """Property-style: any delivery order of the same sequenced events
    converges to the same inventory (duplicates idempotent, revisions win)."""
    events = [
        _event(
            i,
            resource_id=f"term-{i % 3}",
            resource_revision=100 + i,
            payload={"status": s},
        )
        for i, s in enumerate(["working", "blocked", "done"] * 4, start=1)
    ]
    results = []
    for seed in range(8):
        rng = random.Random(seed)  # noqa: S311 - seeded test data
        order = list(events)
        rng.shuffle(order)
        # Deliver each event possibly twice, in shuffled order, through a
        # sequencer that only forwards in-order (like the real consumer).
        state = reducers.apply_snapshot(AppState(), _snapshot())
        pending = {e.sequence: e for e in order}
        nxt = 1
        while pending:
            event = pending.pop(nxt, None)
            if event is None:
                break
            state, _ = reducers.apply_event(state, event)
            state, _ = reducers.apply_event(state, event)  # duplicate
            nxt += 1
        results.append(
            (
                state.connection.last_sequence,
                state.inventory.agents[scope_key("h1", "term-1")].status,
                state.inventory.agents[scope_key("h1", "term-1")].revision,
            )
        )
    assert len(set(results)) == 1
    assert results[0][0] == 12


def test_late_snapshot_cannot_regress_connection_cursor() -> None:
    state = reducers.apply_snapshot(AppState(), _snapshot())
    state, _ = reducers.apply_event(state, _event(1))
    assert state.connection.last_sequence == 1
    # A resnapshot restarts sequencing from a fresh cursor (explicit
    # continuity boundary), it never rewinds silently.
    snap2 = Snapshot(host_id="h1", host_epoch="e1", cursor="c9")
    state = reducers.apply_snapshot(state, snap2)
    assert state.connection.cursor == "c9"
    assert state.connection.last_sequence == 0
    assert state.needs_resync is False
