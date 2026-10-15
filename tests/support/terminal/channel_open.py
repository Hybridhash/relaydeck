"""Harness channel for terminal channel-open tests."""

from __future__ import annotations

from typing import Any


class _HarnessChannel:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.handler: Any = None

    def on_bytes(self, handler: Any) -> None:
        self.handler = handler

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        pass
