"""Actual Flet 1.0.3 WS registration through an in-process ASGI client.

No TCP bind, external identity provider, browser renderer or real host.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from typing import Any

import flet as ft
import msgpack  # type: ignore[import-untyped]
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relaydeck.client.features.auth.web_recovery import RECOVERY_REASON
from relaydeck.client.serve import WebSessionStore, build_web_app, identity_for_page
from relaydeck.gateway.app import GatewayState, create_app
from relaydeck.gateway.backend import FakeBackend


def test_actual_flet_registration_reconnect_and_cross_account_denial() -> None:
    state = GatewayState(backend=FakeBackend(), cursor_secret=b"c" * 32)
    store = WebSessionStore("http://testserver", b"s" * 32)
    now = int(time.time() * 1000)
    owner = store.create_session("owner-a", "da", "at", "rt", now + 900_000)
    other = store.create_session("owner-b", "db", "bt", "br", now + 900_000)
    created: list[str] = []

    async def main(page: ft.Page) -> None:
        ident = identity_for_page(store, page)
        created.append(ident.account if ident else "anonymous")
        page.add(ft.Text(created[-1], key="who"))

    app, _ = build_web_app(
        gateway_state=state,
        gateway_app=create_app(state),
        flet_main=main,
        public_origin="http://testserver",
        cookie_secret=b"s" * 32,
        store=store,
    )

    def headers(sid: str) -> dict[str, str]:
        return {"Origin": "http://testserver", "Cookie": "rd_session=" + store.sign_sid(sid)}

    def register(ws: Any, session_id: str) -> tuple[str, bytes]:
        ws.send_bytes(
            b"\x00" + msgpack.packb([1, {"session_id": session_id, "page_name": "", "page": {}}])
        )
        response = ws.receive_bytes()
        action, body = msgpack.unpackb(response[1:], raw=False)
        assert action == 1
        assert not body["error"]
        return str(body["session_id"]), response

    with TestClient(app) as http:
        with http.websocket_connect("/ws", headers=headers(owner)) as ws:
            assigned, _ = register(ws, "")
            assert store.owner_of(assigned) == owner
            for _ in range(8):
                if b"owner-a" in ws.receive_bytes():
                    break
            else:
                raise AssertionError("the actual Flet page never rendered its owner marker")
        with http.websocket_connect("/ws", headers=headers(owner)) as ws:
            restored, response = register(ws, assigned)
            assert restored == assigned
            assert b"owner-a" in response
        assert created == ["owner-a"]

        with http.websocket_connect("/ws", headers=headers(other)) as ws:
            ws.send_bytes(b"\x00" + msgpack.packb([1, {"session_id": assigned, "page": {}}]))
            with pytest.raises(WebSocketDisconnect) as denied:
                ws.receive_bytes()
            assert denied.value.code == 1008
            assert denied.value.reason == RECOVERY_REASON
        assert store.owner_of(assigned) == owner
        assert created == ["owner-a"]
        with http.websocket_connect("/ws", headers=headers(other)) as ws:
            register(ws, "")
            # A second REGISTER must not reattach this connection to A.
            ws.send_bytes(b"\x00" + msgpack.packb([True, {"session_id": assigned, "page": {}}]))
            for _ in range(8):
                try:
                    data = ws.receive_bytes()
                    assert b"owner-a" not in data
                except WebSocketDisconnect as denied:
                    assert denied.code == 1008
                    break
            else:
                raise AssertionError("a second registration was not rejected")
        assert "owner-a" == created[0]
    assert store._closers == {}


def test_unregistered_socket_is_bounded_without_starting_flet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client import serve

    async def scenario() -> None:
        store = WebSessionStore("https://app.example", b"s" * 32)
        invoked = False
        first = True
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal first
            if first:
                first = False
                return {"type": "websocket.connect"}
            await asyncio.Event().wait()
            raise AssertionError("not reached")

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            nonlocal invoked
            invoked = True

        monkeypatch.setattr(serve, "REGISTER_TIMEOUT_S", 0.01)
        await asyncio.wait_for(
            serve.FletGuardMiddleware(downstream, store)(
                {
                    "type": "websocket",
                    "path": "/ws",
                    "headers": [(b"origin", b"https://app.example")],
                },
                receive,
                send,
            ),
            timeout=1,
        )
        assert not invoked
        assert outgoing[-1] == {"type": "websocket.close", "code": 1008, "reason": "forbidden"}
        assert store._closers == {}

    asyncio.run(scenario())


def test_oversized_register_is_rejected_before_decode_or_flet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relaydeck.client import serve

    async def scenario() -> None:
        store = WebSessionStore("https://app.example", b"s" * 32)
        incoming: Iterator[dict[str, Any]] = iter(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "bytes": b"\x00" * (serve.MAX_REGISTER_BYTES + 1)},
            ]
        )
        outgoing: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        def decode(frame: bytes) -> Any:
            raise AssertionError("oversized registration must be rejected before MsgPack decode")

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("oversized registration must not instantiate Flet")

        monkeypatch.setattr(serve, "parse_register", decode)
        await serve.FletGuardMiddleware(downstream, store)(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [(b"origin", b"https://app.example")],
            },
            receive,
            send,
        )
        assert outgoing[-1]["code"] == 1008
        assert store._closers == {}

    asyncio.run(scenario())


def test_registered_data_channel_bytes_pass_through_unchanged() -> None:
    from relaydeck.client import serve

    async def scenario() -> None:
        store = WebSessionStore("https://app.example", b"s" * 32)
        register = b"\x00" + msgpack.packb([1, {"session_id": "", "page": {}}])
        data = b"\x01\x03\x00\x00\x00\x84\x00\x1b[31m\xf0\x9f\x90\xbe"
        incoming = iter(
            [
                {"type": "websocket.connect"},
                {"type": "websocket.receive", "bytes": register},
                {"type": "websocket.receive", "bytes": data},
            ]
        )

        async def receive() -> dict[str, Any]:
            return next(incoming)

        async def send(message: dict[str, Any]) -> None:
            pass

        async def downstream(scope: Any, receive: Any, send: Any) -> None:
            assert await receive() == {"type": "websocket.connect"}
            await send({"type": "websocket.accept"})
            assert (await receive())["bytes"] == register
            assert (await receive())["bytes"] == data

        await serve.FletGuardMiddleware(downstream, store)(
            {
                "type": "websocket",
                "path": "/ws",
                "headers": [(b"origin", b"https://app.example")],
            },
            receive,
            send,
        )

    asyncio.run(scenario())
