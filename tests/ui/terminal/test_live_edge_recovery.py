"""Production resource/coordinator notifications drive overflow without reacquiring control."""

import asyncio
import json

import pytest
from flet.controls.context import _context_page
from relaydeck_terminal import protocol

from relaydeck.client.features.terminal.ui_resume import terminal_transition
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.contracts.wire import terminal as terminal_wire
from tests.support.host_harness import run
from tests.support.terminal.resume import _report
from tests.support.terminal.retained_transport import mounted, requests, until


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("origin", ["server", "native"])
@pytest.mark.parametrize("mode", ["control", "view"])
def test_overflow_notification_drives_production_same_lease_reader(kind, origin, mode):
    async def scenario():
        scripted, session, entry, resources, app, term, channel = await mounted(kind)
        conn = app.session.connection
        _context_page.set(app.page)
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        entry.task = None
        if mode == "view":
            assert await watch_only(session, lambda: True)
        controller, lease, attachment = session.controller, session.lease, session.attachment
        before = len(scripted.ops.attaches), len(scripted.ops.leases)
        opened = len(scripted.streams.opened)
        entry.supervise(session)
        old_task = entry.task
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: True, closed=asyncio.Event()
            )
            await until(lambda: requests(conn))
            channel.deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            stream = scripted.streams.opened[-1][2]
            if origin == "server":
                stream.queue(("text", json.dumps({"kind": "reset", "detail": "overflow"})))
            else:
                term._sent_bytes = protocol.VIEWER_QUEUE_LIMIT
                stream.queue(("bytes", terminal_wire.encode_output(lease.epoch, 0, b"x")))
            await until(lambda: len(scripted.streams.opened) == opened + 1)
            assert old_task.done() and entry.task is not old_task
            assert "live_edge=1" in scripted.streams.resumes[-1]
            assert controller.ui_input_fenced and controller._overflow_attempts == 1
            replay = scripted.streams.opened[-1][2]
            replay.queue(
                (
                    "text",
                    json.dumps(
                        {
                            "kind": "history_reset",
                            "epoch": lease.epoch,
                            "offset": 100,
                            "incomplete": True,
                        }
                    ),
                )
            )
            await until(lambda: term._reset_count == 1)
            # Real bridge RESET/RESUME_PROBE must be acknowledged before input releases.
            await until(lambda: requests(conn)[-1][1] > 1)
            last = 1
            async with asyncio.timeout(3):
                while controller.live_edge_pending or controller.ui_input_fenced:
                    for number, request in requests(conn):
                        if request > last:
                            last = request
                            conn.channels[number].deliver(
                                _report(request, instance=123, resets=1, applied=0)
                            )
                    await asyncio.sleep(0.001)
            assert session.controller is controller and session.lease is lease
            assert session.attachment is attachment and session.current_term is term
            assert controller.key_generation() == (lease.generation if mode == "control" else None)
            assert controller.gap_recovery.detail != "overflow"
            assert session.gap_recovery.attempt == session.gap_recovery.foreground_attempts == 0
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            assert len(scripted.streams.opened) == opened + 1
            assert not scripted.submitted and not scripted.prompted
            assert not any(
                k == "bytes" for _, _, sock in scripted.streams.opened for k, _ in sock.sent
            )
            if kind == "agent":
                assert session.store.draft == "keep this draft"
        finally:
            await resources.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("loss", ["identity", "leave", "takeover"])
def test_production_overflow_recovery_rechecks_ownership_after_ticket_wait(kind, loss):
    async def scenario():
        scripted, session, entry, resources, app, term, channel = await mounted(kind)
        _context_page.set(app.page)
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        entry.task = None
        entry.supervise(session)
        entered, release = asyncio.Event(), asyncio.Event()
        authorized = [True]
        original = session._ops.terminal_ticket
        controller, lease = session.controller, session.lease
        before = len(scripted.ops.attaches), len(scripted.ops.leases)
        token = None

        async def held_ticket(lease_id):
            entered.set()
            await release.wait()
            return await original(lease_id)

        session._ops.terminal_ticket = held_ticket
        try:
            resources.resume_terminals(
                transport=object(), current=lambda: authorized[0], closed=asyncio.Event()
            )
            conn = app.session.connection
            await until(lambda: requests(conn))
            channel.deliver(_report(requests(conn)[-1][1], instance=123))
            await until(lambda: term.transport_verified)
            scripted.streams.opened[-1][2].queue(
                ("text", json.dumps({"kind": "reset", "detail": "overflow"}))
            )
            await asyncio.wait_for(entered.wait(), 2)
            if loss == "identity":
                authorized[0] = False
            elif loss == "leave":
                session.set_foreground(False)
            else:
                token = terminal_transition(session).begin(session)
            release.set()
            await until(lambda: not entry.ui_recovering)
            assert session.controller is controller and session.lease is lease
            assert len(scripted.streams.opened) == 1 and controller.ui_input_fenced
            assert (len(scripted.ops.attaches), len(scripted.ops.leases)) == before
            assert not scripted.submitted and not scripted.prompted
            assert not any(k == "bytes" for k, _ in scripted.streams.opened[0][2].sent)
        finally:
            release.set()
            if token is not None:
                terminal_transition(session).end(session, token)
            await resources.close()

    run(scenario())
