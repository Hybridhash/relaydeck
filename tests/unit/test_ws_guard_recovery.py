"""the websocket guard wakes a half-open socket when a downstream send
fails, and says why a socket was refused (fixed branch names, no cookies, ids or IPs).

The guard runs with a fake receive/send pair and a stub inner app: no loopback socket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import msgpack  # type: ignore[import-untyped]
import pytest

from relaydeck.client.serve import REGISTER_ACTION, WS_PATH, FletGuardMiddleware, WebSessionStore

ORIGIN = "https://relay.example.test"


def _store() -> WebSessionStore:
    return WebSessionStore(ORIGIN, b"s" * 32)


def _scope(origin: str = ORIGIN) -> dict[str, Any]:
    return {
        "type": "websocket",
        "path": WS_PATH,
        "headers": [(b"origin", origin.encode()), (b"user-agent", b"synthetic-ua")],
        "client": ("192.0.2.1", 4321),
    }


def _register_frame(session_id: str = "") -> bytes:
    return b"\x00" + msgpack.packb([REGISTER_ACTION, {"session_id": session_id}])


class _Peer:
    """A half-open peer: connect, then a register frame, then silence (nothing else arrives)."""

    def __init__(self) -> None:
        self._queue: list[dict[str, Any]] = [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "bytes": _register_frame()},
        ]
        self.sent: list[dict[str, Any]] = []
        self.fail_sends = False

    async def receive(self) -> dict[str, Any]:
        if self._queue:
            return self._queue.pop(0)
        await asyncio.Event().wait()  # half-open: nothing more from the peer
        raise AssertionError("unreachable")

    async def send(self, message: dict[str, Any]) -> None:
        if self.fail_sends and message["type"] == "websocket.send":
            raise OSError("peer gone")  # uvicorn ClientDisconnected is an OSError
        self.sent.append(message)


def test_failed_downstream_send_wakes_the_half_open_receive_promptly() -> None:
    async def scenario() -> None:
        peer = _Peer()
        observed: list[dict[str, Any]] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            await receive()  # connect
            await receive()  # register frame
            peer.fail_sends = True
            try:
                await send({"type": "websocket.send", "bytes": b"output"})
            except OSError:
                # The guard re-raised; the app must now see disconnect, not wait forever.
                observed.append(await asyncio.wait_for(receive(), timeout=1.0))

        guard = FletGuardMiddleware(inner, _store())
        await asyncio.wait_for(guard(_scope(), peer.receive, peer.send), timeout=5.0)
        assert [m["type"] for m in observed] == ["websocket.disconnect"]

    asyncio.run(scenario())


def test_refused_origin_logs_its_branch_and_code_without_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        peer = _Peer()

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("refused sockets never reach the app")

        guard = FletGuardMiddleware(inner, _store())
        await guard(_scope(origin="https://evil.example.test"), peer.receive, peer.send)

    with caplog.at_level(logging.INFO, logger="relaydeck.client.serve"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if "ws_guard_refused" in r.getMessage()]
    assert lines == ["ws_guard_refused branch=origin code=1008"]
    assert "evil.example" not in lines[0] and "192.0.2.1" not in lines[0]
