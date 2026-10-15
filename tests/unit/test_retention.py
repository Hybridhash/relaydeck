"""client-side retained-terminal registry.

Pure registry behavior against stub surfaces (no Flet, no network, no
sleeps — the clock is injected): per-host five-slot cap, five-minute
idle expiry, visible protection, LRU order, detach-not-close eviction,
explicit admission failure, awaited teardown, and bounded offscreen
buffering through the pre-existing viewer queue (verified, not rebuilt).
"""

from __future__ import annotations

import asyncio

import pytest
from relaydeck_terminal.protocol import VIEWER_QUEUE_LIMIT
from relaydeck_terminal.terminal import RelayDeckTerminal, TerminalOverflowError

from relaydeck.client.features.terminal.retention import (
    RETENTION_IDLE_MS,
    RETENTION_SLOTS,
    RetentionFullError,
    TerminalRegistry,
)
from relaydeck.client.features.terminal.screen import deliver_output


class _StubSurface:
    """Retained-surface stub: detach releases the lease; the remote
    pane/process survives (never close). Deliberately has no `close`."""

    def __init__(self) -> None:
        self.detaches = 0
        self.lease_live = True
        self.remote_alive = True

    async def detach(self) -> None:
        self.detaches += 1
        self.lease_live = False
        assert self.remote_alive


def test_retention_baseline_constants() -> None:
    assert RETENTION_SLOTS == 5
    assert RETENTION_IDLE_MS == 5 * 60 * 1000


def test_cap_and_lru_eviction_per_host() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        held = [_StubSurface() for _ in range(5)]
        for index, surface in enumerate(held):
            kept = await registry.retain("home-mac", f"t{index}", surface, index * 1000)
            assert kept is surface
        assert registry.keys("home-mac") == ("t0", "t1", "t2", "t3", "t4")
        # Other hosts are independent pools.
        other = _StubSurface()
        await registry.retain("office-mac", "x0", other, 0)
        assert registry.keys("office-mac") == ("x0",)
        # Foreground t0: t1 becomes the least-recently-active victim.
        assert registry.touch("home-mac", "t0", 9000) is True
        assert registry.touch("home-mac", "missing", 9000) is False
        sixth = _StubSurface()
        assert await registry.retain("home-mac", "t5", sixth, 9000) is sixth
        assert registry.keys("home-mac") == ("t0", "t2", "t3", "t4", "t5")
        assert held[1].detaches == 1
        assert held[1].lease_live is False
        assert held[1].remote_alive is True
        for index in (0, 2, 3, 4):
            assert held[index].detaches == 0
        assert other.detaches == 0
        assert registry.get("home-mac", "t1") is None
        assert registry.get("home-mac", "t0") is held[0]

    asyncio.run(scenario())


def test_idle_expiry_five_minutes_visible_protected() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        idle = _StubSurface()
        seen = _StubSurface()
        await registry.retain("h", "idle", idle, 0)
        await registry.retain("h", "seen", seen, 0)
        registry.release("h", "idle", 1000)
        registry.release("h", "seen", 1000)
        registry.release("h", "missing", 1000)  # idempotent no-op
        assert registry.is_idle("h", "idle") is True
        assert registry.is_idle("h", "missing") is False
        registry.set_visible("h", ["seen"])
        assert registry.visible("h") == frozenset({"seen"})
        assert await registry.evict_idle("h", 1000 + RETENTION_IDLE_MS - 1) == ()
        assert idle.detaches == 0
        assert await registry.evict_idle("h", 1000 + RETENTION_IDLE_MS) == ("idle",)
        assert idle.detaches == 1
        assert idle.remote_alive is True
        # Visible stays even far past the TTL.
        assert await registry.evict_idle("h", 1000 + 10 * RETENTION_IDLE_MS) == ()
        assert seen.detaches == 0
        assert registry.keys("h") == ("seen",)

    asyncio.run(scenario())


def test_lru_evicts_longest_idle_first() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        surfaces = {key: _StubSurface() for key in ("a", "b", "c", "d", "e")}
        for key, surface in surfaces.items():
            await registry.retain("h", key, surface, 0)
        registry.release("h", "b", 200)
        registry.release("h", "a", 100)
        assert await registry.retain("h", "f", _StubSurface(), 1000) is not None
        assert surfaces["a"].detaches == 1  # longest idle, not insertion order
        assert surfaces["b"].detaches == 0
        assert await registry.retain("h", "g", _StubSurface(), 2000) is not None
        assert surfaces["b"].detaches == 1
        assert registry.keys("h") == ("c", "d", "e", "f", "g")

    asyncio.run(scenario())


