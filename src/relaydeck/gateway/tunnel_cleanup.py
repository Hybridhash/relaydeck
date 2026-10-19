"""Bounded cleanup of captured viewer sockets, independent of tunnel intake."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from relaydeck.gateway.sockets import SocketRegistry
from relaydeck.tunnel_queue import BoundedInbox


class TunnelCleanup:
    def __init__(self, registry: SocketRegistry) -> None:
        self.registry = registry
        self.inbox: BoundedInbox[tuple[Any, str, str]] = BoundedInbox(64, 64)
        self.pending: dict[int, Any] = {}

    def add(self, sockets: Iterable[Any], text: str, reason: str = "") -> None:
        for socket in sockets:
            self.inbox.put((socket, text, reason), 1)
            self.pending[id(socket)] = socket

    async def run(self) -> None:
        while True:
            entry = await self.inbox.get()
            socket, text, reason = entry[0]
            try:
                with suppress(Exception):
                    if text:
                        await self.registry.send_text(socket, text)
                if reason:
                    await self.registry.close_socket(socket, reason)
            except BaseException:
                raise
            else:
                self.pending.pop(id(socket), None)
            finally:
                self.inbox.finish(entry)

    async def finish(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Each registry close has its own bounded, cancellation-settled send. An outer
        # timeout cannot interrupt that work and must not escape the handler's finally.
        await asyncio.gather(
            *(self.registry.close_socket(s, "stream_closed") for s in self.pending.values()),
            return_exceptions=True,
        )
        self.pending.clear()
