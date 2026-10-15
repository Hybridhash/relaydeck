"""Reconnect and resume regressions.

1. Held packets must not outlive their resume/viewer — overlapping
resumes must not duplicate output, capture/reset must retire held bytes,
and held control storage must be bounded (coalescing idempotent state).
2. Scheduled channel callbacks must validate the actual current
binding (channel identity + sync generation): old-binding queued
callbacks retire, and an ordinary reset must not orphan the live channel.
3. The transport-loss fence latches through a positively completed
resume — Python gates inbound INPUT/SIZE_REPORT/ACK during the probe and
after failure until verified success, explicit reset, or fresh capture;
success emits RESUME_VERIFIED (0x0D) last so Dart unlatches.
"""

from __future__ import annotations

import asyncio

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import ResumeOutcome, TerminalOverflowError

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


def _held_bytes(term) -> int:
    packets = term._resume_held
    total = 0
    for entry in packets:
        total += len(entry[1] if isinstance(entry, tuple) else entry)
    return total


def _frames_of(conn, channel_id, kind) -> list:
    return [f for f in conn.data_frames if f[0] == channel_id and f[1][0] == kind]


# -- Held-batch ownership ------------------------------------------


def test_overlapping_resume_with_writes_sends_suffix_once() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        first = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        term.write(b"cd")  # held by the first attempt, counted in sent
        second = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(term._resume_next_id, applied=0))
        outcome = await second
        assert (await first).reason == "superseded"
        assert outcome.status == "resumed" and outcome.resent_bytes == 4
        writes = _write_payloads(_frames_of(conn2, 7, protocol.WRITE))
        assert writes == [b"abcd"], "suffix exactly once, no held re-flush"

    run(scenario())


def test_overlapping_resume_partial_applied_no_duplicate() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        first = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        term.write(b"cd")
        second = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(term._resume_next_id, applied=2))
        outcome = await second
        assert (await first).reason == "superseded"
        assert outcome.status == "resumed" and outcome.resent_bytes == 2
        writes = _write_payloads(_frames_of(conn2, 7, protocol.WRITE))
        assert writes == [b"cd"], "displayed ab stays displayed, cd once"

    run(scenario())


def test_fresh_capture_discards_held_output() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        term.write(b"OLD-HELD")
        await app.set_props_and_fire(
            "r1b-term",
            {},
            "data_channel_open",
            {"channel_name": "terminal", "channel_id": 9},
        )
        assert (await task).reason == "superseded"
        assert _frames_of(conn2, 9, protocol.WRITE) == [], (
            "stale hold never crosses the viewer boundary"
        )
        # The fresh viewer is live with clean counters.
        term.write(b"live")
        assert _write_payloads(_frames_of(conn2, 9, protocol.WRITE)) == [b"live"]
        assert (term._sent_bytes, term._acked_bytes) == (4, 0)

    run(scenario())


def test_reset_discards_held_output() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        term.write(b"OLD-HELD")
        term.reset()
        assert (await task).reason == "superseded"
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [protocol.RESUME_PROBE, protocol.RESET], "no stale flush after reset"
        term.write(b"new")
        assert (term._sent_bytes, term._acked_bytes) == (3, 0)
        conn2.channels[7].deliver(protocol.encode_ack(3))
        await app.settle()
        assert term._acked_bytes == 3

    run(scenario())


def test_held_control_coalesces_idempotent_state() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        for _ in range(530000):
            term.set_input_enabled(False)
        assert len(term._resume_held) == 1, "input truth coalesces to latest"
        assert _held_bytes(term) < 100
        conn2.channels[7].deliver(_report(1, applied=0))
        assert (await task).status == "resumed"
        ie_frames = _frames_of(conn2, 7, protocol.INPUT_ENABLED)
        assert len(ie_frames) == 1, "one input truth, not 530k replays"
        assert ie_frames[0][1] == protocol.encode_input_enabled(False)

    run(scenario())


def test_held_control_storage_is_bounded() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        term._channel.deliver(_hello())
        await app.settle()
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        with pytest.raises(TerminalOverflowError):
            for _ in range(600000):
                term.scroll_viewport(1)
        assert _held_bytes(term) <= protocol.VIEWER_QUEUE_LIMIT
        conn2.channels[7].deliver(_report(1, applied=0))
        assert (await task).status == "resumed"

    run(scenario())