def test_eviction_detaches_and_never_closes() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        victim = _StubSurface()
        assert not hasattr(victim, "close")  # the registry cannot end the pane
        await registry.retain("h", "victim", victim, 0)
        for index in range(4):
            await registry.retain("h", f"other{index}", _StubSurface(), 0)
        await registry.retain("h", "new", _StubSurface(), 1000)
        assert victim.detaches == 1
        assert victim.lease_live is False
        assert victim.remote_alive is True

    asyncio.run(scenario())


def test_admission_fails_when_all_slots_visible() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        held = [_StubSurface() for _ in range(5)]
        for index, surface in enumerate(held):
            await registry.retain("h", f"t{index}", surface, 0)
        registry.set_visible("h", [f"t{index}" for index in range(5)])
        with pytest.raises(RetentionFullError) as exc:
            await registry.retain("h", "t5", _StubSurface(), 1000)
        assert exc.value.code == "admission_failed"
        assert exc.value.visible == 5
        assert registry.keys("h") == ("t0", "t1", "t2", "t3", "t4")
        assert [surface.detaches for surface in held] == [0, 0, 0, 0, 0]
        # One hidden slot admits again (evicting the hidden entry).
        registry.set_visible("h", ["t1", "t2", "t3", "t4"])
        await registry.retain("h", "t5", _StubSurface(), 2000)
        assert held[0].detaches == 1

    asyncio.run(scenario())


def test_same_key_retain_touches_existing_entry() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        first = _StubSurface()
        assert await registry.retain("h", "a", first, 0) is first
        registry.release("h", "a", 100)
        assert registry.is_idle("h", "a") is True
        second = _StubSurface()
        assert await registry.retain("h", "a", second, 200) is first
        assert registry.is_idle("h", "a") is False
        assert second.detaches == 0
        assert registry.keys("h") == ("a",)
        # The touched entry survives the TTL sweep (idle cleared).
        assert await registry.evict_idle("h", 200 + RETENTION_IDLE_MS) == ()

    asyncio.run(scenario())


def test_teardown_awaited_before_slot_release() -> None:
    async def scenario() -> None:
        registry = TerminalRegistry()
        gate = asyncio.Event()
        events: list[str] = []

        class _Blocking:
            async def detach(self) -> None:
                events.append("detach-start")
                await gate.wait()
                events.append("detach-done")

        for index in range(4):
            await registry.retain("h", f"k{index}", _StubSurface(), index)
        await registry.retain("h", "slow", _Blocking(), -1000)  # LRU victim
        task = asyncio.create_task(registry.retain("h", "new", _StubSurface(), 5000))
        await asyncio.sleep(0.05)
        assert events == ["detach-start"]
        assert "new" not in registry.keys("h")  # slot held until teardown completes
        assert "slow" in registry.keys("h")
        gate.set()
        await task
        assert events == ["detach-start", "detach-done"]
        assert "slow" not in registry.keys("h")
        assert "new" in registry.keys("h")
        # Explicit detach also completes teardown before returning.
        assert await registry.detach("h", "k0") is True  # free a slot first
        done = _StubSurface()
        await registry.retain("h", "done", done, 6000)
        assert await registry.detach("h", "done") is True
        assert done.detaches == 1
        assert "done" not in registry.keys("h")
        assert await registry.detach("h", "done") is False
        remaining = registry.keys("h")
        pairs = await registry.detach_all("h")
        assert sorted(pairs) == sorted([("h", key) for key in remaining])
        assert registry.keys("h") == ()
        assert await registry.detach_all() == ()

    asyncio.run(scenario())


def test_offscreen_retained_surfaces_keep_bounded_buffering() -> None:
    """`release` never touches the surface (still live, still buffering);
    the bound is the pre-existing viewer queue (verified, not rebuilt)."""

    async def scenario() -> None:
        registry = TerminalRegistry()
        surface = _StubSurface()
        await registry.retain("h", "bg", surface, 0)
        registry.release("h", "bg", 100)  # Back: view detached, stream live
        assert surface.detaches == 0
        assert surface.lease_live is True
        assert registry.get("h", "bg") is surface

    asyncio.run(scenario())
    term = RelayDeckTerminal()
    assert term.overflowed is False
    term.write(b"x" * 1024)  # small writes buffer fine while retained
    assert term.overflowed is False
    with pytest.raises(TerminalOverflowError):
        term.write(b"x" * (VIEWER_QUEUE_LIMIT + 1))
    assert term.overflowed is True
    assert deliver_output(term, b"more") is False
