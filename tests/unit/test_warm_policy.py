"""Warm-connection budget, list visits and idle expiry of retained terminals."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from relaydeck.client.features.terminal.key_bar import width_class
from relaydeck.client.retained_sessions import (
    WARM_BUDGET_PHONE,
    WARM_BUDGET_WIDE,
    WARM_IDLE_MS,
    RetainedSession,
    RetainedSessions,
)
from tests.support.scripted_sessions import make_scripted_sessions
from tests.support.ui.instant_open import jobs

STEP_MS = 1000


async def _open_all(
    pool: RetainedSessions, scripted: Any, count: int, now: list[int]
) -> list[RetainedSession]:
    entries = []
    for i in range(count):
        host = f"mac-{i}"
        entry = await pool.acquire(
            host,
            "shell",
            str(i),
            "h1",
            lambda host_id=host: scripted.factory.make_shell(host_id=host_id),
        )
        await entry.session.open(str(i))
        entries.append(entry)
        await jobs(pool)
        now[0] += STEP_MS
    return entries


def _connected(entries: list[RetainedSession]) -> list[bool]:
    return [entry.session.lease is not None for entry in entries]


def _run(body: Callable[[Any, list[int]], Awaitable[None]], *, phone: bool) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        now = [1000]
        pool = RetainedSessions(clock=lambda: now[0])
        pool.set_form_factor(phone)
        try:
            await body((pool, scripted), now)
        finally:
            await pool.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phone", [True, False])
@pytest.mark.parametrize("count", [3, 4, 5])
def test_list_cycles_keep_the_budget_and_evict_nothing(phone: bool, count: int) -> None:
    budget = WARM_BUDGET_PHONE if phone else WARM_BUDGET_WIDE
    expected = min(count, budget + 1)

    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, count, now)
        assert sum(_connected(entries)) == expected
        for _ in range(3):
            pool.release()  # the list
            await jobs(pool)
            assert sum(_connected(entries)) == expected
            now[0] += STEP_MS
        # Returning to the newest keeps everything it had.
        await pool.acquire(
            entries[-1].host_id, "shell", entries[-1].key, "h1", lambda: entries[-1].session
        )
        await jobs(pool)
        assert sum(_connected(entries)) == expected
        assert not any(entry.session.disposed for entry in entries)

    _run(body, phone=phone)


def test_the_foreground_is_never_evicted_and_the_oldest_goes_first() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, WARM_BUDGET_PHONE + 2, now)
        assert pool.foreground is entries[-1]
        assert entries[-1].session.lease is not None
        assert _connected(entries)[:2] == [False, True]

    _run(body, phone=True)


def test_the_budget_shrinks_on_a_form_factor_change_and_trims_only_the_excess() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, WARM_BUDGET_WIDE + 1, now)
        assert all(_connected(entries))
        pool.set_form_factor(True)
        await jobs(pool)
        assert _connected(entries) == [False, True, True, True, True]
        pool.set_form_factor(False)  # growing never reconnects by itself
        await jobs(pool)
        assert _connected(entries) == [False, True, True, True, True]

    _run(body, phone=False)


def test_idle_expiry_drops_only_the_connection_after_the_window() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 3, now)
        pool.release()
        await jobs(pool)
        released = now[0]
        assert all(_connected(entries))
        now[0] = released + WARM_IDLE_MS - STEP_MS * 4
        pool.expire_idle_warm()
        await jobs(pool)
        assert all(_connected(entries))
        now[0] = released + WARM_IDLE_MS
        pool.expire_idle_warm()
        await jobs(pool)
        assert not any(_connected(entries))
        assert pool.entries() == tuple(entries)
        assert not any(entry.session.disposed for entry in entries)

    _run(body, phone=True)


def test_idle_expiry_never_touches_the_foreground() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 2, now)
        now[0] += WARM_IDLE_MS * 4
        pool.expire_idle_warm()
        await jobs(pool)
        assert _connected(entries) == [False, True]
        assert pool.foreground is entries[-1]

    _run(body, phone=True)


def test_no_session_is_disconnected_twice() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 3, now)
        pool.release()
        await jobs(pool)
        calls: list[str] = []
        for entry in entries:
            original = entry.session.disconnect

            async def counted(key: str = entry.key, original: Any = original) -> Any:
                calls.append(key)
                return await original()

            entry.session.disconnect = counted
        now[0] += WARM_IDLE_MS * 2
        pool.expire_idle_warm()
        pool.expire_idle_warm()
        pool.set_form_factor(False)
        pool.set_form_factor(True)
        await jobs(pool)
        pool.expire_idle_warm()
        await jobs(pool)
        assert sorted(calls) == sorted({entry.key for entry in entries})

    _run(body, phone=False)


def test_returning_before_the_window_ends_restarts_the_idle_clock() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 2, now)
        first = entries[0]
        now[0] += WARM_IDLE_MS - STEP_MS
        await pool.acquire(first.host_id, "shell", first.key, "h1", lambda: first.session)
        pool.release()
        now[0] += WARM_IDLE_MS - STEP_MS
        pool.expire_idle_warm()
        await jobs(pool)
        assert first.session.lease is not None

    _run(body, phone=True)


@pytest.mark.parametrize(
    ("width", "compact"),
    [
        (390, True),
        (599, True),
        (600, False),
        (820, False),
        (1023, False),
        (1024, False),
        (1440, False),
    ],
)
def test_the_budget_follows_the_width_class(width: int, compact: bool) -> None:
    assert (width_class(width) == "compact") is compact


def test_an_expired_entry_stays_cold_until_a_real_visit() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 2, now)
        pool.release()
        await jobs(pool)
        now[0] += WARM_IDLE_MS
        assert pool._warm_entries() == ()
        assert not any(pool._watch_allowed(entry) for entry in entries)
        assert not await pool.preconnect(entries[0])
        pool.expire_idle_warm()
        await jobs(pool)
        # A device resume enforces the passed deadline instead of granting a new window.
        pool.set_device_foreground(False)
        pool.set_device_foreground(True)
        await jobs(pool)
        assert not any(_connected(entries))
        first = entries[0]
        await pool.acquire(first.host_id, "shell", first.key, "h1", lambda: first.session)
        assert pool.foreground is first and first.viewed_at is None

    _run(body, phone=True)


def test_a_drop_waiting_on_teardown_never_releases_a_successor_lease() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 2, now)
        victim = entries[0].session
        old = victim.lease
        assert old is not None
        entered, gate = asyncio.Event(), asyncio.Event()
        released: list[str] = []
        original = victim._ops.release_lease

        async def slow_release(lease_id: str, *args: Any, **kwargs: Any) -> Any:
            released.append(lease_id)
            entered.set()
            await gate.wait()
            return await original(lease_id, *args, **kwargs)

        victim._ops.release_lease = slow_release
        task = asyncio.create_task(victim.disconnect())
        await entered.wait()
        successor = dataclasses.replace(old, lease_id="successor-lease")
        victim._lease = successor
        victim._bump_generation("test")
        phase = victim.phase
        gate.set()
        await task
        assert released == [old.lease_id]
        assert victim._lease is successor
        assert victim.phase == phase

    _run(body, phone=True)


def _count_disconnects(entry: RetainedSession) -> list[str]:
    calls: list[str] = []
    original = entry.session.disconnect

    async def counted() -> Any:
        calls.append(entry.key)
        return await original()

    entry.session.disconnect = counted
    return calls


@pytest.mark.parametrize("reason", ["idle", "capacity"])
def test_a_drop_cancelled_before_it_starts_does_not_block_later_eviction(reason: str) -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 3, now)
        target = entries[0]
        calls = _count_disconnects(target)
        pool.release()
        await jobs(pool)

        def evict() -> None:
            if reason == "idle":
                now[0] = (target.viewed_at or 0) + WARM_IDLE_MS
                pool.expire_idle_warm()
            else:
                pool.warm_budget = 0
                pool._limit_warmth()

        evict()
        assert target.dropping and target.warm_task is not None
        # Cancelled (for example by backgrounding) before the coroutine ever ran.
        target.warm_task.cancel()
        assert not target.dropping
        await jobs(pool)
        assert calls == [] and target.session.lease is not None
        evict()
        await jobs(pool)
        assert target.session.lease is None
        assert calls == [target.key]
        evict()
        await jobs(pool)
        assert calls == [target.key]
        assert target.drop_task is None

    _run(body, phone=True)


@pytest.mark.parametrize("stage", ["renewal", "close", "release"])
def test_a_real_takeover_while_a_drop_waits_keeps_the_successor(stage: str) -> None:
    async def scenario() -> None:
        scripted = make_scripted_sessions()
        now = [1000]
        pool = RetainedSessions(clock=lambda: now[0])
        session = scripted.factory.make_agent(
            host_id="mac", host_epoch="h1", agent_id="a", target="a"
        )
        session.open()
        await session.confirm_takeover()
        entry = await pool.acquire("mac", "agent", "a", "h1", lambda: session)
        pool.release()
        await jobs(pool)
        old_controller, old_lease = session.controller, session.lease
        entered, gate = asyncio.Event(), asyncio.Event()
        fired = False

        async def barrier() -> None:
            nonlocal fired
            if not fired:
                fired = True
                entered.set()
                await gate.wait()

        if stage == "renewal":
            target, attr = session._lease_owner.driver, "stop"
        elif stage == "close":
            target, attr = old_controller, "close"
        else:
            target, attr = session._ops, "release_lease"
        original = getattr(target, attr)

        async def blocked(*args: Any, **kwargs: Any) -> Any:
            await barrier()
            return await original(*args, **kwargs)

        setattr(target, attr, blocked)
        try:
            now[0] += WARM_IDLE_MS
            pool.expire_idle_warm()
            drop = entry.warm_task
            assert drop is not None
            await asyncio.wait_for(entered.wait(), 2)
            await pool.acquire("mac", "agent", "a", "h1", lambda: session)
            session.request_takeover()
            assert await session.confirm_takeover() == "live"
            successor, new_lease = session.controller, session.lease
            session._notice = "successor notice"
            gate.set()
            await asyncio.wait_for(drop, 2)
            assert session.controller is successor and successor is not old_controller
            assert session.lease is new_lease and new_lease is not old_lease
            assert new_lease.lease_id not in scripted.ops.releases
            assert session.phase == "live" and session.notice == "successor notice"
            assert not scripted.streams.opened[-1][2].closed
            assert session._lease_owner.running
        finally:
            gate.set()
            setattr(target, attr, original)
            await pool.shutdown()

    asyncio.run(scenario())


def test_backgrounding_before_a_queued_drop_runs_does_not_disable_the_next_expiry() -> None:
    async def body(parts: Any, now: list[int]) -> None:
        pool, scripted = parts
        entries = await _open_all(pool, scripted, 2, now)
        victim = entries[0]
        now[0] += WARM_IDLE_MS
        pool.expire_idle_warm()
        queued = victim.warm_task
        assert queued is not None and victim.dropping
        pool.set_device_foreground(False)  # before the queued drop gets a turn
        await jobs(pool)
        assert queued.cancelled() and not victim.dropping
        pool.set_device_foreground(True)
        await jobs(pool)
        await pool.acquire(victim.host_id, "shell", victim.key, "h1", lambda: victim.session)
        assert await pool.resume_watch(victim)
        pool.release()
        await jobs(pool)
        now[0] += WARM_IDLE_MS
        pool.expire_idle_warm()
        await jobs(pool)
        assert pool._idle_expired(victim)
        assert victim.session.lease is None and not victim.dropping

    _run(body, phone=True)
