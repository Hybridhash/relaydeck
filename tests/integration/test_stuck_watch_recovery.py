"""real ASGI watch stream versus source attach-death boundary."""

import asyncio
import json
from contextlib import suppress

import pytest
from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.terminal import agent_session
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.gateway.backend import BackendError
from tests.support.host_harness import run
from tests.support.terminal.attach_targets import _DeadSession, _routing_source
from tests.support.terminal.reader_handoff import _actor, _wait


@pytest.mark.parametrize("attach_died", [False, True])
def test_watch_socket_drop_resumes_but_dead_attach_stays_explicit(monkeypatch, attach_died):
    monkeypatch.setattr(agent_session, "WATCH_RETRY_DELAYS_S", (0, 0, 0, 0, 0))

    async def scenario():
        async with _actor("agent") as ctx:
            actor = ctx.actor
            # This test's ASGI client adapter does not synthesize a receive EOF
            # on local close, unlike production websockets. Stop its old reader
            # before changing leases, then supervise the real new WS handler.
            ctx.readers[0].cancel()
            with suppress(asyncio.CancelledError):
                await ctx.readers[0]
            term = RelayDeckTerminal()
            actor.bind_view(term, write=lambda *_: True)
            assert await watch_only(actor, lambda: True)
            lease, controller = actor.lease, actor.controller
            assert lease.mode == "view"
            reader = asyncio.create_task(actor.run())
            ctx.readers.append(reader)
            await _wait(
                lambda: ctx.state.sockets.has_device_lease("acct-1", "dev-1", lease.lease_id)
            )
            before = len(ctx.requests)
            if attach_died:
                # Exercise the real source death/reset producer. Its reset is
                # applied with the same backend/notify/close sequence as the
                # authenticated connector intake (no connector login here).
                source, _ = _routing_source()
                source._epochs = {lease.terminal_id: lease.epoch}
                source._attached = {
                    "agent:" + lease.terminal_id: (_DeadSession(), lease.terminal_id, "")
                }
                source.pump_attached(lambda _: lease.epoch)
                assert source.drain_terminal_resets() == {lease.terminal_id: lease.epoch}
                dead = ctx.backend.reset_terminal(
                    "home-mac", lease.terminal_id, lease.epoch, "acct-1"
                )
                for lease_id in dead:
                    await ctx.state.sockets.notify_lease(
                        lease_id, json.dumps({"kind": "reset", "detail": "stale_epoch"})
                    )
                    await ctx.state.sockets.close_lease(lease_id, "stale_revision")
                assert await asyncio.wait_for(reader, 2) == "disconnected"
                assert actor.controller is controller and controller.reattach_needed
                assert ctx.requests[before:] == []  # No silent view acquire/takeover.
                assert actor._watch_retry_task is None
                with pytest.raises(BackendError) as caught:
                    ctx.backend.renew_lease(lease.lease_id, "acct-1", "dev-1")
                assert caught.value.code == "stale_revision"
                actor._on_renewal_terminal("lease_expired")
                assert controller.writer_revoked
                assert (
                    ctx.backend.get_attachment(lease.attachment_id).generation != lease.generation
                )
                await _wait(
                    lambda: not ctx.state.sockets.has_device_lease(
                        "acct-1", "dev-1", lease.lease_id
                    )
                )
                await _wait(lambda: ctx.backend.fanout_for(lease.attachment_id).viewer_count == 0)
                response = await ctx.http.get("/v1/me", headers=await ctx.ops._auth())
                assert response.status_code == 200
                assert (await ctx.http.get("/healthz")).status_code == 200
            else:
                old_socket = ctx.factory.sockets[-1]
                await old_socket.socket.outbound.put(
                    {"type": "websocket.close", "code": 1006, "reason": "synthetic transport loss"}
                )
                await old_socket.socket.close()
                await _wait(
                    lambda: actor.controller is not controller
                    and actor.phase == "live"
                    and actor._watch_retry_task is None
                )
                assert actor.lease.mode == "view" and actor._watch_retry_task is None
                assert all("/attachments" not in path for _, path in ctx.requests[before:])
                ctx.backend.publish_output(lease.attachment_id, lease.epoch, b"watch resumed")
                await _wait(lambda: bytes(actor.output_log).endswith(b"watch resumed"))
                assert actor.controller._client.next_offset == len(b"initial\r\nwatch resumed")
            assert actor.current_term is term and actor.store.draft == "retained draft"
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            assert actor.takeover_disclosure is None

    run(scenario())
