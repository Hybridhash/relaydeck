"""Reconnect backoff.

Exponential backoff with seeded jitter: deterministic under test, spread
in production. Pure apart from the injected random source.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

BASE_MS = 500
FACTOR = 2.0
MAX_MS = 30_000
JITTER = 0.25


@dataclass
class Backoff:
    attempts: int = 0
    _random: random.Random = field(default_factory=random.Random)

    @classmethod
    def seeded(cls, seed: int) -> Backoff:
        return cls(_random=random.Random(seed))

    def next_delay_ms(self) -> int:
        delay = min(MAX_MS, BASE_MS * (FACTOR ** min(self.attempts, 6)))
        self.attempts = min(self.attempts + 1, 32)
        spread = delay * JITTER
        return int(delay + self._random.uniform(-spread, spread))

    def reset(self) -> None:
        self.attempts = 0
