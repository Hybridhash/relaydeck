"""Nothing stale may touch the control lease after Take over.

Owner timeline: leave (retained watch) -> Take over -> a late watch recovery
and then the server closing the new control stream. Real gateway ring/ASGI
socket, ticket-only production opens, no live Herdr.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

import pytest

from relaydeck.client.features.terminal import agent_session
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.gateway.ring import TerminalRing
from tests.support.host_harness import run
from tests.support.terminal.reader_handoff import _actor
from tests.support.terminal.switch_gap import _Renderer, _TicketOnlySocket, _write


async def _start_overlapping_watch(ctx: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, bytes]:
    actor, backend, lease = ctx.actor, ctx.backend, ctx.lease
    ctx.readers[0].cancel()
    with suppress(asyncio.CancelledError):
        await ctx.readers[0]
    term = _Renderer()
    actor.bind_view(term, write=_write)
    initial = bytes(actor.output_log)
    fanout = backend.fanout_for(lease.attachment_id)
    fanout.ring = TerminalRing(lease.epoch, ctx.clock.now)
    fanout.ring.append(initial + b"new", ctx.clock.now)  # replay overlaps the cursor

    def ticket_only(ticket: str) -> Any:
        socket = _TicketOnlySocket(ctx.factory, ticket)
        ctx.factory.sockets.append(socket)
        return socket

    monkeypatch.setattr(actor, "_socket_factory", ticket_only)
    assert await watch_only(actor, lambda: True)
    ctx.readers.append(asyncio.create_task(actor.run()))
    return term, initial


def _lease_posts(ctx: Any, since: int) -> int:
    return sum(
        1 for method, path in ctx.requests[since:] if method == "POST" and path.endswith("/leases")
    )


def _releases(ctx: Any, since: int) -> list[str]:
    return [path for method, path in ctx.requests[since:] if method == "DELETE"]


def test_takeover_after_overlapping_watch_leaves_control_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0.05, 0.05, 0.05, 0.05, 0.05))

    async def scenario() -> None:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            await _start_overlapping_watch(ctx, monkeypatch)
            view = actor.lease
            assert view.mode == "view" and not actor.controller.reattach_needed
            actor.request_takeover()
            await actor.confirm_takeover()
            control, lease = actor.controller, actor.lease
            assert lease.mode == "control" and lease.lease_id != view.lease_id
            ctx.readers.append(asyncio.create_task(actor.run()))
            mark = len(ctx.requests)
            await asyncio.sleep(0.6)  # past every patched retry/backoff window
            assert actor.controller is control and actor.lease is lease
            assert actor.phase == "live" and actor.keys_enabled
            assert _lease_posts(ctx, mark) == 0 and _releases(ctx, mark) == []
            assert ctx.state.sockets.has_device_lease("acct-1", "dev-1", lease.lease_id)

    run(scenario())


def test_takeover_while_dropped_watch_is_backing_off_never_acquires_or_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0.3, 0.3, 0.3, 0.3, 0.3))

    async def scenario() -> None:
        async with _actor("agent") as ctx:
            actor = ctx.actor
            await _start_overlapping_watch(ctx, monkeypatch)
            view = actor.lease
            # The server drops the watch stream: recovery sleeps.
            await ctx.state.sockets.close_lease(view.lease_id, "stale_revision")
            for _ in range(100):
                if actor._watch_retry_task is not None:
                    break
                await asyncio.sleep(0.01)
            assert actor._watch_retry_task is not None
            retry = actor._watch_retry_task
            actor.request_takeover()
            await actor.confirm_takeover()
            control, lease = actor.controller, actor.lease
            ctx.readers.append(asyncio.create_task(actor.run()))
            mark = len(ctx.requests)
            await asyncio.sleep(1.0)  # well past the 0.3 s backoff
            assert retry.done()
            assert actor.controller is control and actor.lease is lease
            assert lease.mode == "control" and actor.phase == "live"
            assert _lease_posts(ctx, mark) == 0 and _releases(ctx, mark) == []
            assert ctx.state.sockets.has_device_lease("acct-1", "dev-1", lease.lease_id)

    run(scenario())
