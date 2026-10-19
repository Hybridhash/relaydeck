"""Reconnect the visible terminal when the page returns, without a user action.

One flight per entry. It only revives a terminal that was disconnected by the page lifecycle or
the transport: a deliberate release, an exit, a revoked grant, a conflict or an exhausted budget
stays as it is. Control is never forced; the existing conditional paths decide ownership. Every
await of the reacquire runs under one validity guard, so a stale attempt publishes nothing and
gives back whatever it acquired.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.recovery_cause import (
    RECOVERY_LIFECYCLE,
    RECOVERY_TRANSPORT,
)
from relaydeck.client.features.terminal.ui_resume import terminal_transition

MAX_ATTEMPTS = 2
_RECOVERABLE = frozenset({RECOVERY_LIFECYCLE, RECOVERY_TRANSPORT})


@dataclass(eq=False)
class _State:
    stamp: tuple[int, int] = (-1, -1)
    explicit: int = 0
    flight: asyncio.Task[str] | None = None
    exhausted_logged: bool = False
    pending_verify: bool = False


class ForegroundRecovery:
    def __init__(
        self,
        *,
        retained: Any,
        current: Callable[[Any], bool],
        stamp: Callable[[], tuple[int, int]],
        verify: Callable[[Any], None],
    ) -> None:
        self._retained = retained
        self._current = current
        self._stamp = stamp
        self._verify = verify
        self._states: weakref.WeakKeyDictionary[Any, _State] = weakref.WeakKeyDictionary()
        self._closed = False

    def _renew(self, entry: Any, state: _State) -> tuple[int, int]:
        """Only a new visibility epoch refills the foreground allowance."""
        stamp = self._stamp()
        entry.session.gap_recovery.renew_foreground(stamp[0])
        explicit = entry.session.explicit_reconnects
        if state.stamp != stamp or state.explicit != explicit:
            state.stamp, state.explicit = stamp, explicit
            state.exhausted_logged = False
        return stamp

    def request(self, entry: Any, reason: str) -> asyncio.Task[str] | None:
        """Start or join the recovery of one entry."""
        if self._closed:
            return None
        state = self._states.setdefault(entry, _State())
        self._renew(entry, state)
        if state.flight is not None and not state.flight.done():
            return state.flight
        pending = entry.warm_task
        settling = pending is not None and not pending.done()
        if not settling and not (self._wanted(entry) and self._current(entry)):
            return None
        budget = entry.session.gap_recovery
        if budget.exhausted or (not budget.detail and budget.foreground_attempts >= MAX_ATTEMPTS):
            entry.session._gap_exhausted()
            if not state.exhausted_logged:
                state.exhausted_logged = True
                self._emit(entry, reason, entry.session.phase, "exhausted")
            return None
        task = asyncio.create_task(self._run(entry, reason, state))
        state.flight = task
        return task

    def note(self, entry: Any) -> None:
        """Every session notification: a recoverable loss starts the bounded owner."""
        if self._closed:
            return
        state = self._states.get(entry)
        session = entry.session
        if state is not None and state.pending_verify:
            term = session.current_term
            if (
                term is not None
                and session.phase != "disconnected"
                and getattr(term, "transport_verified", False)
            ):
                state.pending_verify = False
                self._emit(entry, "verification", session.phase, "verified")
        if session.recovering or not self._wanted(entry):
            return
        self.request(entry, "loss")

    def close(self) -> None:
        self._closed = True
        for state in tuple(self._states.values()):
            if state.flight is not None and not state.flight.done():
                state.flight.cancel()

    def _emit(self, entry: Any, reason: str, phase: str, outcome: str) -> None:
        diagnostics_logger().info(
            "fg_recovery reason=%s phase=%s foreground=%s entry=%s transport=%s outcome=%s",
            reason
            if reason in {"visible", "reconnect", "resume", "show", "loss", "verification"}
            else "other",
            phase
            if phase in {"live", "disconnected", "conflict", "exited", "connecting", "idle"}
            else "other",
            self._retained.device_foreground,
            entry.serial,
            self._stamp()[1],
            outcome
            if outcome
            in {
                "exhausted",
                "verified",
                "skipped",
                "superseded",
                "busy",
                "connected",
                "failed",
                "refused",
                "cancelled",
                "deferred",
            }
            else "other",
        )

    def _wanted(self, entry: Any) -> bool:
        session = entry.session
        return bool(
            not entry.stopped
            and not entry.watch_blocked
            and not session.disposed
            and session.recovery_cause in _RECOVERABLE
            and session.phase == "disconnected"
        )

    def _valid(self, entry: Any, stamp: tuple[int, int], stop: int) -> bool:
        """The captured guard: stop intent, session identity, entry identity, transport epoch."""
        session = entry.session
        return bool(
            not self._closed
            and self._stamp() == stamp
            and self._retained.foreground is entry
            and self._retained.get(entry.host_id, entry.kind, entry.key) is entry
            and not entry.stopped
            and not entry.watch_blocked
            and not session.disposed
            and session.stop_epoch == stop
            and session.phase != "exited"
            and self._current(entry)
        )

    def _eligible(self, entry: Any, stamp: tuple[int, int], stop: int) -> bool:
        session = entry.session
        return bool(
            self._valid(entry, stamp, stop)
            and not session.paused
            and session.foreground
            and terminal_transition(session).control is None
            and self._wanted(entry)
        )

    async def _run(self, entry: Any, reason: str, state: _State) -> str:
        session = entry.session
        outcome = "skipped"
        phase = session.phase
        try:
            for _ in range(MAX_ATTEMPTS + 1):
                pending = entry.warm_task
                if pending is not None and pending is not asyncio.current_task():
                    await asyncio.gather(pending, return_exceptions=True)
                stamp = self._renew(entry, state)
                stop = session.stop_epoch
                if not self._eligible(entry, stamp, stop):
                    if outcome != "superseded":
                        outcome = "skipped"
                    break
                if session.controller is not None and session.controller.live_edge_pending:
                    outcome = await self._reacquire(entry, stamp, stop)
                    break  # Overflow has its own budget; never spend a transport-gap attempt.
                deferred = session.defer_gap_recovery(
                    lambda: self._run(entry, reason, state),
                    current=lambda stamp=stamp, stop=stop: self._valid(entry, stamp, stop),
                )
                if deferred is not None:
                    outcome = str(deferred)
                    return outcome
                budget = session.gap_recovery
                if not budget.detail and budget.foreground_attempts >= MAX_ATTEMPTS:
                    session._gap_exhausted()
                    outcome = "exhausted"
                    break
                budget.foreground_attempts += 1
                outcome = await self._reacquire(entry, stamp, stop)
                if outcome == "connected":
                    state.pending_verify = True
                if outcome == "busy":
                    budget.foreground_attempts -= 1
                if outcome not in {"superseded", "busy"}:
                    break
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "failed"
        finally:
            session.set_recovering(False)
            self._emit(entry, reason, phase, outcome)
        return outcome

    async def _reacquire(self, entry: Any, stamp: tuple[int, int], stop: int) -> str:
        session = entry.session
        session.log_reopen("fg_reacquire")
        if session.controller is not None and session.controller.live_edge_pending:
            success = await self._retained.resume_output(
                entry, lambda: self._valid(entry, stamp, stop)
            )
            return "connected" if success else "refused"
        transition = terminal_transition(session)
        settling = transition.recovery
        if settling is not None:
            await asyncio.shield(settling)
            if not self._eligible(entry, stamp, stop):
                return "skipped"
        recovery = transition.begin_recovery()
        if recovery is None:
            return "busy"

        def guard() -> bool:
            return self._valid(entry, stamp, stop)

        session.set_recovering(True)
        session.set_recovery_guard(guard)
        control = session.resume_mode == "control"
        conditional_control = bool(
            entry.kind == "agent"
            and control
            and session.gap_recovery.detail
            and getattr(session, "auto_take_allowed", False)
        )
        try:
            if (entry.kind == "shell" and control) or conditional_control:
                # Non-forced attach; a same-device conflict retakes only the observed holder
                # revision, another or changed holder lands in the conflict panel.
                await session.reconnect(automatic=True)
            else:
                await self._retained.resume_watch(entry)
        finally:
            transition.end_recovery(recovery)
            session.set_recovery_guard(None)
        if not guard():
            await self._abandon(entry, stop)
            return "superseded"
        if session.phase == "disconnected" and not conditional_control:
            return "refused"
        if entry.kind == "agent" and control and getattr(session, "auto_take_allowed", False):
            session.set_recovery_guard(guard)
            try:
                # The existing conditional path: it refuses another holder and checks the revision.
                await session.auto_take_control()
            finally:
                session.set_recovery_guard(None)
            if not guard():
                await self._abandon(entry, stop)
                return "superseded"
        self._verify(entry)
        return "connected"

    async def _abandon(self, entry: Any, stop: int) -> None:
        """Give back what a stale attempt published; a deliberate stop already did its own."""
        session = entry.session
        if session.disposed or session.stop_epoch != stop:
            return
        if session.controller is not None or session.lease is not None:
            await session.disconnect(keep_recovery=True)
