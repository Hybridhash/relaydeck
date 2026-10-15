"""Polling and watching helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from relaydeck_terminal.terminal import RelayDeckTerminal

from relaydeck.client.features.terminal.warm_watch import watch_only
from tests.support.scripted_sessions import make_scripted_sessions


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def watching() -> tuple[Any, Any, RelayDeckTerminal]:
    scripted = make_scripted_sessions()
    session = scripted.factory.make_agent(host_id="mac", host_epoch="h1", agent_id="a", target="a")
    session.open()
    await session.confirm_takeover()
    term = RelayDeckTerminal()
    session.bind_view(term, write=lambda *_: True)
    session.on_draft_change("unsent draft")
    assert await watch_only(session, lambda: True)
    assert session.lease.mode == "view"
    return scripted, session, term
