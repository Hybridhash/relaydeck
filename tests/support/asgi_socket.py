"""Cancellable in-process WebSockets: real ASGI handlers, no TCP or threads."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit


class AsgiSocket:
    def __init__(self, app: Any, path: str, *, token: str = "") -> None:
        self.app = app
        self.path = path
        self.token = token
        self.inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[str] = []
        self.task: asyncio.Task[Any] | None = None

    async def open(self) -> AsgiSocket:
        url = urlsplit(self.path)
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": url.path,
            "raw_path": url.path.encode(),
            "query_string": url.query.encode(),
            "root_path": "",
            "headers": [(b"authorization", f"Bearer {self.token}".encode())] if self.token else [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "subprotocols": [],
        }
        await self.inbound.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(scope, self.inbound.get, self.outbound.put))
        first = await self.receive()
        if first["type"] != "websocket.accept":
            raise OSError(f"handshake rejected: {first}")
        return self

    async def receive(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.outbound.get(), 2)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)
        await self.inbound.put({"type": "websocket.receive", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self.inbound.put({"type": "websocket.receive", "bytes": data})

    async def recv_text(self) -> str:
        # Link supplies its own short receive timeout. Queue get is safely
        # cancellable, unlike a blocking TestClient receive on a thread.
        message = await self.outbound.get()
        if message["type"] == "websocket.close":
            raise OSError(f"closed: {message.get('reason')}")
        text: str = message["text"]
        return text

    async def close(self) -> None:
        if self.task is None:
            return
        await self.inbound.put({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(self.task, 2)
        finally:
            if not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
