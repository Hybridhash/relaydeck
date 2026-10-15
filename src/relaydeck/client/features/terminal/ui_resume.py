"""Authenticated, lifecycle-owned native terminal transport recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from relaydeck_terminal.terminal import ResumeOutcome

from relaydeck.client import open_timing
from relaydeck.client.features.terminal.renewal import lease_deadline_monotonic

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.agent_session import AgentSession
    from relaydeck.client.features.terminal.controller import TerminalController

_diag = logging.getLogger("relaydeck.client.typing_split")


class TerminalTransition:
    """Takeover intent excludes new recovery; an existing recovery settles first."""

    def __init__(self) -> None:
        self.control: object | None = None
        self._claims = 0
        self._fence: tuple[TerminalController, bool] | None = None
        self.recovery: asyncio.Future[None] | None = None

    def begin(self, session: AgentSession) -> object:
        if self.control is None and session.controller is not None:
            self._fence = (session.controller, session.controller.ui_input_fenced)
        token = self.claim()
        if session.controller is not None:
            session.controller.fence_ui_input(True)
        session._notify()
        return token

    def end(self, session: AgentSession, token: object) -> None:
        settled = self.release(token)
        if settled:
            previous, self._fence = self._fence, None
            if session.controller is not None and session.phase == "live":
                session.controller.fence_ui_input(
                    previous[1]
                    if previous is not None and previous[0] is session.controller
                    else False
                )
        session._notify()

    async def automatic(self, session: AgentSession) -> str:
        token = self.begin(session)
        try:
            if not await self.settle(token):
                return session.phase
            return await session._auto_take_control(token)
        finally:
            self.end(session, token)
            session.expect_control(False)

    async def confirm(
        self,
        session: AgentSession,
        *,
        expected_control_revision: str | None = None,
    ) -> str:
        """Join one disclosure-authorized takeover; callers cannot cancel each other."""
        flight = session._takeover_flight
        if flight is None or flight.done():
            if not session._disclosure_armed and session.phase == "live":
                return session.phase  # A late tap must not repeat the completed takeover.
            timing = open_timing.of(session)
            if timing is not None:
                timing.mark("confirm")
                open_timing.bind(timing)  # the flight's gateway calls and stream report to it
            token = self.begin(session)

            async def take() -> str:
                try:
                    if not await self.settle(token):
                        return session.phase
                    return await session._confirm_takeover(
                        token, expected_control_revision=expected_control_revision
                    )
                finally:
                    self.end(session, token)

            flight = asyncio.ensure_future(take())
            session._takeover_flight = flight

            def settled(done: asyncio.Future[str]) -> None:
                if session._takeover_flight is done:
                    session._takeover_flight = None
                    session._notify()
                if not done.cancelled():
                    done.exception()

            flight.add_done_callback(settled)
            session._notify()
        # Only explicit operation or lifecycle cancellation retires the shared intent.
        return await asyncio.shield(flight)

    def claim(self) -> object:
        if self.control is None:
            self.control = object()
        self._claims += 1
        return self.control

    def release(self, token: object) -> bool:
        if self.control is token:
            self._claims -= 1
            if not self._claims:
                self.control = None
                return True
        return False

    def invalidate(self, token: object | None = None) -> None:
        if token is None or self.control is token:
            self.control = None
            self._claims = 0

    async def settle(self, token: object) -> bool:
        pending = self.recovery
        if pending is not None:
            await asyncio.shield(pending)
        return self.control is token

    def begin_recovery(self) -> asyncio.Future[None] | None:
        if self.control is not None or self.recovery is not None:
            return None
        self.recovery = asyncio.get_running_loop().create_future()
        return self.recovery

    def end_recovery(self, token: asyncio.Future[None]) -> None:
        if self.recovery is token:
            self.recovery = None
            token.set_result(None)


def terminal_transition(session: Any) -> TerminalTransition:
    transition = getattr(session, "_ui_transition", None)
    if transition is None:
        transition = session._ui_transition = TerminalTransition()
    return transition


@dataclass(eq=False)
class _Attempt:
    session: Any
    term: Any
    controller: Any
    attachment: Any
    lease: Any
    transport: Any
    lifecycle: int
    viewer: int
    resets: int
    native: int | None
    generation: int
    stream_epoch: str
    readiness: tuple[str, bool, bool]
    current: Callable[[], bool]
    ordinal: int = 0
    task: asyncio.Task[ResumeOutcome | None] | None = None
    unsubscribe: Callable[[], None] | None = None
    stopped: bool = False
    lease_failed: bool = False


class TerminalUIResumes:
    """One operation per terminal; stale results have no recovery authority."""

    def __init__(self) -> None:
        self._active: dict[int, _Attempt] = {}
        self._seen: dict[int, _Attempt] = {}
        self._tasks: set[asyncio.Task[ResumeOutcome | None]] = set()
        self._closed = False
        self._serial = 0

    def _live(self, attempt: _Attempt) -> bool:
        session, term = attempt.session, attempt.term
        try:
            authorized = attempt.current()
        except Exception:
            return False
        return bool(
            not self._closed
            and not attempt.stopped
            and self._active.get(id(term)) is attempt
            and authorized
            and not session.disposed
            and session.foreground
            and not session.paused
            and terminal_transition(session).control is None
            and session.current_term is term
            and session.controller is attempt.controller
            and session.attachment is attempt.attachment
            and session.lease is attempt.lease
            and session.interaction_generation == attempt.lifecycle
            and term.viewer_generation == attempt.viewer
            and term._reset_count == attempt.resets
            and (attempt.native is None or term._native_instance == attempt.native)
            and attempt.controller.generation == attempt.generation
            and attempt.controller.state.epoch == attempt.stream_epoch
            and session.term_state.epoch == attempt.stream_epoch
        )

    def _dead_reason(self, attempt: _Attempt) -> str:
        """Name of the first liveness check that failed (content-free diagnostics)."""
        session, term = attempt.session, attempt.term
        try:
            authorized = attempt.current()
        except Exception:
            authorized = False
        checks = (
            ("closed", not self._closed),
            ("stopped", not attempt.stopped),
            ("superseded", self._active.get(id(term)) is attempt),
            ("unauthorized", authorized),
            ("disposed", not session.disposed),
            ("background", session.foreground),
            ("paused", not session.paused),
            ("transition", terminal_transition(session).control is None),
            ("term_changed", session.current_term is term),
            ("controller_changed", session.controller is attempt.controller),
            ("attachment_changed", session.attachment is attempt.attachment),
            ("lease_changed", session.lease is attempt.lease),
            ("lifecycle_changed", session.interaction_generation == attempt.lifecycle),
            ("viewer_changed", term.viewer_generation == attempt.viewer),
            ("reset", term._reset_count == attempt.resets),
            ("native_changed", attempt.native is None or term._native_instance == attempt.native),
            ("generation_changed", attempt.controller.generation == attempt.generation),
            ("stream_epoch_changed", attempt.controller.state.epoch == attempt.stream_epoch),
            ("view_epoch_changed", session.term_state.epoch == attempt.stream_epoch),
        )
        return next((name for name, ok in checks if not ok), "none")

    @staticmethod
    def _lease_live(attempt: _Attempt) -> bool:
        lease, controller = attempt.lease, attempt.controller
        if (
            lease.lease_id != controller.lease_id
            or lease.generation != controller.generation
            or lease.mode != controller.mode
            or controller.writer_revoked
        ):
            return False
        driver = attempt.session._renewal
        if driver is not None and driver.lease_id == lease.lease_id:
            deadline = driver.deadline
            if driver.invalidated:
                return False
        else:
            deadline = lease_deadline_monotonic(lease.expires_at)
        return deadline is not None and deadline > time.monotonic()

    def _schedule_reload(self, attempt: _Attempt) -> None:
        """Resume a replaced native viewer through identity-verified recovery."""
        recover = getattr(attempt.session, "recover_after_reload", None)
        if not callable(recover) or self._closed:
            return

        async def reload() -> None:
            try:
                authorized = attempt.current()
            except Exception:
                return
            if (
                authorized
                and not attempt.session.disposed
                and terminal_transition(attempt.session).control is None
                and attempt.session.lease is attempt.lease
            ):
                await recover()

        task = asyncio.create_task(reload())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cancel(self, attempt: _Attempt) -> None:
        if (
            self._active.get(id(attempt.term)) is attempt
            and attempt.session.controller is attempt.controller
        ):
            attempt.controller.fence_ui_input(True)
        if not attempt.stopped:
            _diag.info("ui_resume_cancel why=%s", self._dead_reason(attempt))
            timing = open_timing.of(attempt.session)
            if timing is not None:
                timing.count("probe_cancels")
            attempt.session._lease_trace(attempt.lease).emit(
                "ui_resume",
                reason="cancelled",
                attempt=attempt.ordinal,
                source="ui_transport",
                viewer=attempt.viewer,
                ui=attempt.transport,
            )
        attempt.stopped = True
        if attempt.unsubscribe is not None:
            attempt.unsubscribe()
            attempt.unsubscribe = None
        if attempt.task is not None and attempt.task is not asyncio.current_task():
            attempt.task.cancel()
        if self._active.get(id(attempt.term)) is attempt:
            del self._active[id(attempt.term)]

    def invalidate(self) -> None:
        """A replaced authenticated transport invalidates every attempt, not its leases."""
        for attempt in tuple(self._active.values()):
            self._cancel(attempt)
        self._seen.clear()

    def defer(self, session: Any) -> None:
        for attempt in tuple(self._active.values()):
            if attempt.session is session:
                self._cancel(attempt)

    def start(
        self,
        session: Any,
        *,
        transport: Any,
        current: Callable[[], bool],
        closed: asyncio.Event,
        timeout: float = 5.0,
        generation: int | None = None,
        force: bool = False,
        recover: Callable[[str], Awaitable[bool]] | None = None,
    ) -> asyncio.Task[ResumeOutcome | None] | None:
        term, controller, attachment, lease = (
            session.current_term,
            session.controller,
            session.attachment,
            session.lease,
        )
        try:
            authorized = current()
        except Exception:
            return None
        if (
            self._closed
            or not authorized
            or closed.is_set()
            or session.disposed
            or not session.foreground
            or session.paused
            or terminal_transition(session).control is not None
            or term is None
            or controller is None
            or attachment is None
            or lease is None
            or term._channel_id is None
        ):
            return None
        for key, seen in tuple(self._seen.items()):
            if key not in self._active and (
                seen.session.disposed or seen.session.current_term is not seen.term
            ):
                del self._seen[key]
        previous = self._seen.get(id(term))
        if (
            previous is not None
            and not force
            and not previous.stopped
            and previous.transport is transport
            and previous.controller is controller
            and previous.attachment is attachment
            and previous.lease is lease
            and previous.lifecycle == session.interaction_generation
            and previous.viewer == term.viewer_generation
            and previous.resets == term._reset_count
            and previous.native == term._native_instance
            and previous.generation == controller.generation
            and previous.stream_epoch == controller.state.epoch
            and previous.readiness
            == (
                controller.state.status,
                controller.reattach_needed,
                controller.pending_resume is not None,
            )
        ):
            return previous.task  # Repeated connect callbacks cannot probe twice.
        active = self._active.get(id(term))
        if active is not None:
            self._cancel(active)
        attempt = _Attempt(
            session,
            term,
            controller,
            attachment,
            lease,
            transport,
            session.interaction_generation,
            term.viewer_generation,
            term._reset_count,
            term._native_instance,
            controller.generation,
            controller.state.epoch,
            (
                controller.state.status,
                controller.reattach_needed,
                controller.pending_resume is not None,
            ),
            current,
        )
        self._serial += 1
        attempt.ordinal = self._serial
        # Bind before scheduling so early cancellation cannot strand output on a dead channel.
        try:
            term.rebind_transport()
        except Exception:  # noqa: BLE001 - best effort; the attempt re-resolves
            pass
        # Publish/fence before scheduling: no queued editor or native send may race the probe.
        self._active[id(term)] = self._seen[id(term)] = attempt
        controller.fence_ui_input(True)
        timing = open_timing.of(session)
        if timing is not None:
            timing.mark("probe_start")
        session._lease_trace(lease).emit(
            "ui_resume",
            reason="started",
            attempt=attempt.ordinal,
            source="ui_transport",
            viewer=attempt.viewer,
            ui=transport,
        )

        def changed(_: Any) -> None:
            if not self._live(attempt):
                # Only a change while the session was still live is a reload; the
                # attempt's own viewer-reopen recovery already disconnected it.
                reloaded = (
                    self._dead_reason(attempt) == "lifecycle_changed"
                    and getattr(attempt.session, "phase", "") == "live"
                )
                self._cancel(attempt)
                if reloaded:
                    self._schedule_reload(attempt)

        attempt.unsubscribe = session.subscribe(changed)

        def verified() -> bool:
            if not self._live(attempt):
                return False
            if not self._lease_live(attempt):
                attempt.lease_failed = True
                return False
            # Existing overflow/reader recovery owns its state, even if native continuity succeeds.
            usable = (
                controller.state.status == "live"
                and not controller.reattach_needed
                and controller.pending_resume is None
            )
            term.set_input_enabled(
                bool(
                    term.input_enabled
                    and usable
                    and controller.writer_eligible
                    and session.input_token is not None
                    and not session.term_state.selection.open
                )
            )
            return True

        async def resume() -> ResumeOutcome | None:
            probe: asyncio.Task[ResumeOutcome] | None = None
            shutdown: asyncio.Task[bool] | None = None
            try:
                if not self._live(attempt):
                    return None
                if not self._lease_live(attempt):
                    session.require_ui_recovery()
                    return ResumeOutcome("incomplete", "lease_expired")
                probe = asyncio.create_task(term.rebind_resumed(timeout=timeout, current=verified))
                shutdown = asyncio.create_task(closed.wait())
                await asyncio.wait((probe, shutdown), return_when=asyncio.FIRST_COMPLETED)
                if closed.is_set() or not self._live(attempt):
                    self._cancel(attempt)
                    return None
                outcome = probe.result()
                session._lease_trace(lease).emit(
                    "ui_resume",
                    reason=outcome.status,
                    attempt=attempt.ordinal,
                    source="ui_transport",
                    viewer=attempt.viewer,
                    ui=transport,
                )
                if outcome.status == "resumed":
                    if not self._lease_live(attempt):
                        term.set_input_enabled(False)
                        session.require_ui_recovery()
                        return ResumeOutcome("incomplete", "lease_expired")
                    if outcome.reason == "ok_after_overflow":
                        controller.fence_ui_input(True)
                        term.set_input_enabled(False)
                        if not controller.reattach_needed and controller.pending_resume is None:
                            controller.on_viewer_overflow()
                    else:
                        # Only a completed current probe may remove the Python send fence.
                        controller.fence_ui_input(
                            controller.state.status != "live"
                            or controller.reattach_needed
                            or controller.pending_resume is not None
                        )
                        if generation is not None:
                            term.verify_transport(generation)
                        timing = open_timing.of(session)
                        if timing is not None:
                            timing.mark("verified")
                            if not controller.ui_input_fenced:
                                timing.mark("input_ready")
                        attempt.native = term._native_instance
                        _diag.info(
                            "ui_resume_verified ui=%s viewer=%s resets=%s post_frame_reports=%s "
                            "cols=%s rows=%s",
                            generation,
                            term.viewer_generation,
                            term._reset_count,
                            term._view_report_count,
                            *term._last_view_size,
                        )
                elif outcome.status == "incomplete":
                    if outcome.reason != "superseded" or attempt.lease_failed:
                        controller.fence_ui_input(True)
                        term.set_input_enabled(False)
                        if recover is None or attempt.lease_failed:
                            session.require_ui_recovery()
                        else:
                            # Retire the probe before the deliberate remount changes
                            # renderer/lifecycle identity. It has no successor authority.
                            self._cancel(attempt)
                            await recover(outcome.reason)
                elif outcome.status == "noop":
                    verified()  # Never attached: validate before removing our Python-only fence.
                # noop/never_attached and superseded never reset or acquire a viewer.
                if outcome.status == "resumed" and not attempt.stopped:
                    session._notify()
                return outcome
            except asyncio.CancelledError:
                raise
            except Exception:
                _diag.warning("ui_resume_failed", exc_info=True)
                if self._live(attempt):
                    controller.fence_ui_input(True)
                    term.set_input_enabled(False)
                    session.require_ui_recovery()
                return ResumeOutcome("incomplete", "resume_failed")
            finally:
                for child in (probe, shutdown):
                    if child is not None:
                        child.cancel()
                await asyncio.gather(
                    *(t for t in (probe, shutdown) if t is not None), return_exceptions=True
                )
                if attempt.unsubscribe is not None:
                    attempt.unsubscribe()
                    attempt.unsubscribe = None
                if self._active.get(id(term)) is attempt:
                    del self._active[id(term)]
                if attempt.stopped and self._seen.get(id(term)) is attempt:
                    del self._seen[id(term)]

        attempt.task = asyncio.create_task(resume())
        self._tasks.add(attempt.task)

        def finished(task: asyncio.Task[ResumeOutcome | None]) -> None:
            self._tasks.discard(task)
            if not task.cancelled():
                task.exception()

        attempt.task.add_done_callback(finished)
        return attempt.task

    async def close(self) -> None:
        self._closed = True
        for attempt in tuple(self._active.values()):
            self._cancel(attempt)
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._seen.clear()
