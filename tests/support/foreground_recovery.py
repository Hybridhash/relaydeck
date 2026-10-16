"""Scripted harness for foreground recovery tests: pool, scripted gateway, injected guards."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from relaydeck.client.features.terminal.foreground_recovery import ForegroundRecovery
from relaydeck.client.retained_sessions import RetainedSession, RetainedSessions
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.instant_open import jobs


class Harness:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.scripted = make_scripted_sessions()
        self.now = [1000]
        self.pool = RetainedSessions(clock=lambda: self.now[0])
        self.stamp = [0, 1]
        self.verified: list[RetainedSession] = []
        self.revoked = False
        self.recovery = ForegroundRecovery(
            retained=self.pool,
            current=self._current,
            stamp=lambda: (self.stamp[0], self.stamp[1]),
            verify=self.verified.append,
        )
        self.entry: RetainedSession | None = None

    def _current(self, entry: RetainedSession) -> bool:
        return (
            not self.revoked
            and self.pool.device_foreground
            and self.pool.foreground is entry
            and not entry.stopped
        )

    async def open(self) -> RetainedSession:
        scripted = self.scripted
        if self.kind == "agent":

            def create() -> Any:
                return scripted.factory.make_agent(
                    host_id="mac", host_epoch="e1", agent_id="a", target="a"
                )

        else:

            def create() -> Any:
                return scripted.factory.make_shell(host_id="mac")

        entry = await self.pool.acquire("mac", self.kind, "a", "e1", create)
        if self.kind == "agent":
            entry.session.open()
            await entry.session.confirm_takeover()
        else:
            await entry.session.open("a")
        await jobs(self.pool)
        self.entry = entry
        return entry

    async def hide(self) -> None:
        self.pool.set_device_foreground(False)
        await jobs(self.pool)

    def show(self) -> None:
        self.stamp[0] += 1
        self.pool.set_device_foreground(True)

    def sent_bytes(self) -> int:
        return sum(
            1
            for _, _, sock in self.scripted.streams.opened
            for kind, _ in sock.sent
            if kind == "bytes"
        )


def run_recovery(kind: str, body: Callable[[Harness], Awaitable[None]]) -> None:
    async def scenario() -> None:
        harness = Harness(kind)
        try:
            await body(harness)
        finally:
            harness.recovery.close()
            await harness.pool.shutdown()

    asyncio.run(scenario())
