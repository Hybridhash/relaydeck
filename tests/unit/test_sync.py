"""backoff, SSE parser, sync controller, terminal client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from relaydeck.client.features.sync.backoff import Backoff
from relaydeck.client.features.sync.controller import (
    SyncController,
    TerminalStreamClient,
    snapshot_from_wire,
)
from relaydeck.client.features.sync.sse import SseFrame, SseParser
from relaydeck.client.features.sync.transport import (
    TerminalSocket,
    TransportError,
)
from relaydeck.domain import reducers
from relaydeck.domain.models import AppState, ConnectionState
from tests.support.sync.scripted_sync import FIXTURES, _meta, _ScriptedTransport


def test_backoff_seeded_deterministic() -> None:
    first = Backoff.seeded(7)
    second = Backoff.seeded(7)
    seq_a = [first.next_delay_ms() for _ in range(6)]
    seq_b = [second.next_delay_ms() for _ in range(6)]
    assert seq_a == seq_b
    assert 375 <= seq_a[0] <= 625
    assert seq_a[1] > seq_a[0] // 2  # growth dominates jitter
    big = Backoff.seeded(1)
    delays = [big.next_delay_ms() for _ in range(20)]
    assert max(delays) <= 37500  # capped at 30s + jitter
    big.reset()
    assert big.attempts == 0


def test_sse_split_and_comments() -> None:
    parser = SseParser()
    frames = parser.feed(b": comment\r\nevent: met")
    assert frames == []
    frames = parser.feed(b'adata\ndata: {"a": 1}\n')
    assert frames == []
    frames = parser.feed(b"data: line2\n\n")
    assert frames == [SseFrame("metadata", '{"a": 1}\nline2')]
    frames = parser.feed(b'event: error\ndata: {"code": "x"}\n\n')
    assert frames == [SseFrame("error", '{"code": "x"}')]


def test_controller_snapshot_stream_stop() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([_meta(1), _meta(2)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3))
        seen: list[str] = []
        controller.subscribe(lambda s: seen.append(s.connection.state.value))
        state = await controller.run("h1", stop=lambda: state_done(controller))
        assert state.connection.state == ConnectionState.LIVE
        assert state.connection.last_sequence == 2
        assert "live" in seen

    def state_done(controller: SyncController) -> bool:
        return controller.state.connection.last_sequence >= 2

    asyncio.run(scenario())


def test_controller_reconnects_after_drop() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([_meta(1), TransportError("host_offline", "drop")])
        transport.scripts.append([_meta(1), _meta(2)])
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=sleep)
        states: list[str] = []
        controller.subscribe(lambda s: states.append(s.connection.state.value))
        await controller.run(
            "h1",
            stop=lambda: controller.state.connection.last_sequence >= 2
            or controller.state.connection.state == ConnectionState.RECONNECTING
            and len(sleeps) >= 1
            and controller.state.connection.last_sequence >= 0
            and _enough(controller),
        )
        assert transport.snapshots == 2
        assert len(sleeps) == 1
        assert "reconnecting" in states

    def _enough(controller: SyncController) -> bool:
        return controller.state.connection.last_sequence >= 2

    asyncio.run(scenario())


def test_controller_revokes_on_unauthorized() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([TransportError("unauthenticated", "no", False)])
        controller = SyncController(transport=transport)
        state = await controller.run("h1", stop=lambda: transport.finished)
        assert state.revoked is True
        assert state.connection.state == ConnectionState.ACCESS_REVOKED

    asyncio.run(scenario())


def test_controller_gap_triggers_resync() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([_meta(5)])  # gap: last_sequence is 0
        transport.scripts.append([_meta(1)])
        controller = SyncController(transport=transport, backoff=Backoff.seeded(3), sleep=_nosleep)
        state = await controller.run(
            "h1", stop=lambda: controller.state.connection.last_sequence >= 1
        )
        assert transport.snapshots == 2
        assert state.connection.last_sequence == 1

    async def _nosleep(_: float) -> None:
        pass

    asyncio.run(scenario())


class _ScriptedSocket(TerminalSocket):
    def __init__(self, inbound: list[tuple[str, Any]]) -> None:
        self.inbound = list(inbound)
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> tuple[str, Any]:
        if not self.inbound:
            raise TransportError("host_offline", "eof")
        await asyncio.sleep(0)
        return self.inbound.pop(0)

    async def close(self) -> None:
        self.closed = True


def test_terminal_client_output_input_ack() -> None:
    from relaydeck.gateway import terminal_wire

    async def scenario() -> None:
        seen_tickets: list[str] = []

        def factory(ticket: str) -> _ScriptedSocket:
            seen_tickets.append(ticket)
            return sock

        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"hi")),
                ("bytes", terminal_wire.encode_output("e1", 2, b"!")),
                ("text", json.dumps({"kind": "bye"})),
            ]
        )
        outputs: list[tuple[int, bytes]] = []
        gaps: list[str] = []
        client = TerminalStreamClient(
            socket_factory=factory,
            on_output=lambda offset, data: outputs.append((offset, data)),
            on_control=lambda doc: None,
            on_gap=gaps.append,
        )
        await client.open("lease-1", 4, "rdt_test")
        await client.send_input(b"x")
        await client.send_key("ctrl+c")
        await client.resize(100, 30)
        await client.run()
        assert outputs == [(0, b"hi"), (2, b"!")]
        assert gaps == []
        assert seen_tickets == ["rdt_test"]
        kinds = [terminal_wire.decode_input(frame) for frame in sock.sent_bytes]
        assert [(c.generation, c.input_seq, c.kind) for c in kinds] == [
            (4, 1, "text"),
            (4, 2, "key"),
            (4, 3, "resize"),
        ]
        assert kinds[0].data == b"x"
        assert kinds[1].data == b"ctrl+c"
        assert (kinds[2].cols, kinds[2].rows) == (100, 30)

    asyncio.run(scenario())


def test_terminal_client_offset_gap_and_resume() -> None:
    from relaydeck.gateway import terminal_wire

    async def scenario() -> None:
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"a")),
                ("bytes", terminal_wire.encode_output("e1", 9, b"skip")),
            ]
        )
        gaps: list[str] = []
        client = TerminalStreamClient(
            socket_factory=lambda ticket: sock,
            on_output=lambda offset, data: None,
            on_control=lambda doc: None,
            on_gap=gaps.append,
        )
        await client.open("lease-1", 1, "rdt_test")
        await client.run()
        assert len(gaps) == 1
        assert client.resume_offset() == 1

    asyncio.run(scenario())


def test_terminal_client_epoch_change_gaps() -> None:
    from relaydeck.gateway import terminal_wire

    async def scenario() -> None:
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"a")),
                ("bytes", terminal_wire.encode_output("e2", 1, b"b")),
            ]
        )
        gaps: list[str] = []
        client = TerminalStreamClient(
            socket_factory=lambda ticket: sock,
            on_output=lambda offset, data: None,
            on_control=lambda doc: None,
            on_gap=gaps.append,
        )
        await client.open("lease-1", 1, "rdt_test")
        await client.run()
        assert gaps == ["epoch e1 -> e2"]

    asyncio.run(scenario())


def test_terminal_client_control_gap() -> None:
    async def scenario() -> None:
        sock = _ScriptedSocket([("text", json.dumps({"kind": "gap", "detail": "stale_revision"}))])
        gaps: list[str] = []
        client = TerminalStreamClient(
            socket_factory=lambda ticket: sock,
            on_output=lambda offset, data: None,
            on_control=lambda doc: None,
            on_gap=gaps.append,
        )
        await client.open("lease-1", 1, "rdt_test")
        await client.run()
        assert gaps == ["stale_revision"]

    asyncio.run(scenario())


def test_terminal_client_rate_limited_gap_keeps_output_and_never_replays_input() -> None:
    from relaydeck.gateway import terminal_wire

    async def scenario() -> None:
        gap = {"kind": "gap", "detail": "rate_limited"}
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"before")),
                *[("text", json.dumps(gap)) for _ in range(50)],
                ("bytes", terminal_wire.encode_output("e1", 6, b"after")),
                ("text", json.dumps({"kind": "bye"})),
            ]
        )
        outputs, controls, gaps = [], [], []
        client = TerminalStreamClient(
            socket_factory=lambda _: sock,
            on_output=lambda offset, data: outputs.append((offset, data)),
            on_control=controls.append,
            on_gap=gaps.append,
        )
        await client.open("lease-1", 1, "ticket")
        await client.send_input(b"dropped")
        await asyncio.wait_for(client.run(), 1)
        assert gaps == []
        assert outputs == [(0, b"before"), (6, b"after")]
        assert controls == [*[gap] * 50, {"kind": "bye"}]
        assert client.next_offset == 11
        assert [terminal_wire.decode_input(frame).data for frame in sock.sent_bytes] == [b"dropped"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["control", "view"])
def test_terminal_controller_rate_limited_gap_does_not_schedule_recovery(mode: str) -> None:
    from relaydeck.client.features.terminal.controller import TerminalController
    from relaydeck.gateway import terminal_wire

    async def scenario() -> None:
        sock = _ScriptedSocket(
            [
                ("bytes", terminal_wire.encode_output("e1", 0, b"a")),
                ("text", json.dumps({"kind": "gap", "detail": "rate_limited"})),
                ("bytes", terminal_wire.encode_output("e1", 1, b"b")),
                ("text", json.dumps({"kind": "bye"})),
            ]
        )
        output, states = [], []
        controller = TerminalController(
            lambda _: sock, "lease-1", 1, mode, output.append, states.append
        )
        await controller.start("ticket")
        await asyncio.wait_for(controller.run(), 1)
        assert output == [b"a", b"b"]
        assert controller.pending_resume is None and not controller.reattach_needed
        assert controller.gap_detail == "" and controller.gap_recovery.attempt == 0
        assert not controller.writer_revoked and not controller.ui_input_fenced
        assert not sock.sent_bytes and not sock.closed

    asyncio.run(scenario())


def test_controller_disconnects_on_terminal_error_without_wipe() -> None:
    """Non-retryable non-auth envelopes end the run WITHOUT revoking sessions."""

    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([TransportError("not_found", "unknown host", False)])
        controller = SyncController(transport=transport)
        state = await controller.run("h1", stop=lambda: transport.finished)
        assert state.revoked is False
        assert state.connection.state == ConnectionState.DISCONNECTED
        assert state.connection.detail == "not_found"
        assert transport.snapshots == 1

    asyncio.run(scenario())


def test_snapshot_carries_host_protocol_version() -> None:
    """Wire snapshot host block lands on the domain Host (protocol /
    version / capabilities). Found by a live browser run: diagnostics
    showed "protocol 0 /" because the parser dropped the block."""
    doc = json.loads((FIXTURES / "snapshot.json").read_text())
    snapshot = snapshot_from_wire(doc)
    assert snapshot.host_protocol == 22
    assert snapshot.host_version == "0.9.1"
    assert "terminal.view" in snapshot.host_capabilities
    state = reducers.apply_snapshot(AppState(), snapshot)
    host = state.inventory.hosts["home-mac"]
    assert host.protocol == 22
    assert host.version == "0.9.1"
    assert "terminal.control" in host.capabilities


def test_controller_does_not_retry_upstream_incompatible() -> None:
    async def scenario() -> None:
        transport = _ScriptedTransport()
        transport.scripts.append([TransportError("upstream_incompatible", "bad dialect", False)])
        controller = SyncController(transport=transport)
        state = await controller.run("h1", stop=lambda: transport.finished)
        assert state.connection.state == ConnectionState.DISCONNECTED
        assert transport.snapshots == 1

    asyncio.run(scenario())