# -- Binding validity ------------------------------------------------


def test_queued_old_binding_callbacks_retire() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        # Queue callbacks on the old binding, then rebind before they run:
        # the task is scheduled first so the channel swap wins the race.
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        ch1.deliver(protocol.encode_input(b"OLD-QUEUED"))
        ch1.deliver(protocol.encode_ack(2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert seen == [], "old-binding input retires"
        assert term._acked_bytes == 0, "old-binding ack retires"
        conn2.channels[7].deliver(_report(1, applied=0))
        assert (await task).status == "resumed"
        assert seen == []
        assert term._acked_bytes == 0

    run(scenario())


def test_post_resume_reset_keeps_live_channel() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=0))
        assert (await task).status == "resumed"
        term.reset()
        term.write(b"new")
        ch2.deliver(protocol.encode_ack(3))
        ch2.deliver(protocol.encode_input(b"AFTER-RESET"))
        await app.settle()
        assert term._acked_bytes == 3, "live channel ack survives reset"
        assert seen == [(protocol.ORIGIN_TEXT, b"AFTER-RESET")]

    run(scenario())


def test_queued_pre_reset_callbacks_retire_without_orphaning() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        ch1.deliver(protocol.encode_ack(5))
        ch1.deliver(protocol.encode_input(b"PRE-RESET"))
        term.reset()
        await app.settle()
        assert term._acked_bytes == 0, "stale ack cannot corrupt the new base"
        assert seen == [], "stale input retires at the sync boundary"
        # The same binding stays live for fresh traffic.
        term.write(b"new")
        ch1.deliver(protocol.encode_ack(3))
        ch1.deliver(protocol.encode_input(b"POST-RESET"))
        await app.settle()
        assert term._acked_bytes == 3
        assert seen == [(protocol.ORIGIN_TEXT, b"POST-RESET")]

    run(scenario())


# -- Verification fence ----------------------------------------------


def test_input_and_size_fenced_during_probe_and_after_failure() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        sizes: list[tuple[int, int]] = []
        modes: list[bool] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        term.add_size_listener(lambda c, r: sizes.append((c, r)))
        term.add_mode_listener(modes.append)
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(protocol.encode_input(b"NEW-UNVERIFIED"))
        ch2.deliver(protocol.encode_size_report(100, 30))
        ch2.deliver(protocol.encode_mode_report(True))
        await app.settle()
        assert seen == [], "unverified input never dispatches"
        assert sizes == [], "unverified size never applies"
        assert modes == [True], "read-only engine reflections still flow"
        ch2.deliver(_report(1, instance=OTHER_INST, applied=0))
        assert (await task).reason == "instance_changed"
        ch2.deliver(protocol.encode_input(b"LATE"))
        ch2.deliver(protocol.encode_size_report(111, 31))
        await app.settle()
        assert seen == [] and sizes == [], "failure keeps the fence up"
        # Explicit resync releases: traffic flows again.
        term.reset()
        ch2.deliver(protocol.encode_input(b"OK"))
        ch2.deliver(protocol.encode_size_report(80, 24))
        await app.settle()
        assert seen == [(protocol.ORIGIN_TEXT, b"OK")]
        assert sizes == [(80, 24)]

    run(scenario())


def test_verified_packet_sent_last_on_success_only() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        ch1.deliver(protocol.encode_ack(2))
        await app.settle()
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(1, applied=2))
        assert (await task) == ResumeOutcome("resumed", "ok", 0)
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [
            protocol.RESUME_PROBE,
            protocol.INPUT_ENABLED,
            protocol.RESUME_VERIFIED,
        ], "verified releases Dart input only after display + truth sync"

        # A failed resume never releases the Dart fence.
        app2, term2, _, chb = await _open_term()
        chb.deliver(_hello())
        await app2.settle()
        term2.write(b"ab")
        conn3 = _MuxConn()
        app2.session.attach_connection(conn3)
        task2 = asyncio.ensure_future(term2.rebind_resumed())
        await asyncio.sleep(0)
        conn3.channels[7].deliver(_report(1, instance=OTHER_INST, applied=0))
        assert (await task2).reason == "instance_changed"
        assert protocol.RESUME_VERIFIED not in [p[0] for _, p in conn3.data_frames]

    run(scenario())


