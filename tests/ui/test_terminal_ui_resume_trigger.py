"""UI resume trigger: real session/controller/bridge, scripted PTY and mux."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from relaydeck_terminal import protocol
from relaydeck_terminal.terminal import ResumeOutcome

from relaydeck.client.features.terminal.ui_resume import TerminalUIResumes
from relaydeck.client.features.terminal.warm_watch import watch_only
from tests.support.host_harness import run
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.terminal.resume import _hello, _MuxConn, _open_term, _report, _write_payloads


async def setup(kind: str) -> tuple[Any, ...]:
    scripted = make_scripted_sessions()
    if kind == "agent":
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="e1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        session.on_draft_change("exact unsent\ndraft")
    else:
        session = scripted.factory.make_shell(host_id="mac")
        await session.open("shell")
    app, term, _, channel = await _open_term()
    channel.deliver(_hello())
    await app.settle()
    session.bind_view(term, write=lambda t, data: (t.write(data), True)[1])
    session.controller._set(replace(session.controller.state, status="live"))
    term.set_input_enabled(True)
    term.write(b"seen")
    channel.deliver(protocol.encode_ack(4))
    await app.settle()
    term.write(b"missing")
    conn = _MuxConn()
    app.session.attach_connection(conn)
    return scripted, session, app, term, conn, TerminalUIResumes(), asyncio.Event()


async def until(check: Any) -> None:
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0)


def probes(conn: Any) -> list[bytes]:
    return [p for _, p in conn.data_frames if p[0] == protocol.RESUME_PROBE]


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_trigger_resumes_exact_output_and_fences_all_writers(kind: str) -> None:
    async def scenario() -> None:
        scripted, session, app, term, conn, manager, closed = await setup(kind)
        controller, lease, attachment = session.controller, session.lease, session.attachment
        before = (
            len(scripted.ops.attaches),
            len(scripted.ops.leases),
            len(scripted.streams.opened),
        )
        generation, resets, token = term.viewer_generation, term._reset_count, session.input_token
        transport = object()
        try:
            task = manager.start(session, transport=transport, current=lambda: True, closed=closed)
            assert task is not None
            assert (
                manager.start(session, transport=transport, current=lambda: True, closed=closed)
                is task
            )
            assert controller.key_generation() is None and controller.paste_generation() is None
            assert controller.wants_resize(40, 20) is None
            if kind == "agent":
                assert await session.on_send() == "ignored"
                assert await session.composer_callbacks()["on_send"]() == "ignored"
                assert session.composer_props()["draft"] == "exact unsent\ndraft"
            await until(lambda: probes(conn))
            assert len(probes(conn)) == 1
            conn.channels[7].deliver(_report(1, applied=4))
            assert await task == ResumeOutcome("resumed", "ok", 7)
            writes = [frame for frame in conn.data_frames if frame[1][0] == protocol.WRITE]
            assert _write_payloads(writes) == [b"missing"]
            assert conn.data_frames[-1][1] == protocol.encode_resume_verified()
            assert controller.key_generation() == controller.generation
            assert term.viewer_generation == generation and term._reset_count == resets
            assert (
                session.controller is controller
                and session.lease is lease
                and session.attachment is attachment
            )
            assert session.input_token == token
            assert (
                manager.start(session, transport=transport, current=lambda: True, closed=closed)
                is task
            )
            assert len(probes(conn)) == 1
            assert (
                len(scripted.ops.attaches),
                len(scripted.ops.leases),
                len(scripted.streams.opened),
            ) == before
            assert scripted.submitted == [] and scripted.prompted == []
            assert all(kind != "bytes" for kind, _ in scripted.streams.opened[-1][2].sent)
            if kind == "agent":
                assert session.composer_props()["draft"] == "exact unsent\ndraft"
        finally:
            await manager.close()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize(
    "transition",
    ["leave", "pause", "logout", "viewer", "reset", "epoch", "ui-close", "replacement"],
)
def test_pending_trigger_cannot_reset_a_successor(kind: str, transition: str) -> None:
    async def scenario() -> None:
        scripted, session, app, term, conn, manager, closed = await setup(kind)
        controller = session.controller
        try:
            task = manager.start(session, transport=object(), current=lambda: True, closed=closed)
            assert task is not None
            await until(lambda: probes(conn))
            if transition == "leave":
                session.set_foreground(False)
            elif transition == "pause":
                await session.pause()
            elif transition == "logout":
                await manager.close()
            elif transition == "viewer":
                await app.set_props_and_fire(
                    "r1b-term",
                    {},
                    "data_channel_open",
                    {"channel_name": "terminal", "channel_id": 9},
                )
            elif transition == "reset":
                term.reset(reason="stream_reset")
                session._notify()
            elif transition == "epoch":
                controller._set(replace(controller.state, epoch="replacement-stream"))
            elif transition == "ui-close":
                closed.set()
            else:
                new_conn = _MuxConn()
                app.session.attach_connection(new_conn)
                winner = manager.start(
                    session, transport=object(), current=lambda: True, closed=asyncio.Event()
                )
                assert winner is not None and winner is not task
                await until(lambda: probes(new_conn))
                new_conn.channels[7].deliver(_report(2, applied=4))
                assert (await winner).status == "resumed"
                assert session.controller is controller and controller.key_generation() is not None
            await asyncio.gather(task, return_exceptions=True)
            if transition != "replacement":
                assert protocol.RESUME_VERIFIED not in [p[0] for _, p in conn.data_frames]
            assert not manager._active
            if transition == "replacement":
                assert term._reset_count == 0 and term.viewer_generation == 1
        finally:
            await manager.close()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize(
    "failure",
    [
        "probe_timeout",
        "instance_changed",
        "protocol_error",
        "lease-expired",
        "superseded",
        "overflow",
    ],
)
def test_mapping_requires_explicit_recovery_and_preserves_overflow(
    kind: str, failure: str, monkeypatch: Any
) -> None:
    async def scenario() -> None:
        scripted, session, app, term, conn, manager, closed = await setup(kind)
        controller, resets = session.controller, term._reset_count
        gate = asyncio.Event()

        async def result(*, timeout: float, current: Any) -> ResumeOutcome:
            await gate.wait()
            if failure == "lease-expired":
                session._renewal.extend(0.0)
                assert current() is False
                return ResumeOutcome("incomplete", "superseded")
            if failure == "overflow":
                assert current() is True
                return ResumeOutcome("resumed", "ok_after_overflow")
            return ResumeOutcome("incomplete", failure)

        monkeypatch.setattr(term, "rebind_resumed", result)
        try:
            task = manager.start(session, transport=object(), current=lambda: True, closed=closed)
            assert task is not None
            gate.set()
            assert await task is not None
            if failure == "superseded":
                assert session.controller is controller and term._reset_count == resets
            elif failure == "overflow":
                assert session.controller is controller and controller.live_edge_pending
                assert not controller.reattach_needed
                assert controller.key_generation() is None
            else:
                assert session.controller is None and "reattach explicitly" in session.notice
                assert term._reset_count == resets
            assert len(scripted.ops.attaches) == 1 and len(scripted.ops.leases) == 1
            assert scripted.submitted == [] and scripted.prompted == []
        finally:
            await manager.close()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_final_guard_rechecks_identity_and_preserves_paused_writer(kind: str) -> None:
    async def scenario() -> None:
        scripted, session, app, term, conn, manager, closed = await setup(kind)
        controller, valid = session.controller, [True]
        try:
            task = manager.start(
                session, transport=object(), current=lambda: valid[0], closed=closed
            )
            assert task is not None
            await until(lambda: probes(conn))
            valid[0] = False
            conn.channels[7].deliver(_report(1, applied=4))
            assert await task is None
            assert session.controller is controller and term._reset_count == 0
            assert not any(p[0] == protocol.RESUME_VERIFIED for _, p in conn.data_frames)
            assert controller.key_generation() is None
            valid[0] = True
            controller.pause_writer()
            second = manager.start(session, transport=object(), current=lambda: True, closed=closed)
            await until(lambda: len(probes(conn)) == 2)
            conn.channels[7].deliver(_report(2, applied=4))
            assert (await second).status == "resumed"
            assert controller.writer_paused and controller.key_generation() is None
            assert not term.input_enabled and term._reset_count == 0
        finally:
            await manager.close()
            await session.dispose()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("eligibility", ["view", "expired", "revoked"])
def test_real_bridge_never_restores_expired_revoked_or_view_control(
    kind: str, eligibility: str
) -> None:
    async def scenario() -> None:
        scripted, session, app, term, conn, manager, closed = await setup(kind)
        try:
            if eligibility == "view":
                assert await watch_only(session, lambda: True)
                session.controller._set(replace(session.controller.state, status="live"))
                term.set_input_enabled(
                    True
                )  # Even an obsolete/misconfigured prop must fail closed.
            controller = session.controller
            before = (len(scripted.ops.attaches), len(scripted.ops.leases))
            task = manager.start(session, transport=object(), current=lambda: True, closed=closed)
            assert task is not None
            await until(lambda: probes(conn))
            if eligibility == "expired":
                session._renewal.extend(0.0)
            elif eligibility == "revoked":
                controller.invalidate_writer("forbidden")
            conn.channels[7].deliver(
                _report(1, resets=term._reset_count, applied=term._acked_bytes)
            )
            result = await task
            assert controller.key_generation() is None and not term.input_enabled
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            if eligibility == "view":
                assert result.status == "resumed" and session.controller is controller
                assert controller.mode == session.lease.mode == "view"
                assert controller.wants_resize(40, 20) is None
            else:
                assert result.status == "incomplete" and session.controller is None
                assert not any(p[0] == protocol.RESUME_VERIFIED for _, p in conn.data_frames)
                assert "reattach explicitly" in session.notice
        finally:
            await manager.close()
            await session.dispose()

    run(scenario())
