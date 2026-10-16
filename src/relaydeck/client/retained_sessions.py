"""Root-owned retained session lifetime over the existing terminal registry."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from relaydeck_terminal.diagnostics import trace_lifecycle

from relaydeck.client.features.terminal.geometry import page_geometry
from relaydeck.client.features.terminal.retention import TerminalRegistry
from relaydeck.client.features.terminal.screen_cache import ScreenCache, ScreenSnapshot
from relaydeck.client.features.terminal.stream import EMULATOR_FRESH
from relaydeck.client.features.terminal.surfaces.sessions import drive_recovery
from relaydeck.client.features.terminal.ui_resume import terminal_transition
from relaydeck.client.features.terminal.warm_watch import fence_hidden_writer, watch_only
from relaydeck.client.sessions import session_live

# Connections kept warm besides the one on screen: a phone keeps fewer than a tablet or desktop.
WARM_BUDGET_PHONE = 3
WARM_BUDGET_WIDE = 4
# A hidden terminal not viewed for this long drops its connection but keeps its screen.
WARM_IDLE_MS = 15 * 60 * 1000
# Whole-slot expiry stays well past the connection expiry so the screen outlives the connection.
SLOT_IDLE_MS = 2 * WARM_IDLE_MS


@dataclass(eq=False)
class RetainedSession:
    owner: RetainedSessions
    host_id: str
    key: str
    kind: str
    epoch: str
    session: Any
    serial: int
    task: asyncio.Task[Any] | None = None
    unsubscribe: Callable[[], None] | None = None
    stopped: bool = False
    warm_task: asyncio.Task[Any] | None = None
    capture_task: asyncio.Task[Any] | None = None
    last_viewed: int = 0
    viewed_at: int | None = None
    drop_task: asyncio.Task[Any] | None = None
    updating_position: tuple[Any, int] | None = None
    watch_blocked: bool = False
    ui_recovering: bool = False
    ui_remount_generation: int = -1
    ui_term: Any = None
    ui_unsubscribe: Callable[[], None] | None = None

    @property
    def dropping(self) -> bool:
        # Derived from the task itself, so a drop cancelled before it ever ran cannot leave a
        # stale marker that blocks every later eviction.
        task = self.drop_task
        return task is not None and not task.done() and not task.cancelling()

    @property
    def cache_key(self) -> tuple[str, str, str]:
        return self.host_id, self.kind, self.key

    @property
    def native_ready(self) -> bool:
        session = self.session
        attachment = session.attachment
        return bool(
            session.current_term is not None
            and session.controller is not None
            and session.controller.live_output_received
            and attachment is not None
            and session.term_state.status == "live"
            and session.term_state.epoch == attachment.epoch
            and getattr(session.current_term, "transport_verified", True)
        )

    def observe(self, session: Any) -> None:
        self.owner.reconcile_ui(self)
        self.owner.recovery_note(self)
        self.supervise(session)
        snapshot = self.owner.screens.get(self.cache_key, self.epoch, session.attachment)
        if session.term_state.status == "reset" or (
            snapshot is not None
            and self.native_ready
            and (snapshot.stream_epoch, snapshot.applied_offset)
            != (session.term_state.epoch, session.term_state.applied_offset)
        ):
            # Hidden output advances the emulator even with no overlay mounted.
            self.owner.screens.discard(self.cache_key)
            snapshot = None
        if session.cached_screen:
            position = (session.controller, session.term_state.applied_offset)
            if (
                snapshot is None
                or self.native_ready
                or session.term_state.status == "reset"
                or (self.updating_position is not None and position[1] != self.updating_position[1])
            ):
                session.cached_screen = ""
                self.updating_position = None
                self.owner.changed()

    @property
    def slot(self) -> str:
        return f"{self.kind}:{self.key}"

    @property
    def render_key(self) -> str:
        return f"{self.host_id}:{self.slot}:{self.epoch}:{self.serial}"

    def supervise(self, session: Any) -> None:
        if (
            not self.stopped
            and not self.ui_recovering
            and session_live(session)
            and (self.task is None or self.task.done())
        ):
            controller = getattr(session, "controller", None)
            self.task = asyncio.create_task(session.run())

            def finished(done: asyncio.Task[Any]) -> None:
                # A replacement may become live before the old reader finishes.
                # Never restart the same ended reader or replay input.
                if not done.cancelled() and controller is not getattr(session, "controller", None):
                    self.supervise(session)

            self.task.add_done_callback(finished)

    async def detach(self) -> None:
        """Eviction releases local ownership and data; never closes a host process."""
        self.stopped = True
        self.session.set_foreground(False)
        for task in (self.warm_task, self.capture_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.unsubscribe is not None:
            self.unsubscribe()
            self.unsubscribe = None
        if self.ui_unsubscribe is not None:
            self.ui_unsubscribe()
            self.ui_unsubscribe = None
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        try:
            await self.session.dispose(clear_sensitive=True)
        except Exception:
            self.owner.error = "retained_detach_failed"
        finally:
            self.session._clear_sensitive_caches()
            self.owner._removed(self)


class RetainedSessions:
    def __init__(
        self,
        changed: Callable[[], None] = lambda: None,
        *,
        registry: TerminalRegistry | None = None,
        clock: Callable[[], int] = lambda: int(time.monotonic() * 1000),
    ) -> None:
        self.registry = registry or TerminalRegistry(idle_ms=SLOT_IDLE_MS)
        self.clock = clock
        self.changed = changed
        self.foreground: RetainedSession | None = None
        self._entries: dict[tuple[str, str], RetainedSession] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.error = ""
        self._serial = 0
        self.screens = ScreenCache()
        self.device_foreground = True
        self._cache_revision = 0
        self._visits = 0
        self.warm_budget = WARM_BUDGET_PHONE
        self.reconcile_ui: Callable[[RetainedSession], None] = lambda entry: None
        self.recovery_note: Callable[[RetainedSession], None] = lambda entry: None

    def _warm_entries(self) -> tuple[RetainedSession, ...]:
        # With no terminal on screen (the list) the foreground slot is free, so nothing is evicted
        # just because the user looked at the list.
        limit = self.warm_budget + (0 if self.foreground is not None else 1)
        return tuple(
            sorted(
                (
                    e
                    for e in self.entries()
                    if e is not self.foreground
                    and not e.stopped
                    and e.session.attachment is not None
                    and not e.session.paused
                    # Expired entries stay cold until a real visit; no late job revives them.
                    and not self._idle_expired(e)
                ),
                key=lambda e: e.last_viewed,
                reverse=True,
            )[:limit]
        )

    def _watch_allowed(self, entry: RetainedSession) -> bool:
        return (
            not self._closed
            and self.device_foreground
            and not entry.stopped
            and not entry.watch_blocked
            and self.get(entry.host_id, entry.kind, entry.key) is entry
            and (entry is self.foreground or entry in self._warm_entries())
        )

    async def preconnect(self, entry: RetainedSession) -> bool:
        # Capture intent BEFORE waiting on the prior warm job. A takeover or
        # explicit reconnect during that wait must not become this job's new
        # control lease to release/demote when it eventually resumes.
        session = entry.session
        operation = session.interaction_generation
        revision = getattr(session, "_watch_recovery_revision", None)
        original_attachment = session.attachment
        snapshot = self.screens.get(entry.cache_key, entry.epoch, original_attachment)
        attachment = original_attachment or (snapshot.attachment if snapshot else None)

        def current() -> bool:
            return (
                self._watch_allowed(entry)
                and session._operation_current(operation)
                and getattr(session, "_watch_recovery_revision", None) == revision
                and (session.attachment is original_attachment or session.attachment is attachment)
            )

        pending = entry.warm_task
        if pending is not None and pending is not asyncio.current_task():
            await asyncio.gather(pending, return_exceptions=True)
        if not current():
            return False
        if attachment is None:
            return False  # First attach still belongs to the existing explicit flow.
        # A canceled conversion can retire the controller while the same native
        # viewer and its applied cursor survive. Preserve that cursor only for
        # the matching attachment epoch, never from a cached text preview.
        state = session.term_state
        preserved = (
            state
            if session.current_term is not None and state.epoch == attachment.epoch and state.epoch
            else None
        )
        success = await watch_only(session, current, attachment, preserved_state=preserved)
        if not success and current():
            self.screens.discard(entry.cache_key)
            entry.session.cached_screen = ""
            entry.session._notify()
        return success

    async def resume_watch(self, entry: RetainedSession) -> bool:
        """Locally resume a validated retained entry, then request VIEW only."""
        session = entry.session
        operation = session.interaction_generation
        revision = getattr(session, "_watch_recovery_revision", None)
        attachment = session.attachment
        pending = entry.warm_task
        if pending is not None and pending is not asyncio.current_task():
            await asyncio.gather(pending, return_exceptions=True)
        if (
            not self._watch_allowed(entry)
            or session.disposed
            or session.interaction_generation != operation
            or getattr(session, "_watch_recovery_revision", None) != revision
            or session.attachment is not attachment
        ):
            return False
        entry.session._paused = False
        entry.session.set_foreground(entry is self.foreground)
        return await self.preconnect(entry)

    def _capture(self, entry: RetainedSession) -> None:
        term, attachment = entry.session.current_term, entry.session.attachment
        if term is None or attachment is None:
            return
        revision = self._cache_revision
        generation = getattr(term, "viewer_generation", 0)
        position = (entry.session.term_state.epoch, entry.session.term_state.applied_offset)

        async def capture() -> None:
            try:
                text = await term.get_snapshot_text(timeout=0.5)
                if (
                    not self._closed
                    and revision == self._cache_revision
                    and not entry.stopped
                    and self.get(entry.host_id, entry.kind, entry.key) is entry
                    and entry.session.current_term is term
                    and getattr(term, "viewer_generation", 0) == generation
                    and entry.session.attachment is attachment
                    and position
                    == (entry.session.term_state.epoch, entry.session.term_state.applied_offset)
                ):
                    self.screens.put(
                        entry.cache_key, ScreenSnapshot(text, entry.epoch, attachment, *position)
                    )
            except Exception:
                pass  # No byte-log fallback: cache only emulator screen truth.

        if entry.capture_task is not None:
            entry.capture_task.cancel()
        entry.capture_task = asyncio.create_task(capture())

    def set_form_factor(self, phone: bool) -> None:
        """A budget change trims only the excess, least recently viewed first."""
        budget = WARM_BUDGET_PHONE if phone else WARM_BUDGET_WIDE
        if budget == self.warm_budget:
            return
        self.warm_budget = budget
        self._limit_warmth()

    def _schedule_drop(
        self, entry: RetainedSession, wanted: Callable[[RetainedSession], bool]
    ) -> None:
        """Disconnect a hidden entry once its previous job settles, unless it is wanted again."""
        if entry.dropping:
            return
        pending = entry.warm_task
        if pending is not None:
            pending.cancel()

        async def drop(victim: RetainedSession = entry, previous: Any = pending) -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            # The decision was made before waiting for the previous job (which can take
            # seconds). The owner may have returned, or taken over, meanwhile: never
            # disconnect a session that is foreground or wanted again now.
            if (
                self._closed
                or victim.stopped
                or victim is self.foreground
                or wanted(victim)
                or self.get(victim.host_id, victim.kind, victim.key) is not victim
            ):
                return
            await victim.session.disconnect()

        task = asyncio.create_task(drop())

        def settled(done: asyncio.Task[Any]) -> None:
            # An older task finishing must not clear a newer task's ownership.
            if entry.drop_task is done:
                entry.drop_task = None

        task.add_done_callback(settled)
        entry.drop_task = task
        entry.warm_task = task

    def _connected(self, entry: RetainedSession) -> bool:
        return entry.session.lease is not None or (
            entry.warm_task is not None and not entry.warm_task.done()
        )

    def _limit_warmth(self) -> None:
        allowed = self._warm_entries()
        for entry in self.entries():
            if entry is self.foreground or entry in allowed or not self._connected(entry):
                continue
            self._schedule_drop(entry, lambda victim: victim in self._warm_entries())

    def _idle_expired(self, entry: RetainedSession) -> bool:
        return entry.viewed_at is not None and self.clock() - entry.viewed_at >= WARM_IDLE_MS

    def expire_idle_warm(self) -> None:
        """Quietly drop the connection of hidden terminals not viewed for the idle window.

        The retained screen stays; reopening reconnects through the normal path.
        """
        if not self._closed:
            self._limit_warmth()

    def set_device_foreground(self, active: bool) -> None:
        self.device_foreground = active
        if active:
            # Deadlines that passed while suspended are enforced, not renewed.
            self._limit_warmth()
            foreground_entry = self.foreground
            if foreground_entry is not None:
                resume_target: RetainedSession = foreground_entry
                pending = foreground_entry.warm_task

                async def local_resume(
                    victim: RetainedSession = resume_target, previous: Any = pending
                ) -> None:
                    if previous is not None:
                        await asyncio.gather(previous, return_exceptions=True)
                    if (
                        not self._closed
                        and self.device_foreground
                        and self.foreground is victim
                        and not victim.stopped
                        and not victim.watch_blocked
                    ):
                        # Foreground actions (including metadata Retry) become
                        # usable again. The closed reader/writer stays closed;
                        # only explicit reconnect/takeover opens another stream.
                        victim.session._paused = False
                        victim.session.set_foreground(True)
                        victim.session._notify()

                foreground_entry.warm_task = asyncio.create_task(local_resume())
        else:
            for entry in self.entries():
                fence_hidden_writer(entry.session)
                if entry.session.controller is not None:
                    entry.session.controller.disconnected()
                entry.session.set_foreground(False)
                pending = entry.warm_task
                if pending is not None:
                    pending.cancel()
                entry.session.suspend_input()

                async def pause(victim: RetainedSession = entry, previous: Any = pending) -> None:
                    if previous is not None:
                        await asyncio.gather(previous, return_exceptions=True)
                    await victim.session.pause()

                entry.warm_task = asyncio.create_task(pause())

    def entries(self) -> tuple[RetainedSession, ...]:
        return tuple(self._entries.values())

    def known_geometry(self) -> tuple[int, int] | None:
        """The terminal area's last measured size on this page (foreground terminal first)."""
        ordered = sorted(self.entries(), key=lambda entry: entry is not self.foreground)
        return page_geometry(entry.session.current_term for entry in ordered)

    def get(self, host_id: str, kind: str, key: str) -> RetainedSession | None:
        return self._entries.get((host_id, f"{kind}:{key}"))

    def _removed(self, entry: RetainedSession) -> None:
        trace_lifecycle(
            "retained_removed", owner=id(self), serial=entry.serial, session=id(entry.session)
        )
        self._entries.pop((entry.host_id, entry.slot), None)
        if self.foreground is entry:
            self.foreground = None
        self.changed()

    def release(self) -> None:
        entry, self.foreground = self.foreground, None
        if entry is None:
            return
        trace_lifecycle(
            "retained_hidden", owner=id(self), serial=entry.serial, session=id(entry.session)
        )
        entry.session.set_foreground(False)
        fence_hidden_writer(entry.session)
        self._visits += 1
        entry.last_viewed = self._visits
        entry.viewed_at = self.clock()
        self._capture(entry)
        if self._watch_allowed(entry):
            entry.warm_task = asyncio.create_task(self.preconnect(entry))
        self._limit_warmth()
        self.registry.set_visible(entry.host_id, ())
        self.registry.release(entry.host_id, entry.slot, self.clock())
        self.changed()

    async def acquire(
        self,
        host_id: str,
        kind: str,
        key: str,
        epoch: str,
        create: Callable[[], Any],
        *,
        before_foreground: Callable[[Any], None] | None = None,
    ) -> RetainedSession:
        async with self._lock:
            if self._closed:
                raise RuntimeError("retained sessions are shut down")
            entry = self.get(host_id, kind, key)
            if entry is not None and entry.epoch != epoch:
                await self.registry.detach(host_id, entry.slot)
                entry = None
            cached = entry is not None
            if entry is None:
                self._serial += 1
                entry = RetainedSession(self, host_id, key, kind, epoch, create(), self._serial)
                try:
                    await self.registry.retain(host_id, entry.slot, entry, self.clock())
                except BaseException:
                    await entry.detach()
                    raise
                self._entries[(host_id, entry.slot)] = entry
                entry.unsubscribe = entry.session.subscribe(entry.observe)
            if self.foreground is not entry:
                self.release()
            if before_foreground is not None:
                before_foreground(entry.session)
            self.foreground = entry
            trace_lifecycle(
                "retained_acquired",
                owner=id(self),
                serial=entry.serial,
                session=id(entry.session),
                cached=cached,
            )
            entry.session.set_foreground(True)
            self.reconcile_ui(entry)
            self._visits += 1
            entry.last_viewed = self._visits
            entry.viewed_at = None  # the idle window starts when it leaves the screen
            snapshot = self.screens.get(entry.cache_key, epoch, entry.session.attachment)
            if snapshot is not None and not entry.native_ready:
                entry.session.cached_screen = snapshot.text
                entry.updating_position = (
                    entry.session.controller,
                    entry.session.term_state.applied_offset,
                )
            else:
                entry.session.cached_screen = ""
                entry.updating_position = None
            self.registry.touch(host_id, entry.slot, self.clock())
            self.registry.set_visible(host_id, (entry.slot,))
            entry.supervise(entry.session)
            self._limit_warmth()
            self.changed()
            return entry

    async def remount_ui(
        self,
        entry: RetainedSession,
        generation: int,
        current: Callable[[], bool],
        *,
        timeout: float = 2.0,
    ) -> bool:
        """One fresh engine per UI generation; recover output on the SAME lease."""
        session = entry.session
        term, controller, lease, attachment = (
            session.current_term,
            session.controller,
            session.lease,
            session.attachment,
        )
        if (
            entry.ui_recovering
            or entry.ui_remount_generation == generation
            or term is None
            or controller is None
            or lease is None
            or not current()
        ):
            return False
        transition = terminal_transition(session)
        recovery = transition.begin_recovery()
        if recovery is None:
            return False
        entry.ui_recovering = True
        entry.ui_remount_generation = generation
        success = False
        lifecycle = session.interaction_generation

        def owned() -> bool:
            return (
                current()
                and not session.disposed
                and not session.paused
                and session.foreground
                and session.controller is controller
                and session.lease is lease
                and session.attachment is attachment
                and session.interaction_generation == lifecycle
                and not controller.writer_revoked
                and transition.recovery is recovery
            )

        async def remount() -> bool:
            nonlocal lifecycle, success
            if not owned() or transition.control is not None:
                return False
            controller.fence_ui_input(True)
            session._renderer.prepare_ui_remount()
            lifecycle = session.interaction_generation  # our synchronous, known renderer retirement
            controller.emulator = EMULATOR_FRESH
            controller.on_viewer_overflow()  # no renderer bound: reset notice, no stale RESET
            if not term.request_remount():
                return False
            term.retire_transport()
            # Retire the old output reader before resetting its stream baseline.
            task, entry.task = entry.task, None
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            while owned():
                fresh = session.current_term
                if (
                    fresh is not None
                    and fresh is not term
                    and fresh.channel_ready
                    and fresh._native_instance is not None
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                return False
            if not owned():
                return False
            fresh.invalidate_transport(generation)
            fresh.rebind_transport()
            await drive_recovery(
                controller, session._ops.terminal_ticket, fresh_viewer=True, current=owned
            )
            if not owned():
                return False
            self.screens.discard(entry.cache_key)
            session.cached_screen = ""
            trace_lifecycle(
                "ui_remounted", session=id(session), viewer=id(fresh), generation=generation
            )
            success = True
            return True

        try:
            async with asyncio.timeout(timeout):
                return await remount()
        except TimeoutError:
            return False
        except Exception:
            return False  # Fixed recovery state below; no exception/URL logging.
        finally:
            resume = success and owned()
            try:
                if owned() and not success:
                    session.require_ui_recovery()
            finally:
                entry.ui_recovering = False
                transition.end_recovery(recovery)
            if resume:
                entry.supervise(session)
            self.reconcile_ui(entry)
            session._notify()

    def invalidate(self, host_id: str | None = None) -> None:
        """Synchronous input fence precedes asynchronous teardown/revocation."""
        self._cache_revision += 1
        self.screens.clear(host_id)
        for entry in self.entries():
            if host_id is None or entry.host_id == host_id:
                entry.watch_blocked = True
                entry.session.cached_screen = ""
                fence_hidden_writer(entry.session)
                entry.session.set_foreground(False)
        if self.foreground is not None and (host_id is None or self.foreground.host_id == host_id):
            self.release()

    async def clear(self, host_id: str | None = None) -> None:
        self.invalidate(host_id)
        async with self._lock:
            await self.registry.detach_all(host_id)

    async def prune(self, hosts: Mapping[str, str], resources: set[tuple[str, str, str]]) -> None:
        async with self._lock:
            self.screens.prune(hosts, resources)
            for entry in self.entries():
                if (
                    hosts.get(entry.host_id) != entry.epoch
                    or (entry.host_id, entry.kind, entry.key) not in resources
                ):
                    await self.registry.detach(entry.host_id, entry.slot)

    async def evict_idle(self) -> None:
        self.expire_idle_warm()
        async with self._lock:
            for host in {entry.host_id for entry in self.entries()}:
                await self.registry.evict_idle(host, self.clock())

    async def discard(self, entry: RetainedSession) -> None:
        async with self._lock:
            if self.get(entry.host_id, entry.kind, entry.key) is entry:
                await self.registry.detach(entry.host_id, entry.slot)

    async def shutdown(self) -> None:
        self._closed = True
        await self.clear()
