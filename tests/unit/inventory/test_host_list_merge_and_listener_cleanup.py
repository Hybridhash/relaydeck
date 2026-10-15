"""Per-host list merge + controller listener cleanup."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import SyncController
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import GatewayTransport, TransportError
from relaydeck.domain import reducers
from relaydeck.domain.models import (
    Agent,
    AppState,
    ConnectionState,
    EventType,
    MetadataEvent,
    Selection,
    Snapshot,
    Tab,
)
from relaydeck.domain.reducers import apply_event, apply_snapshot, merge_hosts

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "gateway"


def test_merge_hosts_adds_unknown_and_preserves_synced_caps() -> None:
    merged = merge_hosts(
        AppState(),
        [
            {
                "host_id": "h1",
                "label": "H1",
                "epoch": "e1",
                "reachable": True,
                "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
            },
            {"host_id": "", "label": "blank"},
        ],
    )
    assert set(merged.inventory.hosts) == {"h1"}
    host = merged.inventory.hosts["h1"]
    assert (host.label, host.epoch, host.reachable, host.protocol, host.version) == (
        "H1",
        "e1",
        True,
        22,
        "0.9.1",
    )
    assert host.capabilities == frozenset()
    synced = replace(
        merged,
        inventory=replace(
            merged.inventory,
            hosts={
                "h1": replace(host, capabilities=frozenset({"terminal.control"}), last_seen_ms=5)
            },
        ),
    )
    again = merge_hosts(
        synced,
        [
            {
                "host_id": "h1",
                "label": "H1b",
                "epoch": "e2",
                "reachable": False,
                "upstream": {},
            }
        ],
    )
    refreshed = again.inventory.hosts["h1"]
    assert refreshed.capabilities == frozenset({"terminal.control"})
    assert refreshed.last_seen_ms == 5
    assert (refreshed.label, refreshed.epoch, refreshed.reachable) == ("H1b", "e2", False)


class _FakeTransport(GatewayTransport):
    async def me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def hosts(self) -> dict[str, Any]:
        raise NotImplementedError

    async def snapshot(self, host_id: str) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads((FIXTURES / "snapshot.json").read_text())
        doc["host"]["id"] = host_id
        return doc

    async def agents(
        self,
        host_id: str,
        limit: int = 50,
        page_token: str = "",
        *,
        project: str = "",
        status: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def stream(self, host_id: str, cursor: str) -> AsyncGenerator[SseFrame]:
        for _ in ():
            yield SseFrame("metadata", "{}")

    async def submit_command(self, command: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def command_status(self, host_id: str, command_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def terminal_ticket(self, lease_id: str) -> dict[str, Any]:
        raise NotImplementedError


def _host_entry(host_id: str) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "label": host_id,
        "epoch": f"{host_id}-e1",
        "reachable": True,
        "upstream": {"protocol": 22, "version": "0.9.1", "compatible": True},
    }


def _granted_state() -> AppState:
    """Two granted hosts with slices, selection on h2's terminal."""
    state = merge_hosts(AppState(), [_host_entry("h1"), _host_entry("h2")])
    inventory = replace(
        state.inventory,
        tabs={
            "term-1": Tab(
                id="term-1",
                host_id="h1",
                workspace_id="w1",
                label="term-1",
            ),
            "term-2": Tab(
                id="term-2",
                host_id="h2",
                workspace_id="w2",
                label="term-2",
            ),
        },
        agents={
            "a2": Agent(
                terminal_id="a2",
                host_id="h2",
                workspace_id="w2",
                tab_id="term-2",
                pane_id="p2",
                name="a2",
            ),
        },
    )
    return replace(
        state,
        inventory=inventory,
        selection=Selection(
            host_id="h2", session_key="term-2", terminal_kind="shell", generation=7
        ),
    )


def test_merge_hosts_empty_listing_revokes_everything() -> None:
    """An empty /v1/hosts listing is the complete authorized set: no
    host, slice, or selection survives; the cleared selection bumps its
    generation so in-flight input for the revoked target is dropped."""
    before = _granted_state()
    assert set(before.inventory.hosts) == {"h1", "h2"}
    merged = merge_hosts(before, [])
    assert merged.inventory.hosts == {}
    assert merged.inventory.tabs == {}
    assert merged.inventory.agents == {}
    assert merged.inventory.workspaces == {}
    assert merged.inventory.panes == {}
    assert merged.selection.host_id == ""
    assert merged.selection.session_key == ""
    assert merged.selection.generation == 8
    # The input is never mutated.
    assert set(before.inventory.hosts) == {"h1", "h2"}


def test_merge_hosts_partial_removal_keeps_granted_slices() -> None:
    """Losing h2's grant drops h2's host/slices and the h2 selection;
    h1's inventory is untouched."""
    merged = merge_hosts(_granted_state(), [_host_entry("h1")])
    assert set(merged.inventory.hosts) == {"h1"}
    assert set(merged.inventory.tabs) == {"term-1"}
    assert merged.inventory.agents == {}
    assert merged.selection.host_id == ""
    assert merged.selection.generation == 8


def test_merge_hosts_ordinary_listing_preserves_selection() -> None:
    """A listing that keeps every grant changes nothing about the
    selection: drafts/switches survive ordinary /v1/hosts refreshes."""
    before = _granted_state()
    merged = merge_hosts(before, [_host_entry("h1"), _host_entry("h2")])
    assert merged.selection == before.selection
    assert set(merged.inventory.tabs) == {"term-1", "term-2"}