def test_fence_releases_on_fresh_capture() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        conn2.channels[7].deliver(_report(1, instance=OTHER_INST, applied=0))
        assert (await task).reason == "instance_changed"
        await app.set_props_and_fire(
            "r1b-term",
            {},
            "data_channel_open",
            {"channel_name": "terminal", "channel_id": 9},
        )
        conn2.channels[9].deliver(protocol.encode_input(b"FRESH-OK"))
        await app.settle()
        assert seen == [(protocol.ORIGIN_TEXT, b"FRESH-OK")]

    run(scenario())


def test_success_releases_fence_for_current_input() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        sizes: list[tuple[int, int]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        term.add_size_listener(lambda c, r: sizes.append((c, r)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(protocol.encode_input(b"DURING-PROBE"))
        await app.settle()
        assert seen == []
        ch2.deliver(_report(1, applied=0))
        assert (await task).status == "resumed"
        ch2.deliver(protocol.encode_input(b"AFTER-OK", origin=protocol.ORIGIN_KEY))
        ch2.deliver(protocol.encode_size_report(80, 24))
        await app.settle()
        assert seen == [(protocol.ORIGIN_KEY, b"AFTER-OK")], "only current authorized input flows"
        assert sizes == [(80, 24)]

    run(scenario())


def test_verified_packet_framing() -> None:
    assert protocol.encode_resume_verified() == b"\x0d"
    buf = bytearray(b"\x0d")
    packets = protocol.decode_packets(buf)
    assert [(p.kind, p.payload) for p in packets] == [(protocol.RESUME_VERIFIED, b"")]
    assert len(buf) == 0
    assert INST != OTHER_INST  # identities used above differ


# -- Protocol-error fencing + hello-less reset -----------------


def test_preexisting_protocol_error_fences_queued_input() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        # A real parser error, then input queued behind it.
        ch1.deliver(bytes([0xFE]))
        await app.settle()
        assert term._protocol_error is not None
        ch1.deliver(protocol.encode_input(b"QUEUED-AFTER-ERROR"))
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        outcome = await term.rebind_resumed()
        await app.settle()
        assert outcome == ResumeOutcome("incomplete", "protocol_error")
        assert term._resume_fence is True, "attached terminal fences before return"
        assert seen == [], "queued input cannot dispatch through the fence"
        assert conn2.data_frames == [], "no probe on a poisoned stream"
        # Explicit reset cannot rehabilitate a disposed connection.
        term.reset()
        assert term._protocol_error is None
        ch1.deliver(protocol.encode_input(b"OK"))
        await app.settle()
        assert seen == []
        assert term.rebind_transport()
        term.reset()  # authoritative reset is delivered on the current binding
        conn2.channels[7].deliver(protocol.encode_input(b"OK"))
        await app.settle()
        assert seen == [(protocol.ORIGIN_TEXT, b"OK")]

    run(scenario())


def test_error_during_probe_fails_closed() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(bytes([0xFE]))
        await app.settle()
        assert term._protocol_error is not None
        # An otherwise matching report must not override failed stream state.
        ch2.deliver(_report(1, applied=0))
        outcome = await task
        assert outcome == ResumeOutcome("incomplete", "protocol_error", 0)
        kinds = [p[0] for _, p in conn2.data_frames]
        assert protocol.RESUME_VERIFIED not in kinds, "no VERIFIED on poisoned stream"
        assert term._resume_fence is True
        ch2.deliver(protocol.encode_input(b"LATE"))
        ch2.deliver(protocol.encode_size_report(80, 24))
        await app.settle()
        assert seen == [], "fence holds after the failed attempt"

    run(scenario())


def test_missing_hello_reset_mismatch_fails_closed() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        # No hello, zero traffic — but a foreign reset identity.
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, resets=999, applied=0))
        outcome = await task
        assert outcome == ResumeOutcome("incomplete", "reset_mismatch", 0)
        assert term._native_instance is None, "adoption commits only after full validation"
        kinds = [p[0] for _, p in conn2.data_frames]
        assert protocol.RESUME_VERIFIED not in kinds
        assert term._resume_fence is True
        ch2.deliver(protocol.encode_input(b"LATE"))
        await app.settle()
        assert seen == []

    run(scenario())


def test_current_guard_false_yields_superseded() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        calls: list[str] = []

        def guard() -> bool:
            calls.append("called")
            return False

        task = asyncio.ensure_future(term.rebind_resumed(current=guard))
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=0))
        outcome = await task
        assert outcome == ResumeOutcome("incomplete", "superseded", 0)
        assert calls == ["called"], "guard runs once, after all checks pass"
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [protocol.RESUME_PROBE], "no suffix, no truth, no VERIFIED"
        assert term._resume_fence is True
        assert (term._sent_bytes, term._acked_bytes) == (2, 0), "counters untouched"
        assert term._native_instance == INST
        ch2.deliver(protocol.encode_input(b"LATE"))
        await app.settle()
        assert seen == []

        # Existing checks still win over the guard: a mismatched
        # report fails closed with its own reason, guard unconsulted.
        app2, term2, _, chb = await _open_term()
        chb.deliver(_hello())
        await app2.settle()
        term2.write(b"ab")
        conn3 = _MuxConn()
        app2.session.attach_connection(conn3)
        task2 = asyncio.ensure_future(term2.rebind_resumed(current=guard))
        await asyncio.sleep(0)
        conn3.channels[7].deliver(_report(1, instance=OTHER_INST, applied=0))
        assert (await task2).reason == "instance_changed"
        assert calls == ["called"], "guard runs only after checks pass"

    run(scenario())


