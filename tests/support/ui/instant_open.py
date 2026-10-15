"""Instant-open job helper."""

from __future__ import annotations

import asyncio

from relaydeck.client.retained_sessions import RetainedSessions


async def jobs(pool: RetainedSessions) -> None:
    for _ in range(2):
        tasks = [t for e in pool.entries() for t in (e.warm_task, e.capture_task) if t is not None]
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        await asyncio.sleep(0)
