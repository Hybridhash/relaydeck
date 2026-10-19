"""Shared identity, lifecycle and input generations for terminal sessions.

Teardown fences input before awaiting; workflow hooks keep composer routing
agent-only. Collaborators receive explicit callbacks, not the session."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from relaydeck.client import open_timing
from relaydeck.client.features.composer.domain import ComposerStore
from relaydeck.client.features.shell.ops import (
    AttachmentInfo,
    LeaseInfo,
    ShellGatewayOps,
    ShellOpError,
)
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.controller import TerminalController
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.output_log import BoundedOutputLog
from relaydeck.client.features.terminal.recovery_cause import (
    RECOVERY_LIFECYCLE,
    RECOVERY_TRANSPORT,
)
from relaydeck.client.features.terminal.session.facade import SessionFacade
from relaydeck.client.features.terminal.session.hooks import (
    key_hooks,
    lease_hooks,
    paste_hooks,
    renderer_hooks,
    reset_history,
    selection_hooks,
)
from relaydeck.client.features.terminal.session.keys import KeyHooks, KeyInput
from relaydeck.client.features.terminal.session.lease import LeaseHooks, LeaseRenewalOwner
from relaydeck.client.features.terminal.session.paste import PasteFlow, PasteHooks
from relaydeck.client.features.terminal.session.recovery_state import DRIVE_REACQUIRE, RecoveryState
from relaydeck.client.features.terminal.session.rendering import (
    RendererBinding,
    RendererHooks,
)
from relaydeck.client.features.terminal.session.selection import (
    SelectionController,
    SelectionHooks,
)
from relaydeck.client.features.terminal.stream import EMULATOR_FRESH
from relaydeck.client.features.terminal.surfaces import (
    DRIVE_REFUSED_EXITED,
    EXIT_DETAILS,
    SnoopingTerminalSocket,
    drive_recovery,
)

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.surfaces import DetailSessions


class TerminalSessionBase(RecoveryState, SessionFacade, ABC):
    # Per-session constants (phase names and user-facing text).
    _PHASE_LIVE: str
    _PHASE_EXITED: str
    _PHASE_DISCONNECTED: str
    _PHASE_CONFLICT: str
    _DISCONNECT_MESSAGE: str
    _PAUSE_MESSAGE: str
    _NOUN: str  # "shell" / "agent": "the <noun> keeps running"
    _EXIT_MESSAGE: str  # notice when the terminal exits (bye closed/exited)
    _LEASE_ENDED_MESSAGE: str  # notice when recovery finds the stream lease ended elsewhere

    def __init__(
        self,
        *,
        ops: ShellGatewayOps,
        host_id: str,
        device_id: str,
        socket_factory: Callable[[str], TerminalSocket],
        clipboard: Any | None,
        on_bytes: Callable[[bytes], None] | None,
        sessions: DetailSessions,
        phase: str,
        renew_lease: Callable[[str], Awaitable[dict[str, Any]]] | None,
    ) -> None:
        self._ops = ops
        self._host_id = host_id
        self._device_id = device_id
        self._socket_factory = socket_factory
        self._clipboard = clipboard
        self._sink = on_bytes
        self._sessions = sessions
        self._phase = phase
        self._attachment: AttachmentInfo | None = None
        self._lease: LeaseInfo | None = None
        self._controller: TerminalController | None = None
        self._snoop: Any | None = None
        self._term_state = TerminalViewState(lease_mode="control")
        self._owner_label = ""
        self._notice = ""
        self._bracketed = False
        self._paused = False
        self._init_recovery()
        self._explicit_reconnects = 0
        self._disposed = False
        self._foreground = True
        self.note_input: Callable[[], None] = lambda: None
        self._interaction_generation = 0
        self.output_log = BoundedOutputLog()
        self._listeners: list[Callable[[Any], None]] = []
        self.cached_screen = ""
        # The five capabilities, composed from explicit hooks.
        self._renderer = RendererBinding(self._renderer_hooks())
        self._lease_owner = LeaseRenewalOwner(self._lease_hooks(), renew_lease=renew_lease)
        self._selection_ctl = SelectionController(self._selection_hooks())
        self._paste_flow = PasteFlow(self._paste_hooks())
        self._key_input = KeyInput(self._key_hooks())

    # -- abstract hooks ---------------------------------------------------------
    @abstractmethod
    def _on_attach_lost(self) -> None:
        """The writer is gone (pause/disconnect/viewer reopen): fence composer state."""

    @property
    @abstractmethod
    def _workflow_kind(self) -> str:
        """Current workflow: Agent Composer, Agent Direct or Shell."""

    @abstractmethod
    async def _reconnect_flow(self) -> str:
        """The session-specific reconnect, after the shared exited/recovery gate."""

    def _cancel_background_recovery(self) -> None:
        """Cancel automatic recovery work (agent: the view-watch retry)."""
        return None

    def _bump_epoch(self) -> None:
        """Invalidate existing composer callbacks (agent only)."""
        return None

    def _detach_store(self) -> None:
        """Settle and unbind the composer store from the writer (agent only)."""
        return None

    def _composer_store(self) -> ComposerStore | None:
        return None

    def _drops_engine_keys(self) -> bool:
        return False

    def _on_auth_revoked(self) -> None:
        """Identity/host authorization was lost (agent: fence every composer write)."""
        return None

    def _clear_composer(self) -> None:
        """Wipe composer state on a sensitive clear (agent only)."""
        return None

    async def _before_pause_close(self) -> None:
        """Pause step between stopping renewal and closing the stream (agent: persist draft)."""
        return None

    async def _after_resume_reconnect(self) -> None:
        """Resume step after the reconnect settles (agent: restore the draft)."""
        return None

    def _spawn_controller(self, lease: LeaseInfo) -> TerminalController:
        self.gap_recovery.stable_since = None

        def factory(ticket: str) -> TerminalSocket:
            snoop = SnoopingTerminalSocket(self._socket_factory(ticket))
            self._snoop = snoop
            return snoop

        controller = TerminalController(
            gap_recovery=self.gap_recovery,
            diagnostics=self._lease_trace(lease),
            socket_factory=factory,
            lease_id=lease.lease_id,
            generation=lease.generation,
            mode=lease.mode,
            on_bytes=lambda data: self._handle_bytes(data)
            if self._controller is controller and self._lease is lease
            else None,
            on_state=lambda state: self.update_term_state(state)
            if self._controller is controller and self._lease is lease
            else None,
            emulator=EMULATOR_FRESH,
            on_history_reset=lambda: reset_history(self, controller),
        )
        return controller

    def _classify_run_exit(self) -> str:
        """Phase after the stream ended without a transport error."""
        if self._phase == self._PHASE_EXITED:
            return self._phase
        doc = self._snoop.last_control if self._snoop is not None else None
        if (
            isinstance(doc, dict)
            and doc.get("kind") == "bye"
            and str(doc.get("detail", "")) in EXIT_DETAILS
        ):
            self._enter_exited(self._EXIT_MESSAGE)
            return self._phase
        controller = self._controller
        if (
            controller is not None
            and (controller.reattach_needed or controller.pending_resume is not None)
            and controller.state.notice is not None
        ):
            # Stream discontinuity with recovery pending (reset/gap): mirror
            # the controller's own notice (reset -> incomplete-history) so
            # the session never shows an unlabelled tail or a stale claim.
            self._notice = controller.state.notice.text
        else:
            self._notice = self._DISCONNECT_MESSAGE
        if self.gap_recovery.detail and self._recoverable_now():
            self._note_resume_mode()
            self._recovery_cause = RECOVERY_TRANSPORT
        self._phase = self._PHASE_DISCONNECTED
        self._notify()
        return self._phase

    async def drive_recovery(self) -> str:
        """Resume a valid reader, or reacquire under the session recovery budget."""
        if self._phase == self._PHASE_EXITED or self._controller is None:
            return DRIVE_REFUSED_EXITED
        controller = self._controller
        generation = self._interaction_generation

        def current() -> bool:
            return (
                self._controller is controller
                and self._foreground
                and self._operation_current(generation)
            )

        try:
            outcome = await drive_recovery(
                controller,
                self._ops.terminal_ticket,
                exited=False,
                current=current,
            )
        except ShellOpError as exc:
            if exc.code == "lease_expired":
                if not current():
                    return DRIVE_REFUSED_EXITED
                return await self._recover_gap_lease()
            raise
        if outcome == DRIVE_REACQUIRE:
            return await self._recover_gap_lease()
        self._notify()
        return outcome

    def _on_renewal_terminal(self, code: str) -> None:
        """Fence the failed writer; restoring input requires a fresh authorized lease.
        Auth or host loss additionally fences composer writes, preserving the draft."""
        self._cancel_background_recovery()
        if self._controller is not None:
            self._controller.invalidate_writer(code)
        if code in ("unauthenticated", "forbidden"):
            self._notice = (
                "Signed out or access revoked; input is disabled until you sign in again."
            )
            self._on_auth_revoked()
        else:
            self._notice = (
                f"Control lease {code.replace('_', ' ')}; "
                "input is disabled until you reconnect or take over."
            )
        self._notify()

    # -- run: template ----------------------------------------------------------
    def _begin_run(self) -> Any:
        return None

    def _snapshot_run(self, controller: TerminalController) -> Any:
        return None

    def _run_superseded(self, controller: TerminalController, snapshot: Any) -> bool:
        return controller is not self._controller

    async def _continue_after_run(
        self,
        controller: TerminalController,
        snapshot: Any,
        failure: TransportError | None,
        context: Any,
    ) -> bool:
        """True to supervise another stream (agent watch recovery); a shell stops."""
        return False

    async def run(self) -> str:
        """Supervise the stream: `bye closed/exited` is terminal exit;
        other endings (reset, link drop, server close) are disconnected.
        Hooks decide whether to supervise another stream."""
        context = self._begin_run()
        while (controller := self._controller) is not None:
            snapshot = self._snapshot_run(controller)
            failure: TransportError | None = None
            try:
                await controller.run()
            except TransportError as exc:
                failure = exc
            if self._run_superseded(controller, snapshot):
                return self._phase
            if failure is not None and self._phase != self._PHASE_EXITED:
                self._notice = self._DISCONNECT_MESSAGE
                self._phase = self._PHASE_DISCONNECTED
                self._notify()
            else:
                self._classify_run_exit()
            self._settle_gap_retry()
            if not await self._continue_after_run(controller, snapshot, failure, context):
                return self._phase
        return self._phase

    async def disconnect(self, *, keep_recovery: bool = False) -> str:
        """Back/leave: drop the live writer, release the lease and stop the
        local stream. The attachment and its remote process survive;
        reconnect resumes. Never marks exit."""
        self._bump_generation("disconnect")
        if not keep_recovery:
            self._recovery_cause = None  # a deliberate leave is never revived on its own
            self._stop_epoch += 1
        generation = self._interaction_generation
        # A reconnect or takeover can install a successor while teardown awaits: only the
        # resources captured now are ever closed or released.
        controller, lease = self._controller, self._lease
        self._cancel_background_recovery()
        self._sessions.invalidate()
        if controller is not None:
            controller.disconnected()
        self._on_attach_lost()
        await self.stop_lease_renewal()
        if controller is not None:
            try:
                await controller.close()
            except TransportError:
                pass
        if lease is not None:
            try:
                await self._ops.release_lease(lease.lease_id)
            except ShellOpError as exc:
                # The lease may already be dead (takeover elsewhere,
                # expiry): detachment still holds locally.
                if self._interaction_generation == generation:
                    self._notice = f"Detach noted ({exc.code}); the {self._NOUN} keeps running."
            if self._lease is lease:
                self._lease = None
        if self._interaction_generation != generation:
            return self._phase
        if self._phase != self._PHASE_EXITED:
            if not self._notice:
                self._notice = self._DISCONNECT_MESSAGE
            self._phase = self._PHASE_DISCONNECTED
        self._notify()
        return self._phase

    def _operation_current(self, generation: int) -> bool:
        guard = self._recovery_guard
        return (
            not self._disposed
            and not self._paused
            and generation == self._interaction_generation
            and (guard is None or guard())
        )

    def suspend_input(self) -> None:
        """Synchronous platform-event fence, preserving the terminal engine."""
        if self._paused:
            return
        self._note_resume_mode()
        self._bump_generation("suspend")
        self._cancel_background_recovery()
        self._paused = True
        if self._controller is not None:
            self._controller.pause_writer()
        self._on_attach_lost()

    async def pause(self) -> str:
        """App-background pause: invalidate the
        writer IMMEDIATELY (pause policy), stop the local stream, and
        release the lease best-effort (a failed release is covered by the
        60s server-side expiry). The attachment and its remote process
        survive; resume via `resume_after_pause()`. Never marks exit; a
        no-op on `exited` (nothing live to invalidate)."""
        if self._phase == self._PHASE_EXITED:
            return self._phase
        # Hiding must not turn a deliberate stop into something recoverable.
        eligible = self._recoverable_now()
        self.suspend_input()
        stop = self._stop_epoch

        def stopped() -> bool:
            return (
                self._stop_epoch != stop
                or self._disposed
                or self._phase in (self._PHASE_EXITED, self._PHASE_CONFLICT)
            )

        await self.stop_lease_renewal()
        if stopped():
            return self._phase
        await self._before_pause_close()
        if stopped():
            return self._phase
        await self._close_controller()
        if stopped():
            return self._phase
        lease = self._lease
        if lease is not None:
            try:
                await self._ops.release_lease(lease.lease_id)
            except ShellOpError as exc:
                if not stopped():
                    self._notice = f"Pause noted ({exc.code}); the {self._NOUN} keeps running."
            if self._lease is lease:
                self._lease = None
        if stopped():
            return self._phase
        self._paused = True
        if not self._notice:
            self._notice = self._PAUSE_MESSAGE
        self._phase = self._PHASE_DISCONNECTED
        self._recovery_cause = RECOVERY_LIFECYCLE if eligible else None
        self._notify()
        return self._phase

    async def resume_after_pause(self) -> str:
        """Explicit resume after `pause()`: clears the paused marker and
        reconnects through the normal path (never a silent steal)."""
        self._paused = False
        phase = await self.reconnect()
        await self._after_resume_reconnect()
        return phase

    async def reconnect(self, *, automatic: bool = False) -> str:
        """Reconnect a disconnected session. Refuses when exited."""
        if not automatic:
            self._explicit_reconnects += 1  # a user tap renews the automatic retry budget
            self.reset_gap_recovery()
            self._recovery_cause = None
        self.log_reopen("reconnect")
        self._cancel_background_recovery()
        if self._phase == self._PHASE_EXITED:
            return self._phase
        self._automatic = automatic
        try:
            return await self._reconnect_flow()
        finally:
            self._automatic = False

    async def detach(self) -> None:
        """Retention-registry teardown: lease release with the remote
        process surviving (never close). Satisfies `RetainedSurface`."""
        await self.disconnect()

    async def dispose(self, clear_sensitive: bool = False) -> None:
        """End the session: stop renewal, unmount the view, and detach.
        The remote process keeps running; this object must not be reused.
        Ordinary leave (Back/route/offline) keeps object contents: Root
        recreates sessions after Back. Pass `clear_sensitive=True` only
        for revoke/logout closes: cached output bytes, any
        composer draft, the selection snapshot, and notices are wiped
        instead of relying on GC."""
        self._disposed = True
        self._cancel_background_recovery()
        await self.stop_lease_renewal()
        self.unbind_view()
        await self.disconnect()
        await self._renderer.drain_close_tasks()
        if clear_sensitive:
            self._clear_sensitive_caches()

    def _clear_sensitive_caches(self) -> None:
        """Revoke/logout wipe: output bytes, any composer draft, the
        frozen selection snapshot, and notices (which may name owners or
        carry error detail) are dropped. Ordinary dispose and every
        live-session transition never call this."""
        self.output_log.clear()
        self.cached_screen = ""
        self._clear_composer()
        self._selection_snapshot = None
        self._notice = ""
        self._notify()

    def set_foreground(self, active: bool) -> None:
        """Retain engine/output/lease while invalidating hidden input paths."""
        if self._foreground == active:
            return
        self._foreground = active
        if self._controller is not None:
            self._controller.cancel_gap_wait()
        self._bump_generation("foreground")
        self._cancel_background_recovery()
        self._bump_epoch()
        if active and not self._disposed and self._controller is not None:
            self._sessions.bind(self._controller)
        elif not active:
            self._sessions.invalidate()
            for term in self._renderer.terms():
                term.set_input_enabled(False)
        self._notify()
        if active:
            self._resume_gap_retry()

    def _enter_exited(self, detail: str) -> None:
        self._recovery_cause = None
        self._stop_epoch += 1
        self._lease = None
        self._notice = detail
        self._phase = self._PHASE_EXITED
        self._notify()

    async def _close_controller(self) -> None:
        if self._controller is not None:
            try:
                await self._controller.close()
            except TransportError:
                pass

    async def resize(self, cols: int, rows: int) -> bool:
        """Control-lease resize through the controller. False means
        rejected (nothing sent); view leases never resize."""
        timing = open_timing.of(self)
        if timing is not None:
            timing.count("size_reports")
        if self._controller is None:
            return False
        resized = await self._controller.request_resize(cols, rows)
        if resized and timing is not None:
            timing.count("resizes")
            timing.mark("first_resize")
        self._notify()
        return resized

    def _handle_bytes(self, data: bytes) -> None:
        self._note_recovery_output()
        self.output_log += data
        # Bracketed-paste framing comes from the engine's MODE_REPORT; re-parsing output bytes in
        # Python diverges from the engine across RIS and split private-mode params.
        self._renderer.deliver(data)
        if self._sink is not None:
            self._sink(data)

    # -- hook builders: explicit callbacks, no session reference (see hooks.py) ---
    def _renderer_hooks(self) -> RendererHooks:
        return renderer_hooks(self)

    def _lease_hooks(self) -> LeaseHooks:
        return lease_hooks(self)

    def _selection_hooks(self) -> SelectionHooks:
        return selection_hooks(self)

    def _paste_hooks(self) -> PasteHooks:
        return paste_hooks(self)

    def _key_hooks(self) -> KeyHooks:
        return key_hooks(self)

    @abstractmethod
    def _terminal_label(self) -> str:
        """Terminal identity used in lease traces (agent: target, shell: terminal id)."""

    def _drop_controller(self) -> None:
        self._controller = None

    def _drop_lease(self) -> None:
        self._lease = None

    def _cancel_renewal_driver(self) -> None:
        self._lease_owner.cancel()

    def _bump_interaction(self, site: str) -> None:
        self._bump_generation(site)

    def _bump_generation(self, site: str) -> None:
        """Every interaction-generation increment is named."""
        self._interaction_generation += 1
        self._cancel_gap_retry()
        self._log_generation(site)

    def _mark_viewer_reopened(self, notice: str) -> None:
        self._note_resume_mode()
        self._recovery_cause = RECOVERY_TRANSPORT if self._recoverable_now() else None
        self._phase = self._PHASE_DISCONNECTED
        self._notice = notice

    def _note_stale_notice(self, stale: str | None) -> None:
        if stale is not None and self._phase == self._PHASE_LIVE and not self._notice:
            self._notice = stale

    def _set_term_state(self, state: TerminalViewState) -> None:
        self._term_state = state


__all__ = ["TerminalSessionBase"]
