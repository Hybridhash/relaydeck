"""Packaged-runtime connectivity and preference stubs."""

from __future__ import annotations

from typing import Any


class Preferences:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> bool:
        self.values[key] = value
        return True


class Connectivity:
    on_change: Any = None
