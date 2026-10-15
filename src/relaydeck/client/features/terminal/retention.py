"""Bounded retained-terminal registry with detach-only eviction.

Each host has five slots and a five-minute idle expiry. Visible terminals are
protected; idle entries are evicted by idle time then last activity. Admission
fails if every slot is visible. Teardown is awaited before reusing a slot.

Detaching releases the lease while the remote process survives. Releasing a
view keeps its surface buffering through the existing bounded viewer queue.
The registry accepts live surfaces because opening can refuse asynchronously."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

# Retention baseline, mirroring the
# connector-side constants without importing the connector layer.
RETENTION_SLOTS = 5
RETENTION_IDLE_MS = 5 * 60 * 1000


class RetentionFullError(RuntimeError):
    """Retention admission failed: every slot is visible, nothing may be
    evicted. Client-local code; surfaces as an explicit
    admission failure, never a silent drop."""

    code = "admission_failed"

    def __init__(self, visible: int) -> None:
        super().__init__(f"terminal retention full: {visible} slots visible, none evictable")
        self.visible = visible


class RetainedSurface(Protocol):
    """A live terminal surface the registry may evict. `detach` releases
    the lease and stops the local stream; the remote pane/process
    survives (never close)."""

    async def detach(self) -> None: ...


@dataclass
class _Retained:
    surface: RetainedSurface
    last_active_ms: int
    idle_since_ms: int | None = None


class TerminalRegistry:
    """Retained live terminals, five slots per host.

    - `retain` admits a live surface (touching it on a same-key hit);
      a full pool detaches the LRU non-visible entry first, and raises
      `RetentionFullError` when every slot is visible-held.
    - `release` detaches a VIEW (Back navigation): the surface stays live
      and keeps buffering, but becomes idle-evictable. Idempotent; it
      never calls into the surface.
    - `touch` foregrounds a retained entry (idle cleared). Idempotent.
    - `evict_idle` detaches and drops idle entries past the TTL.
      Visible entries are protected even when idle.
    - `detach` / `detach_all` release slots explicitly with awaited
      teardown (lease release; the remote pane survives).
    - `set_visible` marks which keys are on screen; visible keys are
      never evicted by any path.

    The clock is injected (`now_ms`): no sleeps, no wall-clock reads.
    """

    def __init__(self, *, slots: int = RETENTION_SLOTS, idle_ms: int = RETENTION_IDLE_MS) -> None:
        if slots < 1:
            raise ValueError("retention slots must be >= 1")
        self._slots = slots
        self._idle_ms = idle_ms
        self._hosts: dict[str, dict[str, _Retained]] = {}
        self._visible: dict[str, frozenset[str]] = {}

    def keys(self, host_id: str) -> tuple[str, ...]:
        """Retained keys for one host (insertion order)."""
        return tuple(self._hosts.get(host_id, {}))

    def get(self, host_id: str, key: str) -> RetainedSurface | None:
        """Look up a retained surface without touching it (pure read)."""
        hit = self._hosts.get(host_id, {}).get(key)
        return hit.surface if hit is not None else None

    def visible(self, host_id: str) -> frozenset[str]:
        return self._visible.get(host_id, frozenset())

    def set_visible(self, host_id: str, keys: Collection[str]) -> None:
        self._visible[host_id] = frozenset(keys)

    def is_idle(self, host_id: str, key: str) -> bool:
        """Whether the entry is retained and idle (released, awaiting TTL
        or LRU eviction). Missing entries report False."""
        hit = self._hosts.get(host_id, {}).get(key)
        return hit is not None and hit.idle_since_ms is not None

    async def retain(
        self, host_id: str, key: str, surface: RetainedSurface, now_ms: int
    ) -> RetainedSurface:
        """Admit a live surface under `key`. A same-key hit touches the
        EXISTING entry and returns it (the passed surface is ignored, so
        a live entry is never swapped out from under its stream)."""
        entries = self._hosts.setdefault(host_id, {})
        hit = entries.get(key)
        if hit is not None:
            hit.idle_since_ms = None
            hit.last_active_ms = now_ms
            return hit.surface
        if len(entries) >= self._slots:
            victim = self._eviction_candidate(host_id)
            if victim is None:
                visible = sum(1 for k in entries if k in self._visible.get(host_id, frozenset()))
                raise RetentionFullError(visible)
            await self._drop(host_id, victim)
        entries[key] = _Retained(surface, now_ms)
        return surface

    def touch(self, host_id: str, key: str, now_ms: int) -> bool:
        """Foreground a retained entry: idle cleared, recency bumped.
        Returns False when the key is not retained."""
        hit = self._hosts.get(host_id, {}).get(key)
        if hit is None:
            return False
        hit.idle_since_ms = None
        hit.last_active_ms = now_ms
        return True

    def release(self, host_id: str, key: str, now_ms: int) -> None:
        """Detach a view (Back navigation): the surface stays live and
        keeps buffering, but becomes idle-evictable. Idempotent; the
        surface itself is never called."""
        hit = self._hosts.get(host_id, {}).get(key)
        if hit is None:
            return
        hit.idle_since_ms = now_ms
        hit.last_active_ms = now_ms

    async def evict_idle(self, host_id: str, now_ms: int) -> tuple[str, ...]:
        """Detach and drop idle entries past the TTL. Visible entries are
        protected even when idle. Returns the evicted keys."""
        entries = self._hosts.get(host_id, {})
        visible = self._visible.get(host_id, frozenset())
        dead = [
            key
            for key, retained in entries.items()
            if key not in visible
            and retained.idle_since_ms is not None
            and now_ms - retained.idle_since_ms >= self._idle_ms
        ]
        for key in dead:
            await self._drop(host_id, key)
        return tuple(dead)

    async def detach(self, host_id: str, key: str) -> bool:
        """Release one slot explicitly: teardown is awaited before the
        slot is released (the remote pane survives). Idempotent; returns
        False when the key was not retained."""
        if key not in self._hosts.get(host_id, {}):
            return False
        await self._drop(host_id, key)
        return True

    async def detach_all(self, host_id: str | None = None) -> tuple[tuple[str, str], ...]:
        """Release every slot (or one host's slots): each teardown is
        awaited before its slot is released. Returns (host, key) pairs."""
        hosts = [host_id] if host_id is not None else list(self._hosts)
        detached: list[tuple[str, str]] = []
        for host in hosts:
            for key in list(self._hosts.get(host, {})):
                await self._drop(host, key)
                detached.append((host, key))
        return tuple(detached)

    def _eviction_candidate(self, host_id: str) -> str | None:
        """Longest-idle non-visible entry first, else the
        least-recently-active non-visible one. Visible: never."""
        entries = self._hosts.get(host_id, {})
        visible = self._visible.get(host_id, frozenset())
        idle_best: str | None = None
        idle_ms = 0
        active_best: str | None = None
        active_ms = 0
        for key, retained in entries.items():
            if key in visible:
                continue
            if retained.idle_since_ms is not None:
                if idle_best is None or retained.idle_since_ms < idle_ms:
                    idle_best, idle_ms = key, retained.idle_since_ms
            elif active_best is None or retained.last_active_ms < active_ms:
                active_best, active_ms = key, retained.last_active_ms
        return idle_best if idle_best is not None else active_best

    async def _drop(self, host_id: str, key: str) -> None:
        entries = self._hosts.get(host_id)
        if entries is None or key not in entries:
            return
        retained = entries[key]
        try:
            await retained.surface.detach()
        finally:
            entries.pop(key, None)


__all__ = [
    "RETENTION_IDLE_MS",
    "RETENTION_SLOTS",
    "RetainedSurface",
    "RetentionFullError",
    "TerminalRegistry",
]
