"""Loop-owned attachment authority; isolated spawn/reap work on one ordered lane."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable

from relaydeck.connector.attach import AttachError, AttachSession
from relaydeck.offload import run_blocking
from relaydeck.tunnel_queue import BoundedInbox


class AttachLifecycle:
    def __init__(self, wake: Callable[[], None]) -> None:
        self.wake = wake
        self.inbox: BoundedInbox[Callable[[], Awaitable[None]]] = BoundedInbox(
            32, 64, control_reserve=32
        )
        self.worker: asyncio.Task[None] | None = None
        self.closed = False

    def _admit(self, call: Callable[[], Awaitable[None]], *, close: bool = False) -> None:
        if self.closed:
            raise RuntimeError("attach_lifecycle_closed")
        self.inbox.put(call, 1, control=close)
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())

    def spawn(self, session: AttachSession) -> None:
        if session.spawn_pending or session._proc is not None:
            raise AttachError("already spawned")
        session._lifecycle_revision += 1
        revision = session._lifecycle_revision
        child = copy.copy(session)
        child.lifecycle = None
        child._stderr_prefix = bytearray()
        session.spawn_pending = True
        session._spawn_done = ready = asyncio.Event()

        async def work() -> None:
            if session._lifecycle_revision != revision:
                return
            try:
                await run_blocking(child.spawn)
            except Exception:  # noqa: BLE001 - fixed failure state, no argv/exception logging
                try:
                    await run_blocking(child.close)
                finally:
                    if session._lifecycle_revision == revision:
                        session.spawn_pending = False
                        session.exit_status = -1
                        self.wake()
                return
            if session._lifecycle_revision != revision:
                await run_blocking(child.close)
                return
            # The worker touched only the isolated child. Publish its handles on the loop.
            session._proc, child._proc = child._proc, None
            session._fd, child._fd = child._fd, None
            session._stderr_category = child._stderr_category
            session.spawn_pending = False
            self.wake()

        async def settled_spawn() -> None:
            try:
                await work()
            finally:
                ready.set()

        try:
            self._admit(settled_spawn)
        except BaseException:
            session.spawn_pending = False
            ready.set()
            raise

    def close_session(self, session: AttachSession, timeout: float) -> int | None:
        self.wake()  # Remove loop FD registrations before ownership moves to the worker.
        session._lifecycle_revision += 1
        session.spawn_pending = False
        if session._spawn_done is not None:
            session._spawn_done.set()
        if session._proc is None and session._fd is None:
            return session.exit_status
        child = copy.copy(session)
        child.lifecycle = None

        async def work() -> None:
            await run_blocking(child.close, timeout)

        self._admit(work, close=True)
        session._proc = None
        session._fd = None
        return session.exit_status

    async def _run(self) -> None:
        while True:
            entry = await self.inbox.get()
            try:
                await entry[0]()
            except Exception:  # noqa: BLE001 - captured cleanup cannot strand later work
                logging.getLogger(__name__).warning(
                    "attach_lifecycle_failed reason=process_cleanup"
                )
            finally:
                self.inbox.finish(entry)

    async def idle(self) -> None:
        await self.inbox.queue.join()

    async def close(self) -> None:
        await self.idle()
        self.closed = True
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
