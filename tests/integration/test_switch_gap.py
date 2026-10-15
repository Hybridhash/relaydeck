"""real gateway ring/ASGI socket, ticket-only production opens."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from relaydeck.client.features.terminal.stream import INCOMPLETE_HISTORY
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.gateway.ring import RING_MAX_AGE_MS, TerminalRing
from tests.support.host_harness import run
from tests.support.terminal.reader_handoff import _actor
from tests.support.terminal.switch_gap import _Renderer, _TicketOnlySocket, _write
from tests.support.terminal.watch_drops import until


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("replay", ["earlier", "overlap", "exact", "evicted"])
def test_retained_control_to_ticket_only_view_replay_has_one_boundary(
    kind: str, replay: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with _actor(kind) as ctx:
            actor, backend, lease = ctx.actor, ctx.backend, ctx.lease
            old_controller = actor.controller
            ctx.readers[0].cancel()
            with suppress(asyncio.CancelledError):
                await ctx.readers[0]
            term = _Renderer()
            actor.bind_view(term, write=_write)
            initial = bytes(actor.output_log)
            fanout = backend.fanout_for(lease.attachment_id)
            assert actor.term_state.applied_offset == len(initial)
            if replay == "overlap":
                # One real ring segment spans the already-rendered cursor.
                fanout.ring = TerminalRing(lease.epoch, ctx.clock.now)
                fanout.ring.append(initial + b"new", ctx.clock.now)
            else:
                if replay == "evicted":
                    backend.publish_output(lease.attachment_id, lease.epoch, b"lost")
                if replay in ("exact", "evicted"):
                    ctx.clock.now += RING_MAX_AGE_MS + 1
                backend.publish_output(lease.attachment_id, lease.epoch, b"new")

            # Deliberately do not append the controller's resume_query here:
            # production_stream_factory currently opens with the ticket only.
            def ticket_only(ticket: str) -> Any:
                socket = _TicketOnlySocket(ctx.factory, ticket)
                ctx.factory.sockets.append(socket)
                return socket

            monkeypatch.setattr(actor, "_socket_factory", ticket_only)
            before = len(ctx.requests)
            assert await watch_only(actor, lambda: True)
            view = actor.lease
            controller = actor.controller
            assert controller is not old_controller and view.mode == "view"
            reader = asyncio.create_task(actor.run())
            ctx.readers.append(reader)

            if replay == "evicted":
                assert await asyncio.wait_for(reader, 2) == "disconnected"
                assert controller.reattach_needed and controller.pending_resume is None
                assert term.resets == 1 and actor.term_state.notice.text == INCOMPLETE_HISTORY
                assert actor.controller is controller and actor.lease is view
                if kind == "agent":
                    assert actor._watch_retry_task is None
                # No automatic acquire/takeover loop: recovery is explicit and
                # reopens this same VIEW at the server's retained base.
                assert await actor.drive_recovery() == "reattached"
                reader = asyncio.create_task(actor.run())
                ctx.readers.append(reader)
                await until(lambda: controller.state.applied_offset == fanout.ring.end_offset)
                assert controller.state.notice.text == INCOMPLETE_HISTORY
            else:
                await until(lambda: controller.state.applied_offset == len(initial) + 3)
                assert not reader.done() and not controller.reattach_needed
                assert controller.pending_resume is None and term.resets == 0
                assert actor.term_state.notice is None
            assert term.output == [b"new"]  # Never re-render an applied prefix.
            assert bytes(actor.output_log) == initial + b"new"
            backend.publish_output(lease.attachment_id, lease.epoch, b"live")
            await until(lambda: term.output == [b"new", b"live"])
            assert actor.current_term is term and actor.controller is controller
            assert actor.lease is view and not actor.keys_enabled
            assert not await actor.resize(30, 70)
            assert not any("/attachments" in path for _, path in ctx.requests[before:])
            assert sum(path.endswith("/leases") for _, path in ctx.requests[before:]) == 1
            assert backend.input_logs == {} and backend.resize_log == []
            if kind == "agent":
                assert actor.store.draft == "retained draft" and actor.takeover_disclosure is None

    run(scenario())


@pytest.mark.parametrize("kind", ["agent", "shell"])
def test_late_view_socket_close_and_view_release_do_not_close_replacement_control(
    kind: str,
) -> None:
    async def scenario() -> None:
        async with _actor(kind) as ctx:
            actor = ctx.actor
            ctx.readers[0].cancel()
            with suppress(asyncio.CancelledError):
                await ctx.readers[0]
            assert await watch_only(actor, lambda: True)
            old_view, old_socket = actor.lease, ctx.factory.sockets[-1]
            if kind == "agent":
                actor.request_takeover()
                assert await actor.confirm_takeover() == "live"
            else:
                assert await actor.take_over() == "live"
            control, controller = actor.lease, actor.controller
            reader = asyncio.create_task(actor.run())
            ctx.readers.append(reader)
            late_view = await ctx.ops.acquire_lease(actor.attachment.attachment_id, "view")
            before = list(ctx.backend.resize_log), dict(ctx.backend.input_logs)
            # Repeat the observed late old-view close/release plus a stale view
            # grant on the NEW generation. Neither can release its control lease.
            await old_socket.close()
            await ctx.ops.release_lease(old_view.lease_id)
            await ctx.ops.release_lease(late_view.lease_id)
            server_lease = ctx.backend.open_lease(control.lease_id)
            assert server_lease is not None
            assert ctx.state.sockets.has_device_lease(server_lease.owner, "dev-1", control.lease_id)
            prior = controller.state.applied_offset
            ctx.backend.publish_output(control.attachment_id, control.epoch, b"still-live")
            await until(lambda: controller.state.applied_offset == prior + len(b"still-live"))
            assert actor.controller is controller and actor.lease is control and not reader.done()
            assert actor.phase == "live" and actor.keys_enabled
            assert (ctx.backend.resize_log, ctx.backend.input_logs) == before

    run(scenario())
