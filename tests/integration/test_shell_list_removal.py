"""a closed shell tab (and its pane) leaves the Terminals list through the real
path: Herdr live line -> connector translate -> gateway stamp -> client event parse -> reducer.
A removal whose payload does not translate still marks the snapshot dirty, so the link
re-reads it. Synthetic payloads only; no network, no live Herdr.
"""

from __future__ import annotations

from typing import Any, cast

from relaydeck.client.features.inventory.components.models import terminal_rows
from relaydeck.client.features.inventory.selectors import space_rows
from relaydeck.client.features.sync.controller import event_from_wire
from relaydeck.connector.herdr import HerdrAdapter
from relaydeck.connector.source import HerdrSource, translate_event
from relaydeck.contracts.schemas import validate_metadata_event
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, Pane, Snapshot, Tab, Workspace, scope_key
from relaydeck.gateway.backend import prepare_event

NOW = 1_700_000_000_000
SECRET = b"s" * 32


def _state() -> AppState:
    snapshot = Snapshot(
        host_id="h1",
        host_epoch="e1",
        cursor="c0",
        workspaces=(Workspace(id="w1", host_id="h1", label="demo"),),
        tabs=(
            Tab(id="w1:tC", host_id="h1", workspace_id="w1", label="test Terminal", pane_count=1),
            Tab(id="w1:tD", host_id="h1", workspace_id="w1", label="other", pane_count=1),
        ),
        panes=(
            Pane(id="w1:pC", host_id="h1", workspace_id="w1", tab_id="w1:tC"),
            Pane(id="w1:pD", host_id="h1", workspace_id="w1", tab_id="w1:tD"),
        ),
        taken_ms=1000,
    )
    return reducers.apply_snapshot(AppState(), snapshot)


def _through_pipeline(state: AppState, name: str, data: dict[str, Any]) -> AppState:
    partial = translate_event(
        name, data, epoch="e1", event_id="ev-1", observed_at="2026-10-11T12:34:07Z"
    )
    assert partial is not None, f"{name} was skipped by the connector"
    stored = prepare_event("h1", partial, 1, SECRET, NOW)
    validate_metadata_event(stored)
    state, action = reducers.apply_event(state, event_from_wire(stored))
    assert action == "applied"
    return state


def test_tab_closed_removes_the_shell_row_and_counts() -> None:
    state = _state()
    assert [entry.row.title for entry in terminal_rows(state, "", "", frozenset())] == [
        "other",
        "test Terminal",
    ]
    assert {r.key: r.count for r in space_rows(state, shells=True)}[scope_key("h1", "w1")] == 2

    state = _through_pipeline(state, "tab.closed", {"tab_id": "w1:tC"})

    assert scope_key("h1", "w1:tC") not in state.inventory.tabs
    assert [entry.row.title for entry in terminal_rows(state, "", "", frozenset())] == ["other"]
    assert {r.key: r.count for r in space_rows(state, shells=True)}[scope_key("h1", "w1")] == 1


def test_pane_closed_removes_the_pane_from_inventory() -> None:
    state = _state()
    state = _through_pipeline(state, "pane.closed", {"pane_id": "w1:pC"})
    assert scope_key("h1", "w1:pC") not in state.inventory.panes
    assert scope_key("h1", "w1:tD") in state.inventory.tabs


def test_removal_marks_the_snapshot_dirty_even_when_the_event_does_not_translate() -> None:
    source = HerdrSource(cast(HerdrAdapter, cast(Any, object())))
    # Unknown payload shape: no gateway event, but the link must still re-read the snapshot.
    assert source._consume_live_line("tab.closed", {"unexpected": "shape"}) is None
    assert source.take_snapshot_dirty() is True
    assert source.take_snapshot_dirty() is False  # consumed once per batch

    assert source._consume_live_line("layout.updated", {}) is None
    assert source.take_snapshot_dirty() is False
