"""Wait for started work to settle before propagating caller cancellation."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager


class OrderedGate:
    """FIFO admission shared by event loops; its tiny mutex never spans an await or I/O."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._tail: Future[None] | None = None

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        released: Future[None] = Future()
        with self._mutex:
            previous, self._tail = self._tail, released
        try:
            if previous is not None:
                # wrap_future wakes each caller's own loop through call_soon_threadsafe.
                await settled(asyncio.wrap_future(previous))
            yield
        finally:
            # A cancelled waiter must first drain its predecessor, never let a successor pass it.
            released.set_result(None)


async def settled[T](future: asyncio.Future[T]) -> T:
    """Cancellation never abandons a commit or releases its serialization lock early."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise


async def run_blocking[T, **P](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Offload blocking work, retaining its lifetime through cancellation."""
    return await settled(asyncio.create_task(asyncio.to_thread(call, *args, **kwargs)))
