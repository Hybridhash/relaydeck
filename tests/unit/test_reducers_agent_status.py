"""a pane status upsert updates the agent in that pane (the dock reads agents)."""

from __future__ import annotations

from relaydeck.domain import reducers
from relaydeck.domain.models import (
    Agent,
    AgentStatus,
    AppState,
    EventType,
    MetadataEvent,
    Pane,
    Snapshot,
    Workspace,
    scope_key,
)


def _state() -> AppState:
    agent = Agent(
        terminal_id="term-p5", host_id="h1", workspace_id="w2", tab_id="w2:t5",
        pane_id="w2:p5", name="Lead", agent="agent-a", status=AgentStatus.IDLE, revision=1,
    )  # fmt: skip
    snapshot = Snapshot(
        host_id="h1",
        host_epoch="e1",
        cursor="c0",
        workspaces=(Workspace(id="w2", host_id="h1", label="Space Alpha"),),
        panes=(Pane(id="w2:p5", host_id="h1", workspace_id="w2", tab_id="w2:t5"),),
        agents=(agent,),
        taken_ms=1000,
    )
    return reducers.apply_snapshot(AppState(), snapshot)


def _pane_status(seq: int, status: str) -> MetadataEvent:
    return MetadataEvent(
        event_id=f"e{seq}",
        host_id="h1",
        host_epoch="e1",
        sequence=seq,
        cursor=f"c{seq}",
        type=EventType.PANE_UPSERTED,
        resource_id="w2:p5",
        resource_revision=0,
        observed_ms=1000 + seq,
        payload={"agent_status": status},
    )


def test_pane_status_upsert_updates_the_agent_in_that_pane() -> None:
    state = _state()
    key = scope_key("h1", "term-p5")
    state, action = reducers.apply_event(state, _pane_status(1, "working"))
    assert action == "applied"
    assert state.inventory.agents[key].status == AgentStatus.WORKING
    assert state.inventory.panes[scope_key("h1", "w2:p5")].agent_status == AgentStatus.WORKING
    state, _ = reducers.apply_event(state, _pane_status(2, "idle"))
    assert state.inventory.agents[key].status == AgentStatus.IDLE


def test_pane_upsert_without_status_leaves_the_agent_alone() -> None:
    state = _state()
    event = MetadataEvent(
        event_id="e1", host_id="h1", host_epoch="e1", sequence=1, cursor="c1",
        type=EventType.PANE_UPSERTED, resource_id="w2:p5", resource_revision=0,
        observed_ms=1001, payload={"focused": True},
    )  # fmt: skip
    state, _ = reducers.apply_event(state, event)
    assert state.inventory.agents[scope_key("h1", "term-p5")].status == AgentStatus.IDLE
