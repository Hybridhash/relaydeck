"""Writer generation tracking.

Pure value type: every terminal surface owns a generation counter. Any
event that replaces the writer (attach, takeover, reconnect, reselection,
revocation) bumps the generation; callbacks carrying an older generation
are stale and must be dropped by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GenerationTracker:
    current: dict[str, int] = field(default_factory=dict)

    def begin(self, terminal_id: str) -> tuple[GenerationTracker, int]:
        """Start (or replace) the writer for a terminal; returns the new
        tracker and the fresh generation."""
        generation = self.current.get(terminal_id, 0) + 1
        updated = dict(self.current)
        updated[terminal_id] = generation
        return GenerationTracker(updated), generation

    def invalidate(self, terminal_id: str) -> GenerationTracker:
        """Drop the writer without installing a replacement. In-flight
        callbacks for any generation of this terminal go stale."""
        updated = dict(self.current)
        updated[terminal_id] = updated.get(terminal_id, 0) + 1
        return GenerationTracker(updated)

    def is_current(self, terminal_id: str, generation: int) -> bool:
        return self.current.get(terminal_id, 0) == generation
