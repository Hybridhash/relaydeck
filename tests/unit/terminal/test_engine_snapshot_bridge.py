"""Engine-snapshot bridge regressions.

The Select Text sheet must show a snapshot from the ACTUAL emulator
engine (cursor overwrites applied, ANSI consumed), frozen at open time —
never the raw `output_log` bytes. These tests pin the packet pair
(0x09 REQUEST_SNAPSHOT / 0x84 SNAPSHOT_CHUNK) and the Python
request/reassemble path without any Flutter client.
"""

from __future__ import annotations

import asyncio
import struct

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal


def _chunk(req_id: int, total: int, offset: int, data: bytes) -> bytes:
    return protocol.encode_snapshot_chunk(req_id, total, offset, data)


def test_snapshot_request_round_trip() -> None:
    packet = protocol.encode_request_snapshot(0x12345678)
    assert packet == bytes([0x09, 0x12, 0x34, 0x56, 0x78])
    buf = bytearray(packet)
    decoded = protocol.decode_packets(buf)
    assert len(buf) == 0
    assert decoded == [protocol.DecodedPacket(0x09, struct.pack(">I", 0x12345678))]


def test_snapshot_request_partial_tail_held() -> None:
    buf = bytearray(b"\x09\x12\x34")
    assert protocol.decode_packets(buf) == []
    assert bytes(buf) == b"\x09\x12\x34"
    buf += b"\x56\x78"
    decoded = protocol.decode_packets(buf)
    assert decoded == [protocol.DecodedPacket(0x09, struct.pack(">I", 0x12345678))]


def test_snapshot_chunk_round_trip() -> None:
    packet = _chunk(7, 3, 0, b"Xed")
    assert packet[0] == 0x84
    buf = bytearray(packet)
    decoded = protocol.decode_packets(buf)
    assert len(buf) == 0
    assert len(decoded) == 1
    assert decoded[0].kind == 0x84
    req_id, total, offset = struct.unpack(">III", decoded[0].payload[:12])
    assert (req_id, total, offset) == (7, 3, 0)
    assert decoded[0].payload[12:] == b"Xed"


def test_snapshot_chunk_partial_tail_held() -> None:
    packet = _chunk(1, 10, 0, b"0123456789")
    buf = bytearray(packet[:6])
    assert protocol.decode_packets(buf) == []
    buf += packet[6:]
    decoded = protocol.decode_packets(buf)
    assert len(decoded) == 1
    assert decoded[0].payload[12:] == b"0123456789"


def test_snapshot_chunk_oversize_rejected() -> None:
    big = b"x" * (protocol.MAX_WRITE_CHUNK + 1)
    with pytest.raises(ValueError):
        protocol.encode_snapshot_chunk(1, len(big), 0, big)


def _ready_term() -> tuple[RelayDeckTerminal, list[bytes]]:
    term = RelayDeckTerminal()
    sent: list[bytes] = []
    term._test_mark_ready(sent)
    return term, sent


def test_get_snapshot_text_single_chunk() -> None:
    async def scenario() -> None:
        term, sent = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        assert sent == [protocol.encode_request_snapshot(1)]
        term._test_inject_dart_message(_chunk(1, 3, 0, b"Xed"))
        assert await task == "Xed"

    asyncio.run(scenario())


def test_get_snapshot_text_reassembles_out_of_order() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        body = b"scrollback line one\nline two with overwrite Xed"
        assert len(body) > 20
        term._test_inject_dart_message(_chunk(1, len(body), 20, body[20:]))
        assert not task.done()
        term._test_inject_dart_message(_chunk(1, len(body), 0, body[:20]))
        assert await task == body.decode()

    asyncio.run(scenario())


def test_get_snapshot_text_drops_stale_request_id() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(999, 3, 0, b"bog"))
        assert not task.done()
        term._test_inject_dart_message(_chunk(1, 2, 0, b"ok"))
        assert await task == "ok"

    asyncio.run(scenario())


def test_get_snapshot_text_timeout_cleans_up_and_drops_late_chunks() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        with pytest.raises(TimeoutError):
            await term.get_snapshot_text(timeout=0.01)
        # A late burst for the dead request must not leak anywhere.
        term._test_inject_dart_message(_chunk(1, 4, 0, b"late"))
        # And the next request gets a fresh id with no contamination.
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(2, 5, 0, b"fresh"))
        assert await task == "fresh"

    asyncio.run(scenario())


def test_get_snapshot_text_concurrent_requests_both_complete() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        first = asyncio.ensure_future(term.get_snapshot_text())
        second = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(2, 1, 0, b"B"))
        term._test_inject_dart_message(_chunk(1, 1, 0, b"A"))
        assert await first == "A"
        assert await second == "B"

    asyncio.run(scenario())


def test_get_snapshot_text_not_ready_raises_immediately() -> None:
    async def scenario() -> None:
        term = RelayDeckTerminal()
        with pytest.raises(RuntimeError, match="not ready"):
            await term.get_snapshot_text(timeout=5.0)

    asyncio.run(scenario())


