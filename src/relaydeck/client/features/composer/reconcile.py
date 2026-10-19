"""Read-only reconciliation of a send whose outcome became unknown.

Only the command's status is read, by id; nothing is ever submitted again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from relaydeck.client.features.composer.observation import CommandRefused

RECONCILE_BUDGET_S = 20.0
RECONCILE_INTERVAL_S = 1.0

DELIVERED = "delivered"
FAILED = "failed"
UNKNOWN = "unknown"


async def reconcile_command(
    observe: Callable[[str, str], Awaitable[dict[str, Any]]],
    host_id: str,
    command_id: str,
    *,
    budget_s: float = RECONCILE_BUDGET_S,
    interval_s: float = RECONCILE_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[str, CommandRefused | None]:
    """Return (outcome, refusal): delivered, failed (with the refusal) or unknown.

    A status read that fails (the connection is still down) is retried until the budget ends.
    """
    deadline = monotonic() + budget_s
    while True:
        try:
            async with asyncio.timeout(max(0.001, deadline - monotonic())):
                await observe(host_id, command_id)
            return DELIVERED, None
        except CommandRefused as refusal:
            return FAILED, refusal
        except Exception:  # noqa: BLE001 - nothing proven yet: read again within the budget
            if monotonic() + interval_s >= deadline:
                return UNKNOWN, None
        await sleep(interval_s)
