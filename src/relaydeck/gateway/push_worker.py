"""One gateway-lifespan owner of the synchronous durable outbox pump."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from relaydeck.gateway.push import ProviderPort, PushStore, pump_outbox


class PushWorker:
    def __init__(
        self,
        store: PushStore,
        provider: ProviderPort,
        *,
        interval: float = 1.0,
        authorized: Callable[[dict[str, Any]], bool] = lambda row: True,
    ) -> None:
        if not 0.01 <= interval <= 60:
            raise ValueError("Invalid push interval")
        self.store = store
        self.provider = provider
        self.authorized = authorized
        self.interval = interval
        self._stop = threading.Event()
        self._wake = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        self.report: dict[str, int] = {}
        self.error = ""

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.report = await asyncio.to_thread(
                    pump_outbox,
                    self.store,
                    self.provider,
                    limit=10,
                    stop=self._stop.is_set,
                    authorized=self.authorized,
                )
                self.error = ""
            except Exception:  # noqa: BLE001 - bounded recovery, no storage/provider content
                self.error = "push_store_unavailable"
            try:
                async with asyncio.timeout(self.interval):
                    await self._wake.wait()
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.task is not None:
            # Do not cancel to_thread while its provider/store are still in use.
            await self.task
        close: Any = getattr(self.provider, "close", None)
        if callable(close):
            close()
