"""Authenticated bounded first replay and same-lease live-edge output recovery."""

import asyncio
import json
import time

import pytest

from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.identity import TicketGrant
from relaydeck.gateway.leases import READER_HANDOFF_TTL_MS
from relaydeck.gateway.ring import FIRST_REPLAY_MAX_BYTES, RING_MAX_BYTES, VIEWER_QUEUE_MAX_BYTES
from tests.support.asgi_socket import AsgiSocket
from tests.support.terminal.reader_handoff import _actor, _wait


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("publish_during_baseline", [False, True])
def test_overflow_resumes_same_lease_at_atomic_live_edge(
    kind, publish_during_baseline, monkeypatch
):
    async def scenario():
        async with _actor(kind, now_ms=int(time.time() * 1000)) as ctx:
            actor, lease = ctx.actor, ctx.lease
            controller = actor.controller
            generation, expires = (
                lease.generation,
                ctx.backend.open_lease(lease.lease_id).expires_ms,
            )
            ctx.backend.publish_output(
                lease.attachment_id, lease.epoch, b"x" * (VIEWER_QUEUE_MAX_BYTES + 1)
            )
            await asyncio.wait_for(ctx.readers[0], 2)
            assert controller.live_edge_pending and controller.ui_input_fenced
            assert not controller.reattach_needed and controller.key_generation() is None
            end = ctx.backend.fanout_for(lease.attachment_id).ring.end_offset
            before = list(ctx.requests)
            send = ctx.state.sockets.send_text

            async def send_text(socket, text):
                if publish_during_baseline and json.loads(text).get("kind") == "history_reset":
                    ctx.backend.publish_output(lease.attachment_id, lease.epoch, b"during")
                await send(socket, text)

            monkeypatch.setattr(ctx.state.sockets, "send_text", send_text)
            assert await actor.drive_recovery() == "resumed"
            ctx.readers.append(asyncio.create_task(actor.run()))
            await _wait(lambda: controller.live_output_received)
            if publish_during_baseline:
                await _wait(lambda: bytes(actor.output_log).endswith(b"during"))
            assert actor.controller is controller and actor.lease is lease
            current = ctx.backend.open_lease(lease.lease_id)
            assert current.generation == generation and current.expires_ms == expires
            assert not current.resume_pending and not controller.live_edge_pending
            assert controller._client.next_offset == end + (6 if publish_during_baseline else 0)
            assert "Older output not kept" in actor.term_state.notice.text
            assert all(path == "/v1/terminal-tickets" for _, path in ctx.requests[len(before) :])
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            if kind == "agent":
                assert actor.store.draft == "retained draft"

    asyncio.run(scenario())


def test_watch_live_edge_does_not_change_control_or_send_input():
    async def scenario():
        async with _actor("agent") as ctx:
            held = ctx.backend.open_lease(ctx.lease.lease_id)
            view = ctx.backend.issue_view(ctx.lease.attachment_id, "acct-1", "dev-1")
            ticket = await ctx.ops.terminal_ticket(view.lease_id)
            count = len(ctx.backend.ownership._leases)
            socket = await AsgiSocket(
                ctx.factory.app,
                f"/v1/terminal/{view.lease_id}?ticket={ticket}&epoch={view.epoch}"
                "&offset=0&output_reset=v1&live_edge=1",
            ).open()
            try:
                doc = json.loads((await socket.receive())["text"])
                assert doc["kind"] == "history_reset" and doc["incomplete"]
                assert ctx.backend.open_lease(view.lease_id).mode == "view"
                assert ctx.backend.open_lease(held.lease_id) == held
                assert len(ctx.backend.ownership._leases) == count
                assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            finally:
                await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["release", "revoke", "replace", "grace", "foreign_device"])
def test_live_edge_cannot_restore_invalid_lease_or_cross_device(invalid):
    async def scenario():
        async with _actor("shell") as ctx:
            lease = ctx.lease
            ticket = await ctx.ops.terminal_ticket(lease.lease_id)
            await ctx.actor.controller.close()
            await ctx.readers[0]
            assert ctx.backend.open_lease(lease.lease_id).resume_pending
            if invalid == "release":
                ctx.backend.close_lease(lease.lease_id)
            elif invalid == "revoke":
                ctx.backend.revoke_leases_device("acct-1", "dev-1")
            elif invalid == "replace":
                ctx.backend.issue_lease("home-mac", "term-1", "acct-1", 9, "new")
            elif invalid == "grace":
                ctx.clock.now += READER_HANDOFF_TTL_MS + 1
            else:
                ticket = ctx.state.tickets.issue(
                    TicketGrant(
                        "acct-1",
                        "other",
                        frozenset({"terminal.view"}),
                        frozenset({"home-mac"}),
                        lease.lease_id,
                        "home-mac",
                        generation=lease.generation,
                        resume=True,
                    ),
                    ctx.clock.now,
                )
            count = len(ctx.backend.ownership._leases)
            with pytest.raises(OSError, match="handshake rejected"):
                await AsgiSocket(
                    ctx.factory.app,
                    f"/v1/terminal/{lease.lease_id}?ticket={ticket}&epoch={lease.epoch}"
                    "&offset=0&output_reset=v1&live_edge=1",
                ).open()
            assert len(ctx.backend.ownership._leases) == count
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []

    asyncio.run(scenario())