def test_current_guard_true_keeps_success() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed(current=lambda: True))
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=0))
        assert (await task) == ResumeOutcome("resumed", "ok", 2)
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [
            protocol.RESUME_PROBE,
            protocol.WRITE,
            protocol.INPUT_ENABLED,
            protocol.RESUME_VERIFIED,
        ]
        assert term._resume_fence is False
        ch2.deliver(protocol.encode_input(b"OK", origin=protocol.ORIGIN_KEY))
        await app.settle()
        assert seen == [(protocol.ORIGIN_KEY, b"OK")]

    run(scenario())


def test_current_guard_raise_stays_fenced() -> None:
    async def scenario() -> None:
        app, term, _, ch1 = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        ch1.deliver(_hello())
        await app.settle()
        term.write(b"ab")
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)

        def guard() -> bool:
            raise RuntimeError("trigger lost ownership")

        task = asyncio.ensure_future(term.rebind_resumed(current=guard))
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, applied=0))
        outcome = await task
        assert outcome == ResumeOutcome("incomplete", "superseded", 0)
        kinds = [p[0] for _, p in conn2.data_frames]
        assert protocol.RESUME_VERIFIED not in kinds
        assert term._resume_fence is True
        ch2.deliver(protocol.encode_input(b"LATE"))
        await app.settle()
        assert seen == []

    run(scenario())


def test_missing_hello_matching_reset_adopts() -> None:
    async def scenario() -> None:
        app, term, _, _ = await _open_term()
        seen: list[tuple[int, bytes]] = []
        term.add_input_listener(lambda origin, data: seen.append((origin, data)))
        conn2 = _MuxConn()
        app.session.attach_connection(conn2)
        task = asyncio.ensure_future(term.rebind_resumed())
        await asyncio.sleep(0)
        ch2 = conn2.channels[7]
        ch2.deliver(_report(1, resets=0, applied=0))
        assert (await task) == ResumeOutcome("resumed", "ok", 0)
        assert term._native_instance == INST
        kinds = [p[0] for _, p in conn2.data_frames]
        assert kinds == [protocol.RESUME_PROBE, protocol.INPUT_ENABLED, protocol.RESUME_VERIFIED]
        assert term._resume_fence is False
        ch2.deliver(protocol.encode_input(b"OK", origin=protocol.ORIGIN_KEY))
        await app.settle()
        assert seen == [(protocol.ORIGIN_KEY, b"OK")]

    run(scenario())
