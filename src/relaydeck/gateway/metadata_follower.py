"""Bounded metadata mailbox with at most one scheduled wake per follower."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any

from relaydeck.gateway.backend import BackendError

MAX_PENDING_METADATA = 128
MAX_METADATA_FOLLOWERS = 1024


class MetadataFollower:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.pending: deque[dict[str, Any]] = deque()
        self.wake = asyncio.Event()
        self.lock = threading.Lock()
        self.notified = False
        self.scheduled = False
        self.overflow = False
        self.closed = False

    def offer(self, event: dict[str, Any]) -> None:
        with self.lock:
            if self.closed or self.overflow:
                return
            if len(self.pending) >= MAX_PENDING_METADATA:
                self.pending.clear()
                self.overflow = True
            else:
                self.pending.append(event)
            if not self.notified and not self.scheduled:
                self.scheduled = True
                try:
                    self.loop.call_soon_threadsafe(self._wake)
                except RuntimeError:
                    self.closed = True
                    self.pending.clear()

    def _wake(self) -> None:
        with self.lock:
            self.scheduled = False
            if not self.closed:
                self.notified = True
                self.wake.set()

    async def next(self, heartbeat_s: float) -> dict[str, Any] | None:
        while True:
            with self.lock:
                if self.overflow:
                    raise BackendError(
                        "stale_epoch", "Metadata overflow; fetch a fresh snapshot", True
                    )
                if self.pending:
                    return self.pending.popleft()
                if self.closed:
                    raise BackendError("host_offline", "Metadata follower closed", True)
                self.wake.clear()
                self.notified = False
            try:
                async with asyncio.timeout(heartbeat_s):
                    await self.wake.wait()
            except TimeoutError:
                return None

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.pending.clear()
