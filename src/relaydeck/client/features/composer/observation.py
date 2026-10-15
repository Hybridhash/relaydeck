"""Observe an existing command; never dispatch or replay a prompt."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from relaydeck.contracts.schemas import SchemaError, validate


class CommandOutcomeUnknown(RuntimeError):
    """No terminal outcome was proven. Keep the draft and reconcile only."""


class CommandRefused(RuntimeError):
    def __init__(self, code: str, record: dict[str, Any] | None = None) -> None:
        self.code = code
        self.record = record
        self.message = f"Command refused ({code})."
        super().__init__(self.message)


class CommandObserver:
    def __init__(
        self,
        status: Callable[[str, str], Awaitable[dict[str, Any] | None]],
        *,
        budget_s: float = 30,
        interval_s: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._status = status
        self._budget = budget_s
        self._interval = interval_s
        self._sleep = sleep
        self._monotonic = monotonic

    async def observe(self, host_id: str, command_id: str) -> dict[str, Any]:
        deadline = self._monotonic() + self._budget
        while self._monotonic() < deadline:
            try:
                async with asyncio.timeout(max(0.001, deadline - self._monotonic())):
                    record = await self._status(host_id, command_id)
            except Exception as exc:
                # A refused status query proves nothing about the already
                # submitted command's effect, including auth/transport loss.
                raise CommandOutcomeUnknown("Command outcome unavailable; do not replay.") from exc
            if (
                not isinstance(record, dict)
                or record.get("host_id") != host_id
                or record.get("command_id") != command_id
            ):
                raise CommandOutcomeUnknown("Command observation identity mismatch.")
            try:
                validate("relaydeck/gateway/v1/command-record", record)
            except SchemaError as exc:
                raise CommandOutcomeUnknown("Invalid command observation.") from exc
            state = record.get("status")
            if state == "succeeded":
                return record
            if state in ("failed", "rejected"):
                code = record.get("error_code")
                raise CommandRefused(
                    code if isinstance(code, str) and code else "command_rejected", record
                )
            if state not in ("accepted", "dispatched"):
                raise CommandOutcomeUnknown("Command effect is unknown; do not replay.")
            await self._sleep(min(self._interval, max(0, deadline - self._monotonic())))
        raise CommandOutcomeUnknown("Command observation timed out; do not replay.")
