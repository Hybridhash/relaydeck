"""Incremental Server-Sent Events parser.

Feeds arbitrary byte chunks and yields complete (event, data) frames on
blank-line boundaries. Comments and split lines are handled; only the
`event:` and `data:` fields are used (multi-line data joins with \\n).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SseFrame:
    event: str
    data: str


@dataclass
class SseParser:
    _buffer: bytearray = field(default_factory=bytearray)
    _event: str = "message"
    _data: list[str] = field(default_factory=list)

    def feed(self, chunk: bytes) -> list[SseFrame]:
        self._buffer += chunk
        frames: list[SseFrame] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline == -1:
                break
            raw = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            line = raw.decode("utf-8", errors="replace")
            if line.endswith("\r"):
                line = line[:-1]
            if line == "":
                if self._data or self._event != "message":
                    frames.append(SseFrame(self._event, "\n".join(self._data)))
                self._event = "message"
                self._data = []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                self._event = line[6:].strip()
            elif line.startswith("data:"):
                self._data.append(line[5:].lstrip(" "))
            # id:/retry: intentionally ignored (cursors live in payloads)
        return frames
