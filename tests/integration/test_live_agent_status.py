"""a captured `pane.agent_status_changed` line changes the agent's status in the
client, so the dock's agent tabs (which read `state.inventory.agents`) update live.

Replays tests/fixtures/upstream/live_agent_status.jsonl (captured from real Herdr, dotted name)
through parse_event -> connector -> gateway stamp -> client parse -> reducer. No network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from relaydeck.client.features.sync.controller import event_from_wire
from relaydeck.client.hooks.views import agent_space_tabs
from relaydeck.connector.herdr import HerdrAdapter, parse_event
from relaydeck.connector.source import HerdrSource
from relaydeck.contracts.schemas import validate_metadata_event
from relaydeck.domain import reducers
from relaydeck.domain.models import Agent, AgentStatus, AppState, Snapshot, Workspace, scope_key
from relaydeck.gateway.backend import prepare_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upstream"
SECRET = b"s" * 32
NOW = 1_700_000_000_000


def _state(epoch: str) -> AppState:
    agent = Agent(
        terminal_id="term-p5",
        host_id="h1",
        workspace_id="w2",
        tab_id="w2:t5",
        pane_id="w2:p5",
        name="Lead",
        agent="agent-a",
        status=AgentStatus.UNKNOWN,
        revision=1,
    )
    snapshot = Snapshot(
        host_id="h1",
        host_epoch=epoch,
        cursor="c0",
        workspaces=(Workspace(id="w2", host_id="h1", label="Space Alpha"),),
        agents=(agent,),
        taken_ms=1000,
    )
    return reducers.apply_snapshot(AppState(), snapshot)


def _push(source: HerdrSource, state: AppState, seq: int, line: bytes) -> AppState:
    name, data = parse_event(line)
    partial = source._consume_live_line(name, data)
    assert partial is not None, f"{name} was skipped by the connector"
    stored = prepare_event("h1", partial, seq, SECRET, NOW)
    validate_metadata_event(stored)
    state, action = reducers.apply_event(state, event_from_wire(stored))
    assert action == "applied", action
    return state


def _dock_status(state: AppState) -> str:
    agent = state.inventory.agents[scope_key("h1", "term-p5")]
    tabs = agent_space_tabs(state, "h1", agent, {"term-p5": "Lead"})
    (entry,) = [tab for tab in tabs if tab[0] == scope_key("h1", "term-p5")]
    return entry[2]


def test_captured_status_lines_flip_the_agent_and_the_dock_tab_live() -> None:
    source = HerdrSource(cast(HerdrAdapter, cast(Any, object())))
    state = _state(source.epoch)
    idle, working = (FIXTURES / "live_agent_status.jsonl").read_bytes().splitlines()
    assert state.inventory.agents[scope_key("h1", "term-p5")].status == AgentStatus.UNKNOWN

    state = _push(source, state, 1, idle)
    assert state.inventory.agents[scope_key("h1", "term-p5")].status == AgentStatus.IDLE
    assert _dock_status(state) == "idle"

    state = _push(source, state, 2, working)
    assert state.inventory.agents[scope_key("h1", "term-p5")].status == AgentStatus.WORKING
    assert _dock_status(state) == "working"


class _Stub:
    """Adapter double for the per-pane subscription: records what the connector opens."""

    def __init__(self) -> None:
        self.opened: list[list[dict[str, Any]]] = []

    async def open_subscription(self, entries: list[dict[str, Any]]) -> Any:
        self.opened.append(entries)

        class _Sub:
            async def aclose(self) -> None:
                return None

        return _Sub()


def test_every_agent_pane_gets_a_per_pane_status_subscription_and_the_set_is_logged(
    caplog: Any,
) -> None:
    import asyncio
    import logging

    from relaydeck.connector import source as source_module

    stub = _Stub()
    source = HerdrSource(cast(HerdrAdapter, stub), clock=lambda: 0)
    source._pane_want = {"w2:p3", "w2:p5", "w2:pB"}
    source._pane_dirty = True
    caplog.set_level(logging.INFO, logger=source_module.LOG.name)

    asyncio.run(source.sync_pane_subscriptions())

    (entries,) = stub.opened
    assert sorted(e["pane_id"] for e in entries) == ["w2:p3", "w2:p5", "w2:pB"]
    assert all(e["type"] == "pane.agent_status_changed" for e in entries)
    records = [
        r.getMessage() for r in caplog.records if "herdr_pane_subscriptions" in r.getMessage()
    ]
    assert records == ["herdr_pane_subscriptions n=3"]
    # Counts only: no pane ids in the log line.
    assert "w2:" not in records[0]
