"""Scripted connector transport."""

from __future__ import annotations

import asyncio


class _ScriptedTransport:
    """In-memory tunnel: scripted inbound, recorded outbound."""

    def __init__(self, inbound: list[str]) -> None:
        self.inbound = list(inbound)
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def recv_text(self) -> str:
        if self.inbound:
            return self.inbound.pop(0)
        await asyncio.sleep(3600.0)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True
