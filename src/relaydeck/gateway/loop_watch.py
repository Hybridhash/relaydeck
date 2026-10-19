"""Content-free loop latency and blocking-frame attribution without asyncio debug reprs."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import time
from contextlib import suppress
from types import FrameType

LOG = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9.]{0,159}\Z")


def _location(frame: FrameType | None) -> tuple[str, str, int]:
    """Prefer project frames, then innermost external symbols; never paths, locals or reprs."""
    fallback = ("asyncio", "callback", 0)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        function = frame.f_code.co_name
        if isinstance(module, str) and _NAME.fullmatch(module) and _NAME.fullmatch(function):
            if module.startswith("relaydeck."):
                return module, function, frame.f_lineno
            if fallback[2] == 0:
                fallback = (module, function, frame.f_lineno)
        frame = frame.f_back
    return fallback


class LoopWatch:
    """A 100 ms heartbeat plus a daemon sampler; one attribution per stalled heartbeat."""

    def __init__(self, *, interval: float = 0.1, threshold: float = 0.25) -> None:
        self.interval, self.threshold = interval, threshold
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._beat = time.monotonic()
        self._loop_thread = 0

    def start(self) -> None:
        self._beat = time.monotonic()
        self._loop_thread = threading.get_ident()
        self._task = asyncio.create_task(self._heartbeat(), name="loop-heartbeat")
        self._thread = threading.Thread(target=self._sample, name="loop-watchdog", daemon=True)
        self._thread.start()

    async def _heartbeat(self) -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(self.interval)
            self._beat = time.monotonic()
            lag = self._beat - before - self.interval
            if lag > self.threshold:
                LOG.warning("loop_lag ms=%d", round(lag * 1000))

    def _sample(self) -> None:
        reported = 0.0
        while not self._stop.wait(min(self.interval, 0.05)):
            beat = self._beat
            lag = time.monotonic() - beat
            if lag <= self.threshold or beat == reported:
                continue
            frame = sys._current_frames().get(self._loop_thread)
            module, function, line = _location(frame)
            del frame  # Never retain application stack frames beyond this sample.
            LOG.warning(
                "loop_blocked ms=%d callback=%s source=%s:%s:%d",
                round(lag * 1000),
                function,
                module,
                function,
                line,
            )
            reported = beat

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
