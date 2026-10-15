"""Bounded output history for selection and diagnostics.

Keep the newest 256 KiB per session and count evicted bytes so incomplete
history is explicit. Five retained logs together stay below 2 MiB."""

from __future__ import annotations

from typing import Any

OUTPUT_LOG_LIMIT = 256 * 1024


class BoundedOutputLog:
    """Append-only newest-wins byte log with an explicit cap.

    Supports the bytearray surface sessions relied on (`+=`, `clear`,
    `bytes()`, `len()`); eviction is by oldest byte, never by message.
    """

    def __init__(self, limit: int = OUTPUT_LOG_LIMIT) -> None:
        if limit < 1:
            raise ValueError("output log limit must be >= 1")
        self._limit = limit
        self._buffer = bytearray()
        self._dropped = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def dropped_bytes(self) -> int:
        """Total bytes evicted over the cap (monotonic, never reset)."""
        return self._dropped

    def __iadd__(self, data: bytes) -> BoundedOutputLog:
        self.append(data)
        return self

    def __bytes__(self) -> bytes:
        return bytes(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, BoundedOutputLog):
            return bytes(self) == bytes(other)
        if isinstance(other, bytes | bytearray):
            return bytes(self) == bytes(other)
        return NotImplemented

    def append(self, data: bytes) -> None:
        """Append bytes, evicting the oldest past the cap."""
        if not data:
            return
        self._buffer += data
        overflow = len(self._buffer) - self._limit
        if overflow > 0:
            del self._buffer[:overflow]
            self._dropped += overflow

    def clear(self) -> None:
        """Drop retained bytes (fresh-attach path); the drop count stays."""
        self._buffer.clear()


__all__ = ["OUTPUT_LOG_LIMIT", "BoundedOutputLog"]
