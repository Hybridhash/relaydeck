"""Cookie-carrying socket and polling helper."""

from __future__ import annotations

import asyncio
from typing import Any

import msgpack

from tests.support.asgi_socket import AsgiSocket


class CookieSocket(AsgiSocket):
    def __init__(
        self, app: Any, cookie: str, ip: str = "192.0.2.10", *, ua: str = "synthetic"
    ) -> None:
        super().__init__(app, "/ws")
        self.cookie, self.ip, self.ua = cookie, ip, ua

    async def open(self) -> CookieSocket:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"origin", b"http://test"),
                (b"user-agent", self.ua.encode()),
                (b"cookie", self.cookie.encode()),
            ],
            "client": (self.ip, 1234),
            "server": ("test", 80),
            "subprotocols": [],
        }
        await self.inbound.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(scope, self.inbound.get, self.outbound.put))
        assert (await self.receive())["type"] == "websocket.accept"
        return self

    async def packet(self, action: int, body: dict[str, Any]) -> None:
        await self.send_bytes(b"\x00" + msgpack.packb([action, body]))

    async def register(self, fid: str = "") -> str | dict[str, Any]:
        await self.packet(
            1,
            {
                "session_id": fid,
                "page_name": "",
                "page": {"route": "/agents/synthetic:term", "width": 390, "height": 844},
            },
        )
        while True:
            response = await self.receive()
            if response["type"] == "websocket.close":
                return response
            packet = response.get("bytes", b"")
            if packet[:1] == b"\x00":
                action, body = msgpack.unpackb(packet[1:], raw=False, strict_map_key=False)
                if action == 1:
                    assert not body.get("error")
                    return str(body["session_id"])


async def until(check: Any) -> None:
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0.005)