def test_snapshot_utf8_split_across_chunks() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        body = "héllo→world".encode()
        term._test_inject_dart_message(_chunk(1, len(body), 0, body[:4]))
        term._test_inject_dart_message(_chunk(1, len(body), 4, body[4:]))
        assert await task == "héllo→world"

    asyncio.run(scenario())


class _AllocGuard:
    """Spy for bytearray(int) construction: records sizes and refuses
    anything above a trip wire, so rejection-without-allocation is
    asserted instead of performed."""

    def __init__(self, trip: int) -> None:
        self.trip = trip
        self.sizes: list[int] = []
        self._real = bytearray

    def __call__(self, *args: object, **kwargs: object) -> bytearray:
        if args and isinstance(args[0], int):
            self.sizes.append(args[0])
            assert args[0] <= self.trip, f"would allocate {args[0]} bytes"
        return self._real(*args, **kwargs)


def _inject_guarded(term: RelayDeckTerminal, packet: bytes) -> _AllocGuard:
    """Feed one packet with allocation spied: the inject path is fully
    synchronous, so any bytearray(int) here is reassembly allocation."""
    import builtins

    guard = _AllocGuard(1024 * 1024)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(builtins, "bytearray", guard)
        term._test_inject_dart_message(packet)
    return guard


def test_snapshot_hostile_total_rejected_without_allocation() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        # The 18-byte prover: accepted by framing, must die on
        # ints with zero reassembly allocation.
        guard = _inject_guarded(term, _chunk(1, 0xFFFFFFFF, 0, b"x"))
        assert guard.sizes == []
        with pytest.raises(RuntimeError, match="snapshot rejected.*total"):
            await task

    asyncio.run(scenario())


def test_snapshot_overrun_offset_rejected_without_allocation() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        guard = _inject_guarded(term, _chunk(1, 4, 99, b"x"))
        assert guard.sizes == []
        with pytest.raises(RuntimeError, match="snapshot rejected.*offset"):
            await task

    asyncio.run(scenario())


def test_snapshot_chunk_overrunning_total_rejected() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        guard = _inject_guarded(term, _chunk(1, 4, 3, b"xy"))
        assert guard.sizes == []
        with pytest.raises(RuntimeError, match="snapshot rejected.*overruns"):
            await task

    asyncio.run(scenario())


def test_snapshot_total_change_mid_stream_rejected() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(1, 10, 0, b"01234"))
        assert not task.done()
        guard = _inject_guarded(term, _chunk(1, 11, 5, b"56789"))
        assert guard.sizes == []
        with pytest.raises(RuntimeError, match="snapshot rejected.*mid-stream"):
            await task

    asyncio.run(scenario())


def test_snapshot_inflight_request_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        term, sent = _ready_term()
        monkeypatch.setattr(protocol, "MAX_SNAPSHOT_INFLIGHT", 2)
        first = asyncio.ensure_future(term.get_snapshot_text())
        second = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="too many in-flight"):
            await term.get_snapshot_text()
        assert len(sent) == 2
        term._test_inject_dart_message(_chunk(1, 1, 0, b"A"))
        term._test_inject_dart_message(_chunk(2, 1, 0, b"B"))
        assert await first == "A"
        assert await second == "B"
        # Capacity released: a third request now sends.
        third = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        assert len(sent) == 3
        term._test_inject_dart_message(_chunk(3, 1, 0, b"C"))
        assert await third == "C"

    asyncio.run(scenario())


def test_snapshot_cumulative_bytes_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        monkeypatch.setattr(protocol, "MAX_SNAPSHOT_INFLIGHT_BYTES", 100)
        # Completion releases its declaration: 60 alone fits.
        first = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(1, 60, 0, b"x" * 60))
        assert await first == "x" * 60
        # Overlapping 60 + 60 breaches the 100 budget: the second
        # declaration is rejected with zero allocation.
        second = asyncio.ensure_future(term.get_snapshot_text())
        third = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term._test_inject_dart_message(_chunk(2, 60, 0, b"y" * 10))
        assert not second.done()
        guard = _inject_guarded(term, _chunk(3, 60, 0, b"z" * 10))
        assert guard.sizes == []
        with pytest.raises(RuntimeError, match="snapshot rejected.*in-flight"):
            await third
        term._test_inject_dart_message(_chunk(2, 60, 10, b"y" * 50))
        assert await second == "y" * 60

    asyncio.run(scenario())


def test_snapshot_reset_aborts_waiter_with_error() -> None:
    async def scenario() -> None:
        term, _ = _ready_term()
        task = asyncio.ensure_future(term.get_snapshot_text())
        await asyncio.sleep(0)
        term.reset()
        with pytest.raises(RuntimeError, match="snapshot aborted"):
            await task
        assert term._snapshot_inflight_bytes == 0

    asyncio.run(scenario())
