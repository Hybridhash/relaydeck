"""Transport-resume rebind regression.

After a session reuse the retained terminal must rebind the SAME
native engine to the replacement UI transport: no viewer-generation
bump (the session reopen path never fires, the controller keeps
running), missing output redelivered exactly once from ACK/journal
offsets, queued packets dropped rather than replayed, no resize, no
takeover. Anything uncertain fails closed as "incomplete" so the
caller drives the honest reset-as-watcher recovery.
"""

from __future__ import annotations

import asyncio

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import RelayDeckTerminal, ResumeOutcome

from tests.support.host_harness import run
from tests.support.terminal.resume import (
    INST,
    OTHER_INST,
    _hello,
    _MuxConn,
    _open_term,
    _report,
    _write_payloads,
)

# -- codec -------------------------------------------------------------


def test_probe_shape_and_range() -> None:
    assert protocol.encode_resume_probe(1) == b"\x0c\x00\x00\x00\x01"
    for bad in (0, -1, 0x100000000, True, "1", None):
        with pytest.raises(ValueError):
            protocol.encode_resume_probe(bad)  # type: ignore[arg-type]


def test_report_round_trip_and_strict_length() -> None:
    pkt = protocol.encode_resume_report(9, INST, 3, 123456)
    assert pkt[0] == protocol.RESUME_REPORT
    assert protocol.decode_resume_report(pkt[1:]) == (9, INST, 3, 123456)
    with pytest.raises(ValueError):
        protocol.decode_resume_report(pkt[1:-1])
    with pytest.raises(ValueError):
        protocol.decode_resume_report(pkt[1:] + b"x")
    with pytest.raises(ValueError):
        protocol.encode_resume_report(1, -1, 0, 0)
    with pytest.raises(ValueError):
        protocol.encode_resume_report(1, INST, 0, 1 << 64)


def test_framing_splits_probe_and_report() -> None:
    buf = bytearray(protocol.encode_resume_probe(2) + protocol.encode_resume_report(2, INST, 0, 40))
    packets = protocol.decode_packets(buf)
    assert [p.kind for p in packets] == [protocol.RESUME_PROBE, protocol.RESUME_REPORT]
    assert len(buf) == 0
    partial = bytearray(protocol.encode_resume_report(3, INST, 0, 1)[:10])
    assert protocol.decode_packets(partial) == []
    assert len(partial) == 10


# -- hello / baseline --------------------------------------------------


def test_hello_records_baseline_and_bad_reports_ignored() -> None:
    async def scenario() -> None:
        app, term, _, channel = await _open_term()
        assert term._native_instance is None
        channel.deliver(_hello(applied=0))
        await app.settle()
        assert term._native_instance == INST
        # Unknown req ids and malformed reports are dropped quietly.
        channel.deliver(_report(99, applied=5))
        channel.deliver(bytes([protocol.RESUME_REPORT]) + b"short")
        await app.settle()
        assert term._native_instance == INST
        assert term._protocol_error is None

    run(scenario())


# -- resume success ----------------------------------------------------


def test_resume_rebinds_same_engine_without_generation_bump() -> None:
    async def scenario() -> None:
        app, term, conn1, ch1 = await _open_term()
        reopened: list[str] = []
        term.add_channel_listener(lambda: reopened.append("reopened"))
        inputs: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: inputs.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"hello ")
        ch1.deliver(protocol.encode_ack(6))
        await app.settle()
        term.write(b"world")  # sent=11, applied stuck at 6: the outage gap
        assert term._sent_bytes == 11 and term._acked_bytes == 6

        # Session reuse: same session, replacement connection, empty mux.
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        assert [c for c, _ in conn2.data_frames] == [7]
        probe = conn2.data_frames[0][1]
        assert probe[0] == protocol.RESUME_PROBE
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=6))
        outcome = await task

        assert outcome == ResumeOutcome("resumed", "ok", 5)
        assert term.viewer_generation == 1, "no fresh-viewer bump"
        assert reopened == [], "session reopen path never fires"
        assert ch1.closed and ch1.handler is None, "old binding fenced"
        # Suffix exactly once, then the live input truth re-asserted,
        # then VERIFIED releasing the Dart fence.
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [
            protocol.RESUME_PROBE,
            protocol.WRITE,
            protocol.INPUT_ENABLED,
            protocol.RESUME_VERIFIED,
        ]
        assert _write_payloads(conn2.data_frames[1:2]) == [b"world"]
        assert conn2.data_frames[2][1] == protocol.encode_input_enabled(False)
        assert (term._sent_bytes, term._acked_bytes) == (11, 6)
        # The rebound channel is live in both directions.
        term.write(b"!")
        assert _write_payloads(conn2.data_frames[4:5]) == [b"!"]
        ch2.deliver(protocol.encode_ack(12))
        await app.settle()
        assert term._acked_bytes == 12
        ch2.deliver(protocol.encode_input(b"z", origin=protocol.ORIGIN_KEY))
        await app.settle()
        assert inputs == [(protocol.ORIGIN_KEY, b"z")]
        # The dead binding delivers nothing anymore.
        ch1.deliver(protocol.encode_input(b"q", origin=protocol.ORIGIN_KEY))
        await app.settle()
        assert inputs == [(protocol.ORIGIN_KEY, b"z")]

    run(scenario())


