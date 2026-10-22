"""Bounded identical metadata-gap recovery; strict contiguity stays intact."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from relaydeck.client.features.sync.controller import SyncController, snapshot_from_wire
from relaydeck.client.features.sync.sse import SseFrame
from relaydeck.client.features.sync.transport import HttpxGatewayTransport, TransportError
from relaydeck.contracts.cursors import AFTER_SEQUENCE_HEADER
from relaydeck.domain import reducers
from relaydeck.domain.models import ConnectionState
from tests.support.sync.scripted_sync import _meta, _ScriptedTransport, _snapshot_doc


def test_identical_gap_stops_after_three_snapshots_and_two_delays() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[SseFrame("stream_open", ""), _meta(5)]] * 10
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)

        controller = SyncController(transport, sleep=sleep)
        state = await asyncio.wait_for(controller.run("h1"), 1)
        assert state.connection.state is ConnectionState.DISCONNECTED
        assert state.connection.detail == "gap_stalled"
        assert state.stale and state.needs_resync and not state.revoked
        assert transport.snapshots == 3 and len(transport.cursors) == 3
        assert len(delays) == 2 and controller._gap_failures == 3
        assert controller.gap_stalled_at("h1")
        assert not controller.gap_stalled_at("another-host")
        # An explicit guarded new run can heal; no automatic fourth retry.
        transport.scripts = [[_meta(1)]]
        state = await controller.run(
            "h1", stop=lambda: controller.state.connection.last_sequence == 1
        )
        assert state.connection.state is ConnectionState.LIVE and not state.stale
        assert controller._gap_failures == 0 and transport.snapshots == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["host", "epoch", "cursor"])
def test_gap_stall_blocks_only_the_identical_boundary(changed: str) -> None:
    controller = SyncController(_ScriptedTransport())
    controller.state = reducers.apply_snapshot(
        controller.state, snapshot_from_wire(_snapshot_doc())
    )
    for _ in range(3):
        controller._gap_stalled("h1", "gap")
    controller.state = reducers.set_connection(
        controller.state, ConnectionState.DISCONNECTED, "gap_stalled"
    )
    assert controller.gap_stalled_at("h1")
    if changed == "host":
        assert not controller.gap_stalled_at("h2")
    elif changed == "epoch":
        inventory = controller.state.inventory
        hosts = {**inventory.hosts, "h1": replace(inventory.hosts["h1"], epoch="new-epoch")}
        controller.state = replace(controller.state, inventory=replace(inventory, hosts=hosts))
        assert not controller.gap_stalled_at("h1")
    else:
        controller.state = replace(
            controller.state, connection=replace(controller.state.connection, cursor="new-cursor")
        )
        assert not controller.gap_stalled_at("h1")


def test_different_boundaries_and_non_gap_errors_reset_the_cap() -> None:
    controller = SyncController(_ScriptedTransport())
    for cursor in ("c0", "c1", "c2"):
        controller.state = replace(
            controller.state, connection=replace(controller.state.connection, cursor=cursor)
        )
        assert not controller._gap_stalled("h1", "gap")
        assert not controller._gap_stalled("h1", "gap")
    assert not controller._gap_stalled("h1", "host_offline")
    assert controller._gap_failures == 0
    assert not controller._gap_stalled("h1", "gap")
    assert controller._gap_failures == 1


def test_replaying_a_contiguous_prefix_cannot_reset_identical_gap_cap() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts = [[_meta(1), _meta(3)]] * 10

        async def sleep(delay: float) -> None:
            pass

        controller = SyncController(transport, sleep=sleep)
        state = await controller.run("h1", stop=lambda: transport.snapshots >= 6)
        assert state.connection.detail == "gap_stalled"
        assert transport.snapshots == 3
        assert state.connection.cursor == "c1" and state.connection.last_sequence == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [-1, True, "3", None])
def test_bad_stream_boundary_fails_closed(value: Any) -> None:
    controller = SyncController(_ScriptedTransport())
    frame = SseFrame(
        "stream_open", json.dumps({"host_id": "h1", "cursor": "c0", "sequence": value})
    )
    with pytest.raises(TransportError, match="bad stream boundary"):
        controller._accept_stream_open(frame, "h1", "c0")
    assert controller.state.connection.state is not ConnectionState.LIVE


@pytest.mark.parametrize("header", ["-1", "+3", "3.0", "", "true"])
def test_invalid_http_boundary_cannot_mark_a_stream_live(header: str) -> None:
    async def scenario() -> None:
        async def token() -> str:
            return "synthetic-token"

        mock = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={AFTER_SEQUENCE_HEADER: header}, content=b""
            )
        )
        async with httpx.AsyncClient(transport=mock) as http:
            transport = HttpxGatewayTransport("https://gateway.test", token, http)
            stream = transport.stream("h1", "c0")
            try:
                with pytest.raises(TransportError, match="bad stream boundary"):
                    await anext(stream)
            finally:
                await stream.aclose()

    asyncio.run(scenario())


def test_stream_boundary_cannot_roll_back_progress_or_revocation() -> None:
    controller = SyncController(_ScriptedTransport())
    controller.state = reducers.apply_snapshot(
        controller.state, snapshot_from_wire(_snapshot_doc())
    )
    frame = SseFrame("stream_open", json.dumps({"host_id": "h1", "cursor": "c0", "sequence": 3}))
    controller._accept_stream_open(frame, "h1", "c0")
    controller._apply_frame(_meta(4))
    controller._accept_stream_open(frame, "h1", "c0")
    assert controller.state.connection.last_sequence == 4
    controller._set(reducers.revoke(controller.state))
    controller._accept_stream_open(frame, "h1", "c0")
    assert controller.state.revoked
    assert controller.state.connection.state is ConnectionState.ACCESS_REVOKED


def test_nonzero_boundary_does_not_swallow_a_real_missing_event() -> None:
    controller = SyncController(_ScriptedTransport())
    controller.state = reducers.apply_snapshot(
        controller.state, snapshot_from_wire(_snapshot_doc())
    )
    frame = SseFrame("stream_open", json.dumps({"host_id": "h1", "cursor": "c0", "sequence": 3}))
    controller._accept_stream_open(frame, "h1", "c0")
    with pytest.raises(TransportError, match="gap: gap"):
        controller._apply_frame(_meta(5))
    assert controller.state.connection.last_sequence == 3 and controller.state.needs_resync
