"""real Herdr 0.9.3 live lines (underscore wire names, nested created/updated
objects) are translated, stamped by the gateway, parsed by the client, and applied by the reducer.
Replays tests/fixtures/upstream/live_tab_lifecycle.jsonl (cwd redacted); no network, no live Herdr.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from relaydeck.client.features.inventory.components.models import terminal_rows
from relaydeck.client.features.inventory.selectors import space_rows
from relaydeck.client.features.sync.controller import event_from_wire
from relaydeck.connector import source as source_module
from relaydeck.connector.herdr import HerdrAdapter, parse_event
from relaydeck.connector.source import _EVENT_MAP, HerdrSource
from relaydeck.contracts.schemas import validate_metadata_event
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, Snapshot, Workspace, scope_key
from relaydeck.gateway.backend import prepare_event

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upstream"
SECRET = b"s" * 32
NOW = 1_700_000_000_000


def _schema_names() -> set[str]:
    schema = json.loads((FIXTURES / "herdr-0.9.1-schema.json").read_text())
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "const" and isinstance(value, str):
                    names.add(value)
                elif key == "enum" and isinstance(value, list):
                    names.update(item for item in value if isinstance(item, str))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return names


def _source(clock: Any = None) -> HerdrSource:
    adapter = cast(HerdrAdapter, cast(Any, object()))
    return HerdrSource(adapter, retry_clock=clock) if clock else HerdrSource(adapter)


def _host_state(epoch: str) -> AppState:
    snapshot = Snapshot(
        host_id="h1",
        host_epoch=epoch,
        cursor="c0",
        workspaces=(Workspace(id="w2", host_id="h1", label="Space Alpha"),),
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


def _shell_count(state: AppState, workspace: str = "w2") -> int:
    return {r.key: r.count for r in space_rows(state, shells=True)}[scope_key("h1", workspace)]


def _lines() -> list[bytes]:
    return (FIXTURES / "live_tab_lifecycle.jsonl").read_bytes().splitlines()


def test_every_mapped_event_has_its_underscore_wire_twin_in_the_schema() -> None:
    names = _schema_names()
    for dotted in _EVENT_MAP:
        twin = dotted.replace(".", "_", 1)
        assert twin in names, f"{dotted} has no wire twin {twin} in the pinned schema"


def test_captured_live_lines_drive_the_terminals_list_and_counts() -> None:
    source = _source()
    state = _host_state(source.epoch)
    created, pane, status, closed = _lines()
    assert _shell_count(state) == 0

    state = _push(source, state, 1, created)
    assert [entry.row.title for entry in terminal_rows(state, "", "", frozenset())] == ["rd-probe"]
    assert _shell_count(state) == 1

    state = _push(source, state, 2, pane)
    assert state.inventory.panes[scope_key("h1", "w2:pD")].tab_id == "w2:tD"

    state = _push(source, state, 3, status)
    assert state.inventory.panes[scope_key("h1", "w2:pD")].agent_status.value == "blocked"

    state = _push(source, state, 4, closed)
    assert scope_key("h1", "w2:tD") not in state.inventory.tabs
    assert scope_key("h1", "w2:pD") not in state.inventory.panes
    assert terminal_rows(state, "", "", frozenset()) == ()
    assert _shell_count(state) == 0
    assert source.take_snapshot_dirty() is True


def test_skipped_event_count_is_logged_at_most_once_a_minute(caplog: Any) -> None:
    now = [0.0]
    source = _source(clock=lambda: now[0])
    caplog.set_level(logging.INFO, logger=source_module.LOG.name)
    for _ in range(3):
        assert source._consume_live_line("layout_updated", {}) is None
    records = [r for r in caplog.records if "herdr_events_skipped" in r.getMessage()]
    assert len(records) == 1
    assert "payload" not in records[0].getMessage()

    now[0] = 61.0
    source._consume_live_line("layout_updated", {})
    records = [r for r in caplog.records if "herdr_events_skipped" in r.getMessage()]
    assert len(records) == 2
