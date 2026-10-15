"""In-process terminal ASGI endpoint."""

from __future__ import annotations

from typing import Any

from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from tests.support.asgi_socket import AsgiSocket


class AsgiTerminal(TerminalSocket):
    def __init__(self, app: Any, lease_id: str, ticket: str) -> None:
        self.socket = AsgiSocket(app, f"/v1/terminal/{lease_id}?ticket={ticket}")

    async def connect(self) -> None:
        await self.socket.open()

    async def send_bytes(self, data: bytes) -> None:
        await self.socket.send_bytes(data)

    async def send_text(self, data: str) -> None:
        await self.socket.send_text(data)

    async def receive(self) -> tuple[str, Any]:
        message = await self.socket.receive()
        if message["type"] == "websocket.close":
            raise TransportError("host_offline", "synthetic stream close", True)
        return ("bytes", message["bytes"]) if "bytes" in message else ("text", message["text"])

    async def close(self) -> None:
        await self.socket.close()
        await self.socket.outbound.put({"type": "websocket.close"})