def test_resume_drops_pending_and_holds_new_writes_in_order() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        ch1.deliver(protocol.encode_ack(2))
        await app.settle()
        term.write(b"cd")  # gap: applied 2, sent 4
        # A pre-attach-style leftover queued packet is dropped, never
        # replayed: the journal suffix subsumes it.
        term._pending_writes.append(protocol.encode_write(b"STALE"))
        term._pending_bytes += len(protocol.encode_write(b"STALE"))

        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        # New output during the probe await is held, still counted.
        term.write(b"ef")
        assert term._sent_bytes == 6
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=2))
        outcome = await task

        assert outcome == ResumeOutcome("resumed", "ok", 2)
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [
            protocol.RESUME_PROBE,
            protocol.WRITE,  # redelivered suffix "cd"
            protocol.WRITE,  # held live bytes "ef", after the suffix
            protocol.INPUT_ENABLED,
            protocol.RESUME_VERIFIED,
        ]
        assert _write_payloads(conn2.data_frames[1:3]) == [b"cd", b"ef"]
        assert b"STALE" not in b"".join(p for _, p in conn2.data_frames)

    run(scenario())


def test_resume_reports_overflow_without_losing_bytes() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"xy")
        term._overflowed = True  # tripped during the outage; session notified
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(1, applied=0))
        outcome = await task
        assert outcome == ResumeOutcome("resumed", "ok_after_overflow", 2)
        assert _write_payloads(conn2.data_frames[1:2]) == [b"xy"]

    run(scenario())


# -- fail-closed negatives ---------------------------------------------


async def _resume_with(report: bytes, *, writes: tuple[bytes, ...] = (b"ab",)) -> ResumeOutcome:
    app, term, _, ch1 = await _open_term()
    ch1.deliver(_hello())
    await app.settle()
    for data in writes:
        term.write(data)
    conn2 = _MuxConn()
    app.session.attach_connection(conn2)
    task = asyncio.ensure_future(term.rebind_resumed())
    await asyncio.sleep(0)
    conn2.channels[7].deliver(report)
    return await task


def test_resume_rejects_foreign_engine() -> None:
    async def scenario() -> None:
        outcome = await _resume_with(_report(1, instance=OTHER_INST, applied=0))
        assert outcome.status == "incomplete" and outcome.reason == "instance_changed"

    run(scenario())


def test_resume_rejects_reset_mismatch() -> None:
    async def scenario() -> None:
        outcome = await _resume_with(_report(1, applied=0, resets=4))
        assert outcome.status == "incomplete" and outcome.reason == "reset_mismatch"

    run(scenario())


def test_resume_rejects_future_and_regressed_offsets() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        ch1.deliver(protocol.encode_ack(2))
        await app.settle()
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)

        async def attempt(applied: int) -> ResumeOutcome:
            task = asyncio.ensure_future(term.rebind_resumed())
            await asyncio.sleep(0)
            req = term._resume_next_id
            conn2.channels[7].deliver(_report(req, applied=applied))
            return await task

        assert (await attempt(3)).reason == "offset_future"
        assert (await attempt(1)).reason == "offset_regressed"

    run(scenario())


def test_resume_rejects_evicted_suffix() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        # Simulate journal eviction past the reported offset.
        term._journal_base = 2
        term._journal.clear()
        term._journal_bytes = 0
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(1, applied=0))
        outcome = await task
        assert outcome.status == "incomplete" and outcome.reason == "offset_evicted"

    run(scenario())


def test_resume_unknown_native_fails_closed_unless_zero_traffic() -> None:
    async def scenario() -> None:
        # Traffic but no hello (lost hello / older engine): closed.
        app, term, _, _ch1 = await _open_term()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(1, applied=0))
        assert (await task).reason == "unknown_native"

        # Zero traffic with no hello: nothing to lose, adopt + resume.
        app2, term2, _, _ = await _open_term()
        conn3 = _MuxConn()
        app2.session.attach_connection(conn3)
        task2 = asyncio.ensure_future(term2.rebind_resumed())
        await asyncio.sleep(0)
        conn3.channels[7].deliver(_report(1, applied=0))
        outcome2 = await task2
        assert outcome2 == ResumeOutcome("resumed", "ok", 0)
        assert term2._native_instance == INST

    run(scenario())


def test_resume_timeout_and_never_attached_and_protocol_error() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        outcome = await term.rebind_resumed(timeout=0.01)
        assert (outcome.status, outcome.reason) == ("incomplete", "probe_timeout")
        # Late reply after a timeout is dropped, not adopted.
        conn2.channels[7].deliver(_report(1, applied=0))
        await app.settle()
        assert term._acked_bytes == 0

        fresh = RelayDeckTerminal()
        assert await fresh.rebind_resumed() == ResumeOutcome("noop", "never_attached")

        term._protocol_error = "framing violated"
        assert (await term.rebind_resumed()).reason == "protocol_error"

    run(scenario())


def test_fresh_capture_supersedes_in_flight_resume() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        reopened: list[str] = []
        term.add_channel_listener(lambda: reopened.append("reopened"))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        # A real remount wins: fresh open supersedes the resume probe.
        await app.set_props_and_fire(
            "r1b-term",
            {},
            "data_channel_open",
            {"channel_name": "terminal", "channel_id": 9},
        )
        assert (await task).reason == "superseded"
        assert term.viewer_generation == 2
        assert reopened == ["reopened"], "fresh-viewer path preserved"
        assert term._native_instance is None, "stale baseline discarded"
        # The late probe reply cannot resurrect the dead binding.
        conn2.channels[7].deliver(_report(1, applied=0))
        await app.settle()
        assert term._sent_bytes == 0

    run(scenario())


def test_second_resume_supersedes_first() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        first = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        second = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        assert (await first).reason == "superseded"
        req = term._resume_next_id
        conn2.channels[7].deliver(_report(req, applied=0))
        assert await second == ResumeOutcome("resumed", "ok", 2)

    run(scenario())


def test_reset_during_resume_supersedes() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        term.reset()
        assert (await task).reason == "superseded"

    run(scenario())