def test_merge_hosts_revoked_selection_host_keeps_granted_selection() -> None:
    """A selection on a still-granted host survives another host's
    removal without a generation bump."""
    before = replace(
        _granted_state(),
        selection=Selection(
            host_id="h1", session_key="term-1", terminal_kind="shell", generation=7
        ),
    )
    merged = merge_hosts(before, [_host_entry("h1")])
    assert merged.selection == before.selection
    assert set(merged.inventory.hosts) == {"h1"}


def test_snapshot_for_ungranted_host_is_rejected_whole() -> None:
    """A snapshot for a host outside the verified grant set returns the
    identical state: stale data can never resurrect revoked inventory."""
    state = merge_hosts(_granted_state(), [_host_entry("h1")])
    assert state.granted_hosts == frozenset({"h1"})
    stale = Snapshot(host_id="h2", host_epoch="h2-e9", cursor="c-stale")
    assert apply_snapshot(state, stale) is state


def test_event_for_ungranted_host_is_rejected() -> None:
    state = merge_hosts(_granted_state(), [_host_entry("h1")])
    conn = replace(state.connection, host_id="h2")
    state = replace(state, connection=conn)
    event = MetadataEvent(
        event_id="e1",
        host_id="h2",
        host_epoch="h2-e9",
        sequence=1,
        cursor="c1",
        type=EventType.HOST_STATUS,
    )
    kept, action = apply_event(state, event)
    assert kept is state
    assert action == "ignored_type"


def test_unknown_grants_accept_all_hosts() -> None:
    """Before any listing is fetched (granted_hosts None), snapshots
    keep legacy accept-all behavior."""
    state = _granted_state()
    assert state.granted_hosts == frozenset({"h1", "h2"})
    fresh = AppState()
    assert fresh.granted_hosts is None
    snap = Snapshot(host_id="h9", host_epoch="e1", cursor="c")
    assert apply_snapshot(fresh, snap).inventory.hosts["h9"].epoch == "e1"


class _ForbiddenTransport(_FakeTransport):
    async def snapshot(self, host_id: str) -> dict[str, Any]:
        raise TransportError("forbidden", "denied", False)


def test_forbidden_on_ungranted_host_stays_per_host() -> None:
    """A gateway denial for a host the verified listing dropped is
    per-host disconnect (detail host_revoked), never account-wide
    revocation: remaining grants keep working."""

    async def scenario() -> None:
        controller = SyncController(transport=_ForbiddenTransport(), backoff=Backoff.seeded(1))
        controller.apply_grants([_host_entry("h2")])
        await controller.run("h1")
        assert controller.state.revoked is False
        assert controller.state.connection.state == ConnectionState.DISCONNECTED
        assert controller.state.connection.detail == "host_revoked"
        assert controller.state.granted_hosts == frozenset({"h2"})

    asyncio.run(scenario())


def test_forbidden_without_grants_revokes_account() -> None:
    """Without a verified listing the denial cannot be scoped, so the
    legacy global revocation stands."""

    async def scenario() -> None:
        controller = SyncController(transport=_ForbiddenTransport(), backoff=Backoff.seeded(1))
        await controller.run("h1")
        assert controller.state.revoked is True
        assert controller.state.connection.state == ConnectionState.ACCESS_REVOKED

    asyncio.run(scenario())


def test_revoked_lineage_rejects_late_grants_snapshots_events_selects() -> None:
    """Once revoked, in-flight pre-revocation
    grant/snapshot/event/select results cannot undo the clearing. Fresh
    authentication starts a new controller lineage explicitly."""

    async def scenario() -> None:
        controller = SyncController(transport=_FakeTransport(), backoff=Backoff.seeded(1))
        controller.apply_grants([_host_entry("h1")])
        assert set(controller.state.inventory.hosts) == {"h1"}
        controller._set(reducers.revoke(controller.state))
        assert controller.state.revoked is True
        assert controller.state.inventory.hosts == {}
        controller.apply_grants([_host_entry("h1")])
        assert controller.state.inventory.hosts == {}
        assert controller.state.granted_hosts is None
        snap = Snapshot(host_id="h1", host_epoch="h1-e2", cursor="c-late")
        assert apply_snapshot(controller.state, snap) is controller.state
        event = MetadataEvent(
            event_id="e9",
            host_id="h1",
            host_epoch="h1-e2",
            sequence=9,
            cursor="c9",
            type=EventType.HOST_STATUS,
        )
        kept, action = apply_event(controller.state, event)
        assert kept is controller.state
        assert action == "ignored_type"
        controller.select("h1", "term-1", "shell")
        assert controller.state.selection.session_key == ""
        assert controller.state.revoked is True

    asyncio.run(scenario())


def test_controller_unsubscribe_stops_listener() -> None:
    async def scenario() -> None:
        controller = SyncController(transport=_FakeTransport(), backoff=Backoff.seeded(1))
        seen1: list[str] = []
        seen2: list[str] = []
        first = lambda s: seen1.append(s.connection.state.value)  # noqa: E731
        second = lambda s: seen2.append(s.connection.state.value)  # noqa: E731
        controller.subscribe(first)
        controller.subscribe(second)
        controller.unsubscribe(first)
        # A live subscription now reconnects on EOF; this listener-only
        # probe ends explicitly after snapshot delivery instead of treating EOF
        # as successful completion.
        await asyncio.wait_for(
            controller.run("h1", stop=lambda: bool(controller.state.inventory.hosts)), timeout=1
        )
        assert seen1 == []
        assert "synchronizing" in seen2
        assert "live" not in seen2  # snapshot-only probe does not open a stream

    asyncio.run(scenario())
