"""Real ASGI tunnel intake remains responsive behind journal and viewer waits."""

import asyncio
import json

from relaydeck.contracts.tunnel import SnapshotIntake
from relaydeck.gateway.api import sockets as sockets_module
from relaydeck.gateway.app import create_app
from relaydeck.tunnel_queue import BoundedInbox
from tests.support.asgi_socket import AsgiSocket
from tests.support.terminal.sockets import _state


def test_journal_wait_does_not_block_tunnel_ping_or_receive(monkeypatch) -> None:
    async def scenario() -> None:
        state, _, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), asyncio.Event()
        original = sockets_module.command_io

        async def command_io(backend, operation, *args):
            if operation == "command_owner":
                entered.set()
                await release.wait()
                return None
            return await original(backend, operation, *args)

        monkeypatch.setattr(sockets_module, "command_io", command_io)
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        try:
            assert json.loads(await socket.recv_text())["kind"] == "hello"
            await socket.send_text(
                json.dumps(
                    {
                        "kind": "command.ack",
                        "host_id": "home-mac",
                        "command_id": "held",
                    }
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            await socket.send_text(json.dumps({"kind": "ping"}))
            message = await socket.receive()
            assert json.loads(message["text"]) == {"kind": "pong"}
            assert not release.is_set()
        finally:
            release.set()
            await socket.close()
        assert state.sockets.host_count("home-mac") == 0

    asyncio.run(scenario())


def test_fourteen_full_cadence_agents_survive_a_100ms_command_wait(monkeypatch):
    import base64
    import time

    async def scenario():
        state, backend, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), asyncio.Event()
        original = sockets_module.command_io
        received = []

        async def command_io(backend, operation, *args):
            if operation == "command_owner":
                entered.set()
                await release.wait()
                return ("acct-1", "home-mac")
            if operation == "note_command_ack":
                return {}
            return await original(backend, operation, *args)

        monkeypatch.setattr(sockets_module, "command_io", command_io)
        monkeypatch.setattr(backend, "ingest_terminal_output", lambda *args: received.append(args))
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        try:
            await socket.recv_text()
            await socket.send_text(
                json.dumps({"kind": "command.ack", "host_id": "home-mac", "command_id": "held"})
            )
            await entered.wait()
            started = time.monotonic()
            for _ in range(20):
                for agent in range(14):
                    await socket.send_text(
                        json.dumps(
                            {
                                "kind": "terminal.output",
                                "host_id": "home-mac",
                                "terminal_id": f"agent-{agent}",
                                "epoch": "e1",
                                "data_b64": base64.b64encode(b"x" * 64).decode(),
                            }
                        )
                    )
                await asyncio.sleep(0.005)
            assert time.monotonic() - started >= 0.1
            await socket.send_text(json.dumps({"kind": "ping"}))
            assert json.loads(await socket.recv_text()) == {"kind": "pong"}
            assert not socket.task.done() and not release.is_set()
            async with asyncio.timeout(2):
                while len(received) < 280:
                    await asyncio.sleep(0)
            release.set()
            await socket.send_text(json.dumps({"kind": "ping"}))
            assert json.loads(await socket.recv_text()) == {"kind": "pong"}
            assert not socket.task.done()
        finally:
            release.set()
            await socket.close()

    asyncio.run(scenario())


def test_idle_snapshot_transfer_still_has_a_deadline(monkeypatch):
    async def scenario():
        state, _, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        intake = SnapshotIntake()
        monkeypatch.setattr(sockets_module, "SnapshotIntake", lambda: intake)
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        try:
            await socket.recv_text()
            await socket.send_text(
                json.dumps(
                    {
                        "kind": "snapshot.begin",
                        "host_id": "home-mac",
                        "transfer_id": "transfer",
                        "total_bytes": 10,
                        "total_chunks": 1,
                        "sha256": "a" * 64,
                    }
                )
            )
            async with asyncio.timeout(1):
                while not intake.active:
                    await asyncio.sleep(0)
            intake.deadline = 0
            assert (await socket.receive())["type"] == "websocket.close"
        finally:
            await socket.close()

    asyncio.run(scenario())


def test_gateway_byte_overflow_closes_with_distinct_fixed_reason(monkeypatch):
    async def scenario():
        state, _, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), asyncio.Event()
        original = sockets_module.command_io

        async def command_io(backend, operation, *args):
            if operation == "command_owner":
                entered.set()
                await release.wait()
                return None
            return await original(backend, operation, *args)

        monkeypatch.setattr(sockets_module, "command_io", command_io)
        monkeypatch.setattr(sockets_module, "BoundedInbox", lambda: BoundedInbox(byte_limit=1024))
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        try:
            await socket.recv_text()
            await socket.send_text(
                json.dumps({"kind": "command.ack", "host_id": "home-mac", "command_id": "held"})
            )
            await asyncio.wait_for(entered.wait(), 1)
            for _ in range(20):
                await socket.send_text(
                    json.dumps({"kind": "command.ack", "host_id": "home-mac", "command_id": "held"})
                )
            message = await socket.receive()
            assert message["type"] == "websocket.close"
            assert message["reason"] == "tunnel_queue_full"
        finally:
            release.set()
            await socket.close()

    asyncio.run(scenario())


def test_viewer_cleanup_is_captured_and_cannot_close_replacement(monkeypatch) -> None:
    async def scenario() -> None:
        state, backend, _ = _state()
        token = state.hosts.enroll("home-mac", account="acct-1")
        entered, release = asyncio.Event(), asyncio.Event()

        class Viewer:
            def __init__(self):
                self.closed = False

            async def send_text(self, text):
                entered.set()
                await release.wait()

            async def close(self, **kwargs):
                self.closed = True

        old, replacement = Viewer(), Viewer()
        state.sockets.register("acct-1", "lease", old)
        monkeypatch.setattr(backend, "reset_terminal", lambda *_: ("lease",))
        socket = await AsgiSocket(create_app(state), "/v1/connectors", token=token).open()
        try:
            await socket.recv_text()
            await socket.send_text(
                json.dumps(
                    {
                        "kind": "terminal.reset_required",
                        "host_id": "home-mac",
                        "terminal_id": "term",
                        "epoch": "e1",
                    }
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            state.sockets.register("acct-1", "lease", replacement)
            await socket.send_text(json.dumps({"kind": "ping"}))
            assert json.loads((await socket.receive())["text"]) == {"kind": "pong"}
            release.set()
            await socket.send_text(json.dumps({"kind": "bye"}))
            await socket.close()
            assert old.closed and not replacement.closed
        finally:
            release.set()
            await socket.close()
            await state.sockets.close_lease("lease", "closed")

    asyncio.run(scenario())