def test_unnegotiated_large_first_replay_fails_explicitly():
    async def scenario():
        async with _actor("shell") as ctx:
            lease = ctx.lease
            ticket = await ctx.ops.terminal_ticket(lease.lease_id)
            ctx.backend.publish_output(
                lease.attachment_id, lease.epoch, b"x" * (VIEWER_QUEUE_MAX_BYTES + 1)
            )
            socket = await AsgiSocket(
                ctx.factory.app, f"/v1/terminal/{lease.lease_id}?ticket={ticket}"
            ).open()
            try:
                assert json.loads((await socket.receive())["text"]) == {
                    "kind": "reset",
                    "detail": "history_incomplete",
                }
                assert (await socket.receive())["type"] == "websocket.close"
            finally:
                await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [FIRST_REPLAY_MAX_BYTES + 1, VIEWER_QUEUE_MAX_BYTES])
def test_unnegotiated_clients_keep_legacy_replay_through_viewer_bound(size):
    async def scenario():
        async with _actor("shell") as ctx:
            lease = ctx.lease
            ring = ctx.backend.fanout_for(lease.attachment_id).ring
            ctx.backend.publish_output(
                lease.attachment_id, lease.epoch, b"x" * (size - ring.end_offset)
            )
            ticket = await ctx.ops.terminal_ticket(lease.lease_id)
            expected = ctx.backend.read_stream(lease.lease_id, "", 0)
            socket = await AsgiSocket(
                ctx.factory.app, f"/v1/terminal/{lease.lease_id}?ticket={ticket}"
            ).open()
            try:
                data = []
                for offset, payload in expected.chunks:
                    frame = await socket.receive()
                    assert "bytes" in frame  # No new baseline/reset document for an old client.
                    chunk = terminal_wire.decode_output(frame["bytes"])
                    assert chunk.offset == offset and chunk.data == payload
                    data.append(chunk.data)
                assert sum(map(len, data)) == size and not socket.task.done()
            finally:
                await socket.close()

    asyncio.run(scenario())


def test_nonzero_retention_base_with_full_retained_replay_is_not_incomplete():
    from relaydeck.gateway.ring import RING_MAX_AGE_MS, TerminalRing

    async def scenario():
        async with _actor("shell") as ctx:
            lease = ctx.lease
            ring = TerminalRing(lease.epoch, 0)
            ring.append(b"evicted", ctx.clock.now - RING_MAX_AGE_MS - 1)
            ring.append(b"all retained", ctx.clock.now)
            assert ring.base_offset == 7
            ctx.backend.fanout_for(lease.attachment_id).ring = ring
            ticket = await ctx.ops.terminal_ticket(lease.lease_id)
            socket = await AsgiSocket(
                ctx.factory.app, f"/v1/terminal/{lease.lease_id}?ticket={ticket}&output_reset=v1"
            ).open()
            try:
                assert json.loads((await socket.receive())["text"]) == {
                    "kind": "history_reset",
                    "epoch": lease.epoch,
                    "offset": 7,
                    "incomplete": False,
                }
                assert (
                    terminal_wire.decode_output((await socket.receive())["bytes"]).data
                    == b"all retained"
                )
            finally:
                await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "size", [0, FIRST_REPLAY_MAX_BYTES, FIRST_REPLAY_MAX_BYTES + 1, RING_MAX_BYTES + 7]
)
def test_first_connect_replay_cap_is_server_selected_and_labeled(size):
    async def scenario():
        async with _actor("shell") as ctx:
            controller, lease = ctx.actor.controller, ctx.lease
            ticket = await ctx.actor._ops.terminal_ticket(lease.lease_id)
            await controller.close()
            await ctx.readers[0]
            # Include split UTF-8/ANSI bytes: the server preserves bytes, does not
            # pretend a truncated tail is a complete terminal snapshot.
            payload = (b"\x1b[31m\xe2\x98\x83" * ((size + 7) // 8))[:size]
            ctx.backend.publish_output(lease.attachment_id, lease.epoch, payload)
            ring = ctx.backend.fanout_for(lease.attachment_id).ring
            start = max(ring.base_offset, ring.end_offset - FIRST_REPLAY_MAX_BYTES)
            expected = ctx.backend.read_stream(lease.lease_id, lease.epoch, start)
            socket = await AsgiSocket(
                ctx.factory.app,
                f"/v1/terminal/{lease.lease_id}?ticket={ticket}&epoch=&offset=0&output_reset=v1",
            ).open()
            try:
                baseline = json.loads((await socket.receive())["text"])
                assert baseline == {
                    "kind": "history_reset",
                    "epoch": lease.epoch,
                    "offset": start,
                    "incomplete": start > ring.base_offset,
                }
                output = []
                for offset, data in expected.chunks:
                    chunk = terminal_wire.decode_output((await socket.receive())["bytes"])
                    assert (
                        chunk.epoch == lease.epoch and chunk.offset == offset and chunk.data == data
                    )
                    output.append(chunk.data)
                assert sum(map(len, output)) <= FIRST_REPLAY_MAX_BYTES
                assert b"".join(output) == b"".join(data for _, data in expected.chunks)
                assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            finally:
                await socket.close()

    asyncio.run(scenario())
