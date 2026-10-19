"""Why a session lost its connection, and whether it may come back without a user action."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.recovery_cause import RECOVERY_TRANSPORT
from relaydeck.lease_diagnostics import opaque, safe_reason

WATCH_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 16.0)
GAP_STABLE_S = 30.0
HOST_OFFLINE_WAIT_S = 60.0
GAP_EXHAUSTED_NOTICE = "Connection keeps dropping. Tap Reconnect."
DRIVE_DEFERRED = "deferred"
DRIVE_EXHAUSTED = "exhausted"
DRIVE_REACQUIRE = "reacquire"


@dataclass
class GapRecovery:
    detail: str = ""
    revision: int = 0
    consumed: int = -1
    attempt: int = 0
    foreground_attempts: int = 0
    visibility_epoch: int | None = None
    stale_retry_at: float | None = None
    stable_since: float | None = None
    gap_at: float = 0.0
    gap_ms: float = 0.0
    host_online: bool = False
    waiting: bool = False
    exhausted: bool = False
    delay: float = 0.0
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def reset(self) -> None:
        self.cancel_wait()
        self.revision += 1
        self.detail = ""
        self.attempt = self.foreground_attempts = 0
        self.stale_retry_at = self.stable_since = None
        self.exhausted = False

    def cancel_wait(self) -> None:
        self.wake.set()

    def renew_foreground(self, epoch: int) -> None:
        if self.visibility_epoch is not None and epoch != self.visibility_epoch:
            self.foreground_attempts = 0
        self.visibility_epoch = epoch

    def rearm(self) -> None:
        """A failed admission keeps its detail: a host still away restarts the host wait, a
        host that came back or a generic gap moves on to the next backoff delay."""
        self.revision += 1
        if self.detail == "host_offline" and not self.host_online:
            self.gap_at, self.gap_ms = time.monotonic(), time.time() * 1000
        elif self.detail != "stale_revision" and self.attempt >= len(WATCH_RETRY_DELAYS_S):
            self.exhausted = True

    def record(self, detail: str) -> None:
        self.detail = detail
        self.revision += 1
        self.gap_at, self.gap_ms = time.monotonic(), time.time() * 1000
        self.stable_since = None
        if detail == "host_offline":
            self.host_online = False
        self.wake.set()

    def output(self) -> None:
        self.detail = ""
        now = time.monotonic()
        if self.stable_since is None:
            self.stable_since = now
        elif now - self.stable_since >= GAP_STABLE_S:
            self.attempt = self.foreground_attempts = 0
            self.stale_retry_at = None
            self.exhausted = False

    def set_host_online(self, online: bool) -> None:
        self.host_online = online
        if online:
            self.wake.set()

    async def wait(self, current: Callable[[], bool]) -> bool:
        if not current() or self.exhausted:
            return False
        if not self.detail:
            return True
        if self.waiting or self.consumed == self.revision:
            return False
        revision, detail = self.revision, self.detail
        now = time.monotonic()
        host_wait = detail == "host_offline" and not self.host_online
        if detail == "stale_revision":
            if self.stale_retry_at is not None and now - self.stale_retry_at < 10:
                self.exhausted = True
                return False
            self.delay = 0.0
        elif host_wait:
            self.delay = max(0.0, self.gap_at + HOST_OFFLINE_WAIT_S - now)
        elif self.attempt < len(WATCH_RETRY_DELAYS_S):
            self.delay = WATCH_RETRY_DELAYS_S[self.attempt]
        else:
            self.exhausted = True
            return False
        deadline = now + self.delay
        self.waiting = True
        try:
            while current() and revision == self.revision:
                self.wake.clear()
                if host_wait and self.host_online:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.wake.wait(), remaining)
                except TimeoutError:
                    break
            if not current() or revision != self.revision:
                return False
            if detail == "stale_revision":
                self.stale_retry_at = time.monotonic()
            elif not host_wait:
                self.attempt += 1
            self.consumed = revision
            return True
        finally:
            self.waiting = False


class RecoveryState:
    """Recovery bookkeeping shared by agent and shell sessions.

    Kept apart from the phase: the phase is what the screen shows, the cause is what an
    automatic reconnect may act on. Deliberate stops bump `stop_epoch`, so an attempt that
    captured an older value publishes nothing.
    """

    _phase: str
    _PHASE_EXITED: str
    _PHASE_CONFLICT: str
    _PHASE_DISCONNECTED: str
    _controller: Any
    _lease: Any

    def _init_recovery(self) -> None:
        self.gap_recovery = GapRecovery()
        self._gap_retry: asyncio.Task[str] | None = None
        self._gap_admitted: asyncio.Task[Any] | None = None
        self._gap_admitted_revision = -1
        self._gap_retry_action: Callable[[], Awaitable[str]] | None = None
        self._gap_retry_current: Callable[[], bool] = lambda: True
        self._recovery_cause: str | None = None
        self._recovering = False
        self._resume_mode = "view"
        self._stop_epoch = 0
        self._recovery_guard: Callable[[], bool] | None = None
        self._explicit_reconnects = 0
        self._automatic = False

    def log_reopen(self, site: str) -> None:
        session: Any = self
        diagnostics_logger().info(
            "reopen site=%s term=%s gap=%s attempt=%s",
            site
            if site
            in {
                "auto_take",
                "recover_after_reload",
                "open",
                "take_over",
                "reconnect",
                "fg_reacquire",
            }
            else "other",
            opaque(
                "terminal",
                session.host_id,
                session._terminal_label() or getattr(session, "_pending_target", ""),
            ),
            safe_reason(self.gap_recovery.detail) if self.gap_recovery.detail else "none",
            self.gap_recovery.attempt,
        )

    def _gap_exhausted(self) -> str:
        session: Any = self
        if session._notice != GAP_EXHAUSTED_NOTICE:
            session._notice = GAP_EXHAUSTED_NOTICE
            session._notify()
        return DRIVE_EXHAUSTED

    def reset_gap_recovery(self) -> None:
        self._cancel_gap_retry()
        self.gap_recovery.reset()

    def _cancel_gap_retry(self) -> None:
        task = self._gap_retry
        try:
            running = asyncio.current_task()
        except RuntimeError:
            running = None
        if task is not None and task is not running and not task.done():
            task.cancel()
        self.gap_recovery.cancel_wait()

    def defer_gap_recovery(
        self, retry: Callable[[], Awaitable[str]], *, current: Callable[[], bool] = lambda: True
    ) -> str | None:
        session: Any = self
        budget = self.gap_recovery
        if (
            self._gap_admitted is asyncio.current_task()
            and self._gap_admitted_revision == budget.revision
        ):
            return None
        if budget.exhausted:
            return self._gap_exhausted()
        if not budget.detail:
            return None
        previous = self._gap_retry
        if previous is not None and not previous.done() and not previous.cancelling():
            if self._gap_retry_current():
                return DRIVE_DEFERRED
            previous.cancel()
        if budget.consumed == budget.revision:
            self._settle_gap_retry()
            if budget.exhausted:
                return self._gap_exhausted()
            if budget.consumed == budget.revision:
                return DRIVE_DEFERRED
        identity = (session.controller, session.attachment, session.lease)
        generation, stop = session.interaction_generation, self.stop_epoch
        detail = safe_reason(budget.detail)
        terminal = opaque("terminal", session.host_id, session._terminal_label())
        self._gap_retry_action, self._gap_retry_current = retry, current

        def valid() -> bool:
            return bool(
                current()
                and session.foreground
                and not session.paused
                and not session.disposed
                and session._operation_current(generation)
                and not getattr(session, "_auth_revoked", False)
                and self.stop_epoch == stop
                and session.phase not in (self._PHASE_EXITED, self._PHASE_CONFLICT)
                and all(
                    a is b
                    for a, b in zip(
                        identity,
                        (session.controller, session.attachment, session.lease),
                        strict=True,
                    )
                )
                and (
                    session.controller is None
                    or not session.controller.writer_revoked
                    or session.controller.revoke_reason in ("", "disconnected", "viewer_reopened")
                )
            )

        async def run() -> str:
            started = time.monotonic()
            outcome, waited_ms = "invalid", 0
            try:
                if previous is not None and not previous.done():
                    await asyncio.gather(previous, return_exceptions=True)
                if not await budget.wait(valid):
                    outcome = "deferred" if valid() else "invalid"
                    if budget.exhausted:
                        outcome = "exhausted"
                        return self._gap_exhausted()
                    return DRIVE_DEFERRED
                if not valid():
                    return DRIVE_DEFERRED
                outcome = "admitted"
                waited_ms = round((time.monotonic() - started) * 1000)
                self._gap_admitted = asyncio.current_task()
                admitted = self._gap_admitted_revision = budget.revision
                try:
                    return await retry()
                finally:
                    if (
                        budget.revision == admitted
                        and current()
                        and session._operation_current(generation)
                        and self.stop_epoch == stop
                        and session.foreground
                        and not session.paused
                        and not session.disposed
                        and not getattr(session, "_auth_revoked", False)
                    ):
                        self._settle_gap_retry()
            finally:
                diagnostics_logger().info(
                    "gap_retry outcome=%s gap=%s waited_ms=%s term=%s",
                    outcome,
                    detail,
                    waited_ms
                    if outcome == "admitted"
                    else round((time.monotonic() - started) * 1000),
                    terminal,
                )
                if self._gap_admitted is asyncio.current_task():
                    self._gap_admitted = None
                if self._gap_retry is asyncio.current_task():
                    self._gap_retry = None
                if budget.exhausted:
                    self._gap_retry_action = None
                session._notify()

        self._gap_retry = asyncio.create_task(run())
        self._gap_retry.add_done_callback(
            lambda task: None if task.cancelled() else task.exception()
        )
        return DRIVE_DEFERRED

    def _settle_gap_retry(self) -> None:
        """A failed admission or pre-output stream death must not strand recovery."""
        if self._gap_admitted is not None and self._gap_admitted is not asyncio.current_task():
            return  # The admitted operation may be closing its previous reader.
        session: Any = self
        budget = self.gap_recovery
        if (
            self._phase == self._PHASE_DISCONNECTED
            and budget.detail
            and budget.consumed == budget.revision
            and session._recoverable_now()
        ):
            budget.rearm()
            session._note_resume_mode()
            self._recovery_cause = RECOVERY_TRANSPORT
            if budget.exhausted:
                self._gap_exhausted()

    def _resume_gap_retry(self) -> None:
        if (
            self._gap_retry_action is not None
            and self.gap_recovery.detail
            and self.recovery_cause is not None
            and self._gap_retry_current()
        ):
            self.defer_gap_recovery(self._gap_retry_action, current=self._gap_retry_current)

    def _log_generation(self, site: str) -> None:
        session: Any = self
        diagnostics_logger().info(
            "generation_bump site=%s term=%s",
            site
            if site in {"disconnect", "suspend", "foreground", "unbind_view", "viewer_reopened"}
            else "other",
            opaque(
                "terminal",
                session.host_id,
                session._terminal_label() or getattr(session, "_pending_target", ""),
            ),
        )

    def set_host_online(self, online: bool) -> None:
        self.gap_recovery.set_host_online(online)

    def _note_recovery_output(self) -> None:
        if not self.gap_recovery.detail or (
            self._controller and self._controller.mode == "control"
        ):
            self._recovery_cause = None

    async def _recover_after_reload(self) -> str:
        """Recover a replacement viewer under the verified identity and takeover preference."""
        session: Any = self
        self.log_reopen("recover_after_reload")
        if self._controller is not None and self._controller.live_edge_pending:
            return str(await session.drive_recovery())
        if session.disposed or session.paused or self._phase != self._PHASE_DISCONNECTED:
            return self._phase
        if not (session._target or session._pending_target):
            return self._phase
        automatic = bool(session.auto_take_allowed)
        if automatic and (outcome := self.defer_gap_recovery(self._recover_after_reload)):
            return outcome
        await session.reconnect(automatic=True)
        if automatic and self._phase == self._PHASE_DISCONNECTED:
            return str(await session.auto_take_control())
        return self._phase

    async def _recover_gap_lease(self) -> str:
        session: Any = self
        if not self.gap_recovery.detail:
            session._on_renewal_terminal("lease_expired")
            self._phase = self._PHASE_DISCONNECTED
            return self._phase
        previous = (self._gap_admitted, self._gap_admitted_revision)
        self._gap_admitted, self._gap_admitted_revision = (
            asyncio.current_task(),
            self.gap_recovery.revision,
        )
        try:
            await session.reconnect(automatic=True)
            if getattr(session, "auto_take_allowed", False):
                return str(await session.auto_take_control())
            return str(session.phase)
        finally:
            self._gap_admitted, self._gap_admitted_revision = previous

    @property
    def recovery_cause(self) -> str | None:
        """Lifecycle or transport loss that may be recovered automatically; None otherwise."""
        return self._recovery_cause

    @property
    def stop_epoch(self) -> int:
        return self._stop_epoch

    def set_recovery_guard(self, guard: Callable[[], bool] | None) -> None:
        self._recovery_guard = guard

    @property
    def resume_mode(self) -> str:
        """The lease mode held before the connection was lost (control or view)."""
        return self._resume_mode

    @property
    def explicit_reconnects(self) -> int:
        return self._explicit_reconnects

    @property
    def recovering(self) -> bool:
        return self._recovering

    def set_recovering(self, active: bool) -> None:
        if self._recovering != active:
            self._recovering = active
            self._notify()  # type: ignore[attr-defined]

    def _note_resume_mode(self) -> None:
        # The lease is still present here; afterwards the stream state reads "none".
        if self._lease is not None:
            self._resume_mode = self._lease.mode

    def _recoverable_now(self) -> bool:
        """Only a connection lost to lifecycle or transport may come back without a user action."""
        if self._phase in (self._PHASE_EXITED, self._PHASE_CONFLICT):
            return False
        if self._phase == self._PHASE_DISCONNECTED and self._recovery_cause is None:
            return False
        controller = self._controller
        if controller is None or not controller.writer_revoked:
            return True
        # Local hiding and viewer replacement revoke the writer too; only a lease failure is a stop.
        return bool(controller.revoke_reason in ("", "disconnected", "viewer_reopened"))
