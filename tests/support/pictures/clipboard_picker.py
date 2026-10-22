"""Blocked clipboard picker stub."""

from __future__ import annotations

from tests.support.fakes import FakeClipboard


class _Blocked(FakeClipboard):
    def __init__(self, copied: str) -> None:
        super().__init__()
        self._copied = copied

    async def get_text(self) -> str:
        raise TimeoutError()

    def copied_text(self) -> str:
        return self._copied
