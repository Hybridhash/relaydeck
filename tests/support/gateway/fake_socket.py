"""Fake WebSocket used by host and device management tests."""

from __future__ import annotations


class _FakeSocket:
    def __init__(self) -> None:
        self.closes: list[tuple[int, str]] = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closes.append((code, reason))
