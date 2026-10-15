"""Fake terminal stream: scripted byte player.

Plays bytes (often VT fixture content) with scripted behaviors: normal,
slow drip, stalled, takeover conflict, and mid-stream disconnect. Input
is recorded per writer generation so tests can assert the single-writer
contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from relaydeck.client.adapters.ports import FakeClock


class TakeoverConflictError(RuntimeError):
    def __init__(self, holder: str) -> None:
        super().__init__(f"terminal owned by {holder}")
        self.holder = holder


class StreamDisconnectedError(RuntimeError):
    pass


@dataclass
class FakeTerminalStream:
    terminal_id: str
    clock: FakeClock = field(default_factory=FakeClock)
    output: bytes = b""
    chunk_size: int = 4096
    stalled: bool = False
    disconnected: bool = False
    owner: str | None = None
    _offset: int = 0
    input_log: list[tuple[int, bytes]] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)

    def load(self, data: bytes) -> None:
        self.output = data
        self._offset = 0

    def attach(self, holder: str, generation: int) -> None:
        if self.owner is not None and self.owner != holder:
            raise TakeoverConflictError(self.owner)
        self.owner = holder
        self.generations.append(generation)

    def detach(self, holder: str) -> None:
        if self.owner == holder:
            self.owner = None

    def take_over(self, holder: str, generation: int) -> str:
        """Forced takeover: previous owner is displaced (explicit UX)."""
        previous = self.owner or ""
        self.owner = holder
        self.generations.append(generation)
        return previous

    def read(self, max_bytes: int | None = None) -> bytes:
        if self.disconnected:
            raise StreamDisconnectedError(self.terminal_id)
        if self.stalled or self._offset >= len(self.output):
            return b""
        size = min(self.chunk_size, len(self.output) - self._offset)
        if max_bytes is not None:
            size = min(size, max_bytes)
        chunk = self.output[self._offset : self._offset + size]
        self._offset += size
        return chunk

    def write_input(self, generation: int, data: bytes) -> None:
        if self.disconnected:
            raise StreamDisconnectedError(self.terminal_id)
        if not self.generations or generation != self.generations[-1]:
            raise ValueError(f"stale writer generation {generation}")
        self.input_log.append((generation, data))

    @property
    def consumed(self) -> int:
        return self._offset
