"""socket registry, ticket redeem, and wire-v2 handler paths.

The handler is driven directly with stub WebSockets for deterministic
interleaving (revoke arriving mid-connection); the TestClient suite
covers the real transport.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.websockets import WebSocket, WebSocketState

from relaydeck.contracts.wire import terminal as terminal_wire
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import BackendError, FakeBackend
from relaydeck.gateway.revocation import revoke_account_and_close
from relaydeck.gateway.sockets import SocketRegistry
from tests.support.asgi_socket import AsgiSocket
from tests.support.terminal.sockets import CAPS, _grant, _state


class _StubSocket:
    def __init__(self, query: dict[str, str], inbound: list[dict[str, Any]]) -> None:
        self.query_params = query
        self._inbound = list(inbound)
        self.accepted = False
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.closes: list[tuple[int, str]] = []
        self.on_receive: Any = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def receive(self) -> dict[str, Any]:
        if self.on_receive is not None:
            await self.on_receive()
            self.on_receive = None
        if not self._inbound:
            return {"type": "websocket.disconnect"}
        await asyncio.sleep(0)
        return self._inbound.pop(0)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closes.append((code, reason))


@pytest.mark.parametrize("source", ["budget", "backend"])
@pytest.mark.parametrize("race", ["live", "closed", "send_failure"])
def test_rate_limited_gap_stays_live_unless_close_wins(monkeypatch, source, race) -> None:
    async def scenario() -> None:
        state, backend, _ = _state()
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=True
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
        if source == "budget":
            state.input_bytes.capacity = 0
        else:

            def refuse(*args):
                raise BackendError("rate_limited", "synthetic refusal", True)

            monkeypatch.setattr(backend, "write_input", refuse)
        original_receive, original_send = WebSocket.receive, state.sockets.send_text
        sends = []

        async def receive(socket):
            message = await original_receive(socket)
            if race == "closed" and message.get("bytes"):
                await state.sockets.close_lease(lease.lease_id, "stale_revision")
            return message

        async def send(socket, text):
            sends.append(json.loads(text))
            if race == "send_failure":
                await state.sockets.close_lease(lease.lease_id, "stale_revision")
            await original_send(socket, text)

        monkeypatch.setattr(WebSocket, "receive", receive)
        monkeypatch.setattr(state.sockets, "send_text", send)
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        socket = await AsgiSocket(
            create_app(state), f"/v1/terminal/{lease.lease_id}?ticket={ticket}"
        ).open()
        try:
            await socket.send_bytes(terminal_wire.encode_text(lease.lease_id, 1, 1, b"x"))
            if race == "live":
                message = await socket.receive()
                assert json.loads(message["text"]) == {"kind": "gap", "detail": "rate_limited"}
                assert not socket.task.done()
                assert backend.open_lease(lease.lease_id) is not None
                await socket.send_text(json.dumps({"kind": "bye"}))
            await asyncio.wait_for(socket.task, 1)
            assert len(sends) == (0 if race == "closed" else 1)
            assert not backend.input_logs.get(lease.lease_id)
        finally:
            await socket.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("race", ["closed", "send_failure", "gap"])
def test_input_failure_racing_close_has_one_reason_and_no_asgi_error(
    race: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        caplog.set_level("INFO", logger="relaydeck.lease_lifecycle")
        state, backend, _ = _state()
        attachment, _ = backend.create_attachment(
            "home-mac", "term-1", "shell", "acct-1", "dev-1", takeover=True
        )
        lease = backend.acquire_control(attachment.attachment_id, "acct-1", "dev-1")
        state.hosts.enroll("home-mac", account="acct-1")
        monkeypatch.setattr(state.sockets, "host_count", lambda *args: 1)
        send_text = state.sockets.send_text

        async def fail_input(*args: Any, **kwargs: Any) -> int:
            if race == "closed":
                await state.sockets.close_lease(lease.lease_id, "stale_revision")
            return 0

        async def race_send(socket: Any, data: str) -> None:
            if race == "send_failure" and json.loads(data).get("kind") == "gap":
                await socket.close(code=1008, reason="stale_revision")
                assert socket.application_state is WebSocketState.DISCONNECTED
            await send_text(socket, data)

        monkeypatch.setattr(state.sockets, "notify_host", fail_input)
        monkeypatch.setattr(state.sockets, "send_text", race_send)
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        socket = await AsgiSocket(
            create_app(state), f"/v1/terminal/{lease.lease_id}?ticket={ticket}"
        ).open()
        await socket.send_bytes(terminal_wire.encode_text(lease.lease_id, 1, 1, b"x"))
        assert socket.task is not None
        await asyncio.wait_for(socket.task, 2)
        messages = []
        while not socket.outbound.empty():
            messages.append(socket.outbound.get_nowait())
        gaps = [json.loads(m["text"]) for m in messages if m.get("text")]
        assert bool(gaps) is (race == "gap")
        if gaps:
            assert gaps[0]["detail"] == "host_offline"
        closes = [r.getMessage() for r in caplog.records if "event=socket_close" in r.getMessage()]
        assert len(closes) == 1
        assert f"reason={'stale_revision' if race == 'closed' else 'host_offline'}" in closes[0]
        assert not any(r.exc_info for r in caplog.records)
        retained = backend.open_lease(lease.lease_id)
        assert retained is not None and retained.resume_pending
        resumed_ticket = state.tickets.issue(
            replace(_grant(lease.lease_id), generation=lease.generation, resume=True), 1_000_000
        )
        resumed = await AsgiSocket(
            create_app(state),
            f"/v1/terminal/{lease.lease_id}?ticket={resumed_ticket}&epoch={lease.epoch}&offset=0",
        ).open()
        await resumed.send_text(json.dumps({"kind": "bye"}))
        assert resumed.task is not None
        await asyncio.wait_for(resumed.task, 2)
        assert backend.open_lease(lease.lease_id) is None

    asyncio.run(scenario())


def test_registry_close_scopes() -> None:
    async def scenario() -> None:
        registry = SocketRegistry()
        a1, a2, b1 = _StubSocket({}, []), _StubSocket({}, []), _StubSocket({}, [])
        registry.register("a", "lease-1", a1)
        registry.register("a", "lease-2", a2)
        registry.register("b", "lease-1", b1)
        assert len(registry) == 3
        assert await registry.close_lease("lease-1", "lease_expired") == 2
        assert a1.closes == [(1008, "lease_expired")]
        assert b1.closes == [(1008, "lease_expired")]
        assert len(registry) == 1
        assert await registry.close_account("a", "unauthenticated") == 1
        assert a2.closes == [(1008, "unauthenticated")]
        assert len(registry) == 0
        assert await registry.close_account("nobody", "x") == 0

    asyncio.run(scenario())


def test_registry_close_tolerates_errors() -> None:
    class _Boom(_StubSocket):
        async def close(self, code: int = 1000, reason: str = "") -> None:
            raise RuntimeError("boom")

    async def scenario() -> None:
        registry = SocketRegistry()
        registry.register("a", "lease-1", _Boom({}, []))
        assert await registry.close_account("a", "x") == 1
        assert len(registry) == 0

    asyncio.run(scenario())


def test_handler_registers_and_cleans_up() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        sizes: list[int] = []
        stub = _StubSocket(
            {"ticket": ticket},
            [{"type": "websocket.receive", "text": json.dumps({"kind": "bye"})}],
        )

        async def probe() -> None:
            sizes.append(len(state.sockets))

        stub.on_receive = probe
        await endpoint(stub, lease.lease_id)
        assert stub.accepted is True
        assert sizes == [1]
        assert len(state.sockets) == 0

    asyncio.run(scenario())


def test_handler_rejects_without_ticket() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        stub = _StubSocket({}, [])
        await endpoint(stub, lease.lease_id)
        assert stub.accepted is False
        assert stub.closes == [(1008, "unauthenticated")]
        assert len(state.sockets) == 0

    asyncio.run(scenario())


def test_handler_rejects_lease_mismatch_and_future_generation() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        stub = _StubSocket(
            {"ticket": ticket},
            [
                {
                    "type": "websocket.receive",
                    "bytes": terminal_wire.encode_text("other-lease", 1, 1, b"x"),
                }
            ],
        )
        await endpoint(stub, lease.lease_id)
        assert stub.closes == [(1008, "forbidden")]

        ticket2 = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        stub2 = _StubSocket(
            {"ticket": ticket2},
            [
                {
                    "type": "websocket.receive",
                    "bytes": terminal_wire.encode_text(lease.lease_id, 99, 1, b"x"),
                }
            ],
        )
        await endpoint(stub2, lease.lease_id)
        assert stub2.closes == [(1008, "forbidden")]

    asyncio.run(scenario())


def test_handler_resize_requires_control() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        view_caps = frozenset({"sessions.read", "terminal.view"})
        ticket = state.tickets.issue(_grant(lease.lease_id, view_caps), 1_000_000)
        stub = _StubSocket(
            {"ticket": ticket},
            [
                {
                    "type": "websocket.receive",
                    "bytes": terminal_wire.encode_resize(lease.lease_id, 1, 1, 80, 24),
                }
            ],
        )
        await endpoint(stub, lease.lease_id)
        assert stub.closes == [(1008, "unsupported_capability")]

    asyncio.run(scenario())


def test_handler_drops_duplicate_seq_silently() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        stub = _StubSocket(
            {"ticket": ticket},
            [
                {
                    "type": "websocket.receive",
                    "bytes": terminal_wire.encode_text(lease.lease_id, 1, 1, b"a"),
                },
                {
                    "type": "websocket.receive",
                    "bytes": terminal_wire.encode_text(lease.lease_id, 1, 1, b"a"),
                },
                {"type": "websocket.receive", "text": json.dumps({"kind": "bye"})},
            ],
        )
        await endpoint(stub, lease.lease_id)
        assert backend.input_logs[lease.lease_id] == [(1, 1, "text", b"a")]
        assert stub.sent_text == []

    asyncio.run(scenario())


def test_revoke_during_connection_severs_socket() -> None:
    async def scenario() -> None:
        state, backend, endpoint = _state()
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        access, _ = state.sessions.create("acct-1", CAPS, frozenset({"home-mac"}), 1_000_000)
        assert state.sessions.lookup(access, 1_000_000) is not None
        ticket = state.tickets.issue(_grant(lease.lease_id), 1_000_000)
        stub = _StubSocket({"ticket": ticket}, [])

        async def revoke_mid_connection() -> None:
            closed = await revoke_account_and_close(state, "acct-1")
            assert closed == 1

        stub.on_receive = revoke_mid_connection
        await endpoint(stub, lease.lease_id)
        assert state.sessions.lookup(access, 1_000_000) is None
        assert stub.closes == [(1008, "unauthenticated")]
        assert len(state.sockets) == 0

    asyncio.run(scenario())


def test_registry_host_tracking() -> None:
    from relaydeck.gateway.sockets import SocketRegistry

    async def scenario() -> None:
        registry = SocketRegistry()
        tunnel = _StubSocket({}, [])
        registry.register_host("home-mac", tunnel)
        assert len(registry) == 1
        assert await registry.close_host("other", "x") == 0
        assert await registry.close_host("home-mac", "unauthenticated") == 1
        assert tunnel.closes == [(1008, "unauthenticated")]
        assert len(registry) == 0

    asyncio.run(scenario())


def test_revoke_host_severs_tunnel_and_leases() -> None:
    from relaydeck.gateway.revocation import revoke_host_and_close

    async def scenario() -> None:
        backend = FakeBackend(now_ms=lambda: 1_000_000)
        state = GatewayState(backend=backend, cursor_secret=b"x" * 32, now_ms=lambda: 1_000_000)
        app = create_app(state)
        endpoint = next(
            r.endpoint
            for r in app.routes
            if isinstance(r, APIRoute | APIWebSocketRoute) and r.path == "/v1/connectors"
        )
        host_token = state.hosts.enroll("home-mac", "Home", "acct-1")
        lease = backend.issue_lease("home-mac", "term-1", "acct-1", 1)
        other_lease = backend.issue_lease("other", "term-2", "acct-1", 1)
        term_socket = _StubSocket({}, [])
        other_socket = _StubSocket({}, [])
        state.sockets.register("acct-1", lease.lease_id, term_socket)
        state.sockets.register("acct-1", other_lease.lease_id, other_socket)

        tunnel = _StubSocket(
            {"token": host_token},
            [{"type": "websocket.receive", "text": json.dumps({"kind": "ping"})}],
        )

        async def revoke_mid_tunnel() -> None:
            revoked, tunnels, leases = await revoke_host_and_close(state, "home-mac")
            assert (revoked, tunnels, leases) == (True, 1, 1)

        tunnel.on_receive = revoke_mid_tunnel
        await endpoint(tunnel)
        assert tunnel.closes == [(1008, "unauthenticated")]
        assert term_socket.closes == [(1008, "lease_expired")]
        assert other_socket.closes == []
        assert state.hosts.lookup(host_token) is None
        assert backend.open_lease(lease.lease_id) is None
        assert backend.open_lease(other_lease.lease_id) is not None

    asyncio.run(scenario())
