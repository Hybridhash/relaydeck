"""Virtual deadlines for one hook module; Flet rendering and writer tasks stay real."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _Timer:
    deadline: float
    callback: Callable[..., Any]
    args: tuple[Any, ...]
    cancelled: bool = False
    fired: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class HookClock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: str) -> None:
        self.now = 0.0
        self.timers: list[_Timer] = []
        loop = asyncio.get_running_loop()
        original = loop.call_later

        def schedule(
            delay: float, callback: Callable[..., Any], *args: Any, context: Any = None
        ) -> Any:
            if getattr(callback, "__module__", None) != module:
                return original(delay, callback, *args, context=context)
            timer = _Timer(self.now + delay, callback, args)
            self.timers.append(timer)
            return timer

        monkeypatch.setattr(loop, "call_later", schedule)

    def advance_by(self, duration: float) -> None:
        self.advance_to(self.now + duration)

    def advance_to(self, target: float) -> None:
        assert target >= self.now
        while due := [
            timer
            for timer in self.timers
            if not timer.cancelled and not timer.fired and timer.deadline <= target
        ]:
            timer = min(due, key=lambda item: item.deadline)
            self.now = timer.deadline
            timer.fired = True
            timer.callback(*timer.args)
        self.now = target
