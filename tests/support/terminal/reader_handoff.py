"""Actors and sockets for reader handoff tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx

from relaydeck.client.adapters.fake import FakeClipboard
from relaydeck.client.features.shell.ops import HttpxShellGatewayOps
from relaydeck.client.features.shell.session import ShellSession
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.agent_session import AgentSession
from relaydeck.gateway.leases import READER_HANDOFF_TTL_MS
from tests.support.asgi_socket import AsgiSocket
from tests.support.terminal.lease_gateway import _client, _token


async def _wait(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class _Socket(TerminalSocket):
    def __init__(self, factory, ticket):
        self.factory, self.ticket = factory, ticket
        self.socket = None
        self.sent = []

    async def connect(self):
        controller = self.factory.actor.controller
        query = urlencode({"ticket": self.ticket}) + "&" + controller.resume_query()
        self.socket = AsgiSocket(self.factory.app, f"/v1/terminal/{controller.lease_id}?{query}")
        if self.factory.fail_next:
            self.factory.fail_next = False
            self.factory.clock.now += READER_HANDOFF_TTL_MS + 1
        try:
            await self.socket.open()
        except OSError as exc:
            raise TransportError("host_offline", str(exc)) from exc

    async def receive(self):
        message = await self.socket.receive()
        if message["type"] == "websocket.close":
            raise TransportError("host_offline", message.get("reason", "closed"))
        return ("bytes", message["bytes"]) if "bytes" in message else ("text", message["text"])

    async def send_bytes(self, data):
        self.sent.append(data)
        await self.socket.send_bytes(data)

    async def send_text(self, data):
        await self.socket.send_text(data)

    async def close(self):
        if self.socket is not None:
            await self.socket.close()


class _Factory:
    def __init__(self, app, clock):
        self.app, self.clock = app, clock
        self.actor = None
        self.sockets = []
        self.fail_next = False

    def __call__(self, ticket):
        socket = _Socket(self, ticket)
        self.sockets.append(socket)
        return socket


@asynccontextmanager
async def _actor(kind):
    client, state, backend, clock = _client()
    app, token = client.app, _token(state)
    requests = []

    async def auth():
        return token

    async def no_command(*_args):
        raise AssertionError("reader recovery must not submit commands")

    async def record(request):
        requests.append((request.method, request.url.path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        event_hooks={"request": [record]},
    ) as http:
        ops = HttpxShellGatewayOps("http://test", auth, http)
        factory = _Factory(app, clock)
        kwargs = dict(
            ops=ops,
            host_id="home-mac",
            device_id="dev-1",
            socket_factory=factory,
            clipboard=FakeClipboard(),
        )
        if kind == "agent":
            actor = AgentSession(
                **kwargs,
                submit_command=no_command,
                prompt_text=no_command,
                host_epoch="epoch-7",
                agent_id="term-1",
                target="term-1",
            )
        else:
            actor = ShellSession(**kwargs)
        factory.actor = actor
        readers = []
        try:
            if kind == "agent":
                actor.open()
                await actor.confirm_takeover()
                actor.on_draft_change("retained draft")
            else:
                await actor.open("term-1")
            assert actor.lease is not None
            lease = actor.lease
            backend.publish_output(lease.attachment_id, lease.epoch, b"initial\r\n")
            readers.append(asyncio.create_task(actor.run()))
            await _wait(lambda: bytes(actor.output_log) == b"initial\r\n")
            yield SimpleNamespace(
                actor=actor,
                backend=backend,
                state=state,
                clock=clock,
                http=http,
                ops=ops,
                factory=factory,
                lease=lease,
                requests=requests,
                readers=readers,
            )
        finally:
            await actor.dispose()
            for task in readers:
                task.cancel()
                with suppress(asyncio.CancelledError, TransportError):
                    await task
            for socket in factory.sockets:
                await socket.close()
            assert len(state.sockets) == 0
            if actor.lease is not None:
                assert backend.fanout_for(actor.lease.attachment_id).viewer_count == 0
