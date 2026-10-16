"""Why a session lost its connection, and whether it may come back without a user action."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


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
        self._recovery_cause: str | None = None
        self._recovering = False
        self._resume_mode = "view"
        self._stop_epoch = 0
        self._recovery_guard: Callable[[], bool] | None = None
        self._explicit_reconnects = 0
        self._automatic = False

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
