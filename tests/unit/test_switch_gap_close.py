"""Bounded socket close and no stale warmth-drop disconnect."""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any

import pytest
import websockets.asyncio.client as ws_client

from relaydeck.client.features.sync import transport as transport_mod
from relaydeck.client.retained_sessions import RetainedSessions
from tests.support.scripted_sessions import make_scripted_sessions


class _HungWs:
    """A websockets connection whose reader is paused: close() never completes."""

    def __init__(self) -> None:
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1
        await asyncio.sleep(3600)


def test_websocket_close_is_bounded_and_connect_shortens_the_close_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    ws = _HungWs()

    async def fake_connect(url: str, **kwargs: Any) -> _HungWs:
        seen.update(kwargs)
        return ws

    monkeypatch.setattr(ws_client, "connect", fake_connect)
    monkeypatch.setattr(transport_mod, "WS_CLOSE_TIMEOUT_S", 0.05)

    async def scenario() -> float:
        socket = transport_mod.WebsocketTerminalSocket("ws://example.invalid/x")
        await socket.connect()
        started = time.monotonic()
        await asyncio.gather(socket.close(), socket.close())  # second close is a no-op
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert seen["close_timeout"] == 0.05  # not the library's 10 s default
    assert elapsed < 1.0 and ws.closes == 1


def test_default_close_timeout_is_far_below_the_library_default() -> None:
    assert transport_mod.WS_CLOSE_TIMEOUT_S <= 2.0


def test_stale_warmth_drop_cannot_disconnect_a_returned_or_taken_over_entry() -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        pool = RetainedSessions()
        entries = []
        for index, key in enumerate("abcd"):
            entry = await pool.acquire(
                "mac",
                "agent",
                key,
                "h1",
                partial(
                    scripted.factory.make_agent,
                    host_id="mac",
                    host_epoch="h1",
                    agent_id=key,
                    target=key,
                ),
            )
            entry.session.open()
            await entry.session.confirm_takeover()
            entry.last_viewed = index
            entries.append(entry)
        victim, *_, foreground = entries
        pool.foreground = foreground
        release_hold = asyncio.Event()

        async def held() -> None:
            await release_hold.wait()

        victim.warm_task = asyncio.create_task(held())
        before = list(scripted.ops.releases)
        pool._limit_warmth()  # a is the oldest: a drop job is queued behind `held`
        drop = victim.warm_task
        assert drop is not None and drop is not asyncio.current_task()
        # The owner returns to `a` and takes over while the drop job waits.
        pool.foreground = victim
        victim.session.set_foreground(True)
        victim.last_viewed = 99
        victim.session.request_takeover()
        await victim.session.confirm_takeover()
        lease, controller = victim.session.lease, victim.session.controller
        release_hold.set()
        await asyncio.gather(drop, return_exceptions=True)
        assert victim.session.lease is lease and victim.session.controller is controller
        assert lease is not None and lease.lease_id not in scripted.ops.releases
        assert scripted.ops.releases == before
        await pool.shutdown()

    asyncio.run(scenario())
