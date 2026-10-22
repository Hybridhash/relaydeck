"""Bounded revocation signals for authenticated metadata streams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import anyio
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class MetadataResponse(StreamingResponse):
    """Close the producer even when ASGI cancellation interrupts a yielded frame."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()


@dataclass
class MetadataSignal:
    account: str
    device_id: str
    host_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    code: str = "unauthenticated"


class MetadataStreams:
    def __init__(self, limit: int = 1024) -> None:
        self.limit = limit
        self._held: dict[int, MetadataSignal] = {}
        self._sequence = 0

    def __len__(self) -> int:
        return len(self._held)

    def register(self, account: str, device_id: str, host_id: str) -> tuple[int, MetadataSignal]:
        if len(self._held) >= self.limit:
            raise RuntimeError("metadata stream capacity reached")
        self._sequence += 1
        signal = MetadataSignal(account, device_id, host_id)
        self._held[self._sequence] = signal
        return self._sequence, signal

    def unregister(self, key: int) -> None:
        self._held.pop(key, None)

    def revoke(self, *, account: str = "", device_id: str = "", host_id: str = "") -> int:
        count = 0
        for signal in self._held.values():
            if (
                (not account or signal.account == account)
                and (not device_id or signal.device_id == device_id)
                and (not host_id or signal.host_id == host_id)
            ):
                signal.code = "not_found" if host_id else "unauthenticated"
                signal.event.set()
                count += 1
        return count
