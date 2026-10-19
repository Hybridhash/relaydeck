"""A recovery that reopens the same control lease keeps it, through the production URL builder.

When a reader closes while this device holds a resume ticket, the gateway keeps the control lease
for that ticket's reader and admits it only with a stream position. The production socket factory
must therefore send the position on a reopen; without it the lease ends and the owner is left with
an explicit Reconnect (seen after a phone came back from the lock screen).
"""

import asyncio
from urllib.parse import urlsplit

import pytest

from relaydeck.client.features.terminal.stream import INCOMPLETE_HISTORY
from relaydeck.client.features.terminal.surfaces.sessions import drive_recovery
from relaydeck.client.sessions import SessionFactory, production_stream_factory
from relaydeck.gateway.ring import RING_MAX_AGE_MS
from tests.support.asgi_socket import AsgiSocket
from tests.support.host_harness import run
from tests.support.terminal.reader_handoff import _actor, _Socket, _wait


class _ProductionUrlSocket(_Socket):
    """The harness socket, but connecting to the exact URL production builds."""

    def __init__(self, factory, ticket, url):
        super().__init__(factory, ticket)
        self.url = url

    async def connect(self):
        parts = urlsplit(self.url)
        self.socket = AsgiSocket(self.factory.app, f"{parts.path}?{parts.query}")
        await self.socket.open()


def _production_factory(ctx, urls):
    build = production_stream_factory("ws://test")

    def open_stream(lease_id, ticket, resume=""):
        url = build(lease_id, ticket, resume).url
        urls.append(url)
        socket = _ProductionUrlSocket(ctx.factory, ticket, url)
        ctx.factory.sockets.append(socket)
        return socket

    factory = SessionFactory(
        ops=ctx.ops,
        open_stream=open_stream,
        submit_command=None,
        prompt_text=None,
        renew_lease=None,
        clipboard=None,
        device_id="dev-1",
    )
    return factory._socket_factory_for({"session": ctx.actor})


@pytest.mark.parametrize("kind", ["agent", "shell"])
@pytest.mark.parametrize("history", ["retained", "evicted"])
def test_a_fresh_viewer_recovery_reopens_the_same_control_lease(kind, history):
    async def scenario():
        async with _actor(kind) as ctx:
            actor, lease = ctx.actor, ctx.lease
            controller = actor.controller
            held = ctx.backend.open_lease(lease.lease_id)
            generation, expires = held.generation, held.expires_ms
            input_seq = controller._client._input_seq
            if history == "evicted":
                # Renewed in steps as the client does, so only the old output ages out.
                for _ in range(RING_MAX_AGE_MS // 10_000 + 1):
                    ctx.clock.now += 10_000
                    ctx.backend.renew_lease(lease.lease_id, "acct-1", "dev-1")
                ctx.backend.publish_output(lease.attachment_id, lease.epoch, b"new")
                held = ctx.backend.open_lease(lease.lease_id)
                generation, expires = held.generation, held.expires_ms
            urls = []
            actor._socket_factory = _production_factory(ctx, urls)
            before = len(ctx.requests)
            # This helper models a fresh renderer; its old diagnostic byte log is not
            # the new renderer's screen. Complete retained history needs no native reset.
            actor.output_log.clear()
            outcome = await drive_recovery(
                controller, actor._ops.terminal_ticket, fresh_viewer=True
            )
            assert outcome == "reattached"
            assert "&epoch=&offset=0" in urls[-1], "the reopen named its position"
            reader = asyncio.create_task(actor.run())
            ctx.readers.append(reader)
            live = ctx.backend.open_lease(lease.lease_id)
            assert live is not None, "the control lease survived the reopen"
            assert not live.resume_pending
            assert live.generation == generation and live.expires_ms == expires
            assert actor.controller is controller and actor.lease is lease
            assert all(path == "/v1/terminal-tickets" for _m, path in ctx.requests[before:])
            if history == "retained":
                await _wait(lambda: controller._client.next_offset == len(b"initial\r\n"))
                assert actor.term_state.notice is None or (
                    actor.term_state.notice.text != INCOMPLETE_HISTORY
                )
                assert not reader.done()
            else:
                # Retention base moved, but the complete retained ring fits the cap.
                # It needs no incomplete notice, native reset or ownership change.
                await _wait(lambda: bytes(actor.output_log) == b"new")
                assert actor.term_state.notice is None or (
                    actor.term_state.notice.text != INCOMPLETE_HISTORY
                )
                assert not reader.done()
                assert controller.pending_resume is None and not controller.reattach_needed
                assert bytes(actor.output_log) == b"new"
                assert ctx.backend.open_lease(lease.lease_id).generation == generation
            assert controller._client._input_seq == input_seq
            assert ctx.backend.input_logs == {} and ctx.backend.resize_log == []
            if kind == "agent":
                assert actor.store.draft == "retained draft"

    run(scenario())


def test_the_session_factory_opens_first_ticket_only_and_reopens_with_the_position():
    async def scenario():
        async with _actor("agent") as ctx:
            actor = ctx.actor
            urls = []
            factory = _production_factory(ctx, urls)
            fresh = actor._spawn_controller(actor.lease)
            actor._controller = fresh  # a new controller has never opened: first open
            factory("tk-first")
            assert "epoch=" not in urls[-1]
            fresh._client.reopening = True  # as when opening again after its first open
            factory("tk-again")
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(urls[-1]).query, keep_blank_values=True)
            assert query == {
                "ticket": ["tk-again"],
                "output_reset": ["v1"],
                "epoch": [""],
                "offset": ["0"],
            }

    run(scenario())


def test_the_url_builder_appends_the_position_only_when_given():
    build = production_stream_factory("ws://test")
    assert build("lease-1", "tk").url == "ws://test/v1/terminal/lease-1?ticket=tk&output_reset=v1"
    assert (
        build("lease-1", "tk", "epoch=e&offset=5").url
        == "ws://test/v1/terminal/lease-1?ticket=tk&output_reset=v1&epoch=e&offset=5"
    )
