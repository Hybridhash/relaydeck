"""Agent composer and generation-bound terminal interaction.

Draft edits stay local; sending requires committed input and the current writer.
Agent attach takes upstream ownership, so open and reconnect arm disclosure
without attaching. Confirmation authorizes takeover."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from relaydeck.client.adapters.ports import CredentialStore

from relaydeck.client import open_timing
from relaydeck.client.features.composer.domain import (
    SEND_IGNORED,
    ComposerStore,
    bind_blocked_writer,
)
from relaydeck.client.features.lifecycle.persist import (
    PausePersistReport,
    ResumeRestoreReport,
    draft_key,
    pause_persist,
    resume_restore,
)
from relaydeck.client.features.shell.ops import (
    AttachmentInfo,
    LeaseInfo,
    ShellGatewayOps,
    ShellOpError,
    owner_display_from_details,
)
from relaydeck.client.features.sync.transport import TerminalSocket, TransportError
from relaydeck.client.features.terminal.controller import WATCH_RETRY_DELAYS_S, TerminalController
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.geometry import DEFAULT_GEOMETRY, reported_geometry
from relaydeck.client.features.terminal.input_modes import (
    KEYBOARD_TEXT,
    WORKFLOW_AGENT_COMPOSER,
    InputModeState,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.session.base import TerminalSessionBase
from relaydeck.client.features.terminal.session.lease import LeaseHooks
from relaydeck.client.features.terminal.stream import EMULATOR_FRESH, EMULATOR_PRESERVED
from relaydeck.client.features.terminal.surfaces import (
    TAKEOVER_DISCLOSURE_AGENT,
    DetailSessions,
    gated_composer_send,
)
from relaydeck.client.features.terminal.ui_resume import terminal_transition
from relaydeck.client.features.terminal.warm_watch import watch_only
from relaydeck.contracts.terminal_ownership import is_control_revision

AGENT_CONNECTING = "connecting"
AGENT_LIVE = "live"
AGENT_DISCONNECTED = "disconnected"
AGENT_CONFLICT = "conflict"
AGENT_EXITED = "exited"

AGENT_DISCONNECT_MESSAGE = "Disconnected. The agent keeps running on the Host."
AGENT_PAUSE_MESSAGE = "Paused. The agent keeps running on the Host."
AGENT_EXIT_MESSAGE = "Agent terminal exited."
WATCH_RECONNECT_MESSAGE = "Reconnecting…"

TAKEOVER_UNDISCLOSED_MESSAGE = (
    "Takeover requires the ownership disclosure first; nothing was attached."
)


@dataclass(frozen=True)
class AgentViewState:
    """Immutable agent-detail view props for `mount_agent_screen`."""

    phase: str = AGENT_CONNECTING
    target: str = ""
    owner_label: str = ""
    notice: str = ""
    workflow: str = WORKFLOW_AGENT_COMPOSER
    keyboard: str = KEYBOARD_TEXT


@dataclass
class _RunContext:
    attempt: int = 0


@dataclass(frozen=True)
class _RunSnapshot:
    operation: int
    revision: int
    lease: LeaseInfo | None
    attachment: AttachmentInfo | None
    term: Any
    viewer_generation: Any
    offset: int
    started: float


@dataclass(frozen=True)
class _WatchRecovery:
    operation: int
    revision: int
    controller: TerminalController
    attachment: AttachmentInfo
    lease: LeaseInfo
    term: Any
    viewer_generation: Any
    state: TerminalViewState


class AgentSession(TerminalSessionBase):
    """One agent detail end to end (Flet-free): real composer store,
    generation-bound keys/paste sessions, disclosure-gated attach with
    takeover, control-lease streaming, disconnect/reconnect, recovery
    drive, and terminal exit. The caller supervises the `run()` task."""

    _PHASE_LIVE = AGENT_LIVE
    _PHASE_EXITED = AGENT_EXITED
    # A replacement native viewer cannot inherit the retired emulator's screen.
    _emulator_replaced: bool = False
    # Automatic takeover after a verified reload follows the Settings preference
    # (set by the retained-session view before each render).
    auto_take_allowed: bool = False
    _PHASE_DISCONNECTED = AGENT_DISCONNECTED
    _PHASE_CONFLICT = AGENT_CONFLICT
    _DISCONNECT_MESSAGE = AGENT_DISCONNECT_MESSAGE
    _PAUSE_MESSAGE = AGENT_PAUSE_MESSAGE
    _NOUN = "agent"
    _EXIT_MESSAGE = AGENT_EXIT_MESSAGE
    _LEASE_ENDED_MESSAGE = "Stream lease ended elsewhere; take over explicitly."

    def __init__(
        self,
        *,
        ops: ShellGatewayOps,
        host_id: str,
        device_id: str,
        socket_factory: Callable[[str], TerminalSocket],
        submit_command: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        prompt_text: Callable[[str, str], Awaitable[dict[str, Any]]] | None,
        host_epoch: str,
        agent_id: str,
        target: str,
        initial_status: str = "idle",
        clipboard: Any | None = None,
        on_bytes: Callable[[bytes], None] | None = None,
        draft_store: CredentialStore | None = None,
        renew_lease: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        observe_command: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        expected_revision: int = 0,
    ) -> None:
        self._agent_id = agent_id
        self._store = ComposerStore(
            target=target,
            submit_command=submit_command,
            prompt_text=prompt_text,
            host_id=host_id,
            host_epoch=host_epoch,
            agent_id=agent_id,
            initial_status=initial_status,
            observe_command=observe_command,
            expected_revision=expected_revision,
            on_change=self._notify,
        )
        self._target = ""
        self._composer_epoch = 0
        self._auth_revoked = False
        self._watch_retry_task: asyncio.Task[int | None] | None = None
        self._watch_recovery_revision = 0
        super().__init__(
            ops=ops,
            host_id=host_id,
            device_id=device_id,
            socket_factory=socket_factory,
            clipboard=clipboard,
            on_bytes=on_bytes,
            sessions=DetailSessions(
                modes=InputModeState(workflow=WORKFLOW_AGENT_COMPOSER, store=self._store)
            ),
            phase=AGENT_CONNECTING,
            renew_lease=renew_lease,
        )
        self._pending_target = target
        self._pending_geometry = (80, 24)
        self._status_label = initial_status
        self._disclosure_armed = False
        self._takeover_flight: asyncio.Future[str] | None = None
        # Set while the app is about to take control on its own (the Settings rule): the screen
        # then lays out as in control from the first frame instead of flashing the take-over panel.
        self._control_expected = False
        self._draft_store = draft_store
        self._last_pause_persist: PausePersistReport | None = None
        self._last_resume_restore: ResumeRestoreReport | None = None
        self.draft_loading = False

    # -- observable state -------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def title(self) -> str:
        return self._agent_id

    @property
    def target(self) -> str:
        return self._target

    @property
    def store(self) -> ComposerStore:
        return self._store

    @property
    def last_pause_persist(self) -> PausePersistReport | None:
        """The last pause-time draft persist report (None before any `pause()`)."""
        return self._last_pause_persist

    @property
    def last_resume_restore(self) -> ResumeRestoreReport | None:
        """The last resume-time draft restore report (None when no draft
        store is wired or `resume_after_pause()` has not run)."""
        return self._last_resume_restore

    @property
    def agent_status_label(self) -> str:
        """Read-only metadata display (rendered verbatim, never inferred)."""
        return self._status_label

    @property
    def conflict_owner(self) -> str | None:
        if self._phase == AGENT_CONFLICT:
            return self._owner_label
        return None

    @property
    def takeover_disclosure(self) -> str | None:
        if self._disclosure_armed:
            return TAKEOVER_DISCLOSURE_AGENT
        return None

    # -- session-skeleton hooks (see TerminalSessionBase) ---------------------
    @property
    def _workflow_kind(self) -> str:
        return self._sessions.modes.workflow

    def _terminal_label(self) -> str:
        return self._target

    def _composer_store(self) -> ComposerStore | None:
        return self._store

    def _drops_engine_keys(self) -> bool:
        # Composer typing stays local: engine keys are dropped while it edits the draft.
        return self._sessions.modes.text_edits_draft

    def _cancel_background_recovery(self) -> None:
        if self._disposed or not self._foreground:
            terminal_transition(self).invalidate()
        self._cancel_watch_recovery()

    def _bump_generation(self, site: str) -> None:
        transition = terminal_transition(self)
        if (
            site in ("disconnect", "suspend")
            or (site == "foreground" and not self._foreground)
            or (site in ("unbind_view", "viewer_reopened") and transition.recovery is None)
        ):
            transition.invalidate()
        super()._bump_generation(site)

    def _bump_epoch(self) -> None:
        self._bump_composer_epoch()

    def _detach_store(self) -> None:
        self._store.reconnect("detach")
        self._store.unbind_writer("detach")

    def _on_attach_lost(self) -> None:
        self._bump_composer_epoch()
        self._detach_store()

    def _on_auth_revoked(self) -> None:
        """Fence saved and future composer sends after authorization loss.
        Keep drafts editable; only explicit takeover re-arms the writer."""
        self._auth_revoked = True
        terminal_transition(self).invalidate()
        self._bump_composer_epoch()

    def _lease_hooks(self) -> LeaseHooks:
        hooks = super()._lease_hooks()

        def failed(code: str) -> None:
            terminal_transition(self).invalidate()
            hooks.on_terminal(code)

        return replace(hooks, on_terminal=failed)

    def _clear_composer(self) -> None:
        self._store.clear_sensitive()

    async def _before_pause_close(self) -> None:
        await self._persist_pause_draft()
        if self._controller is not None:
            self._controller.pause_writer()

    async def _after_resume_reconnect(self) -> None:
        await self._restore_resume_draft()
        self._notify()

    # -- run supervision (template hooks): only a dropped foreground VIEW may auto-resume.
    def _begin_run(self) -> _RunContext:
        return _RunContext()

    def _snapshot_run(self, controller: TerminalController) -> _RunSnapshot:
        term = self.current_term
        return _RunSnapshot(
            operation=self._interaction_generation,
            revision=self._watch_recovery_revision,
            lease=self._lease,
            attachment=self._attachment,
            term=term,
            viewer_generation=getattr(term, "viewer_generation", None),
            offset=controller.state.applied_offset,
            started=asyncio.get_running_loop().time(),
        )

    def _run_superseded(self, controller: TerminalController, snapshot: Any) -> bool:
        return (
            controller is not self._controller
            or snapshot.lease is not self._lease
            or snapshot.attachment is not self._attachment
        )

    async def _continue_after_run(
        self,
        controller: TerminalController,
        snapshot: Any,
        failure: TransportError | None,
        context: Any,
    ) -> bool:
        """Control, exit and invalidated viewers retain their explicit recovery.
        Repeated opens without output/30 seconds of liveness share one budget."""
        lease, attachment = snapshot.lease, snapshot.attachment
        doc = self._snoop.last_control if self._snoop is not None else None
        if (
            lease is None
            or attachment is None
            or lease.mode != "view"
            or snapshot.revision != self._watch_recovery_revision
            or not self._foreground
            or not self._operation_current(snapshot.operation)
            or self._phase == AGENT_EXITED
            or self._auth_revoked
            or controller.reattach_needed
            or controller.pending_resume is not None
            or controller.live_edge_pending
            or (isinstance(doc, dict) and doc.get("kind") == "bye")
            or (failure is not None and failure.code in ("unauthenticated", "forbidden"))
        ):
            return False
        if (
            controller.state.applied_offset > snapshot.offset
            or asyncio.get_running_loop().time() - snapshot.started >= 30
        ):
            context.attempt = 0
        recovery = _WatchRecovery(
            snapshot.operation,
            snapshot.revision,
            controller,
            attachment,
            lease,
            snapshot.term,
            snapshot.viewer_generation,
            self._term_state,
        )
        # Register the entire operation before its first await, including
        # renewal shutdown. Never rediscover identity after that shutdown.
        retry = asyncio.create_task(self._recover_watch(recovery, context.attempt))
        self._watch_retry_task = retry
        try:
            next_attempt = await retry
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling():
                raise
            return False
        finally:
            if self._watch_retry_task is retry:
                self._watch_retry_task = None
            if self._notice == WATCH_RECONNECT_MESSAGE:
                self._notice = AGENT_DISCONNECT_MESSAGE
                self._notify()
        if next_attempt is None:
            return False
        context.attempt = next_attempt
        return True

    # -- lease renewal ----------------------------------------------------
    def view(self) -> AgentViewState:
        modes = self._sessions.modes
        return AgentViewState(
            phase=self._phase,
            target=self._target,
            owner_label=self._owner_label,
            notice=self._notice,
            workflow=modes.workflow,
            keyboard=modes.keyboard,
        )

    @property
    def ui_input_fenced(self) -> bool:
        return self._controller is not None and self._controller.ui_input_fenced

    def set_agent_status(self, status: str, label: str = "") -> None:
        """Fold a status push into composer progress; `label` (which may
        contain approval-looking text) is inert display metadata only."""
        self._status_label = label or status
        self._store.agent_status_did_change(status, label)
        self._notify()

    # -- composer wiring (the five ComposerBox callbacks) ------------------
    def composer_props(self) -> dict[str, Any]:
        """The eight controlled `ComposerBox` props from the real store."""
        pending = self._store.pending_blocked
        return {
            "draft": self._store.draft,
            "markers": self._store.markers,
            "can_send": self._store.can_send,
            "blockers": self._store.send_blockers(),
            "submission": self._store.submission,
            "error": self._store.error,
            "review": pending.review if pending is not None else None,
            "input_mode": self._store.input_mode,
        }

    def composer_callbacks(self) -> dict[str, Callable[..., Any]]:
        """The five `ComposerBox` callbacks, lifetime-gated.

        Each closure captures the current composer epoch: after any
        lifecycle transition (disconnect, pause, reconnect, unbind,
        dispose) saved closures go stale and fail closed (`ignored` /
        `False` / no-op) with the draft preserved and nothing
        submitted — so logout/switch/dispose never leave a stale
        callback capable of submitting. Mounts re-fetch every render,
        so live views always hold fresh closures."""
        epoch = self._composer_epoch

        def _live() -> bool:
            return (
                self._foreground
                and not self._paused
                and not self._disposed
                and not self.draft_loading
                and epoch == self._composer_epoch
            )

        def _send_live() -> bool:
            # Composer WRITES additionally require identity/host
            # authorization: after auth loss even freshly obtained
            # closures fail closed (draft edits stay live so unsent
            # work is never trapped).
            return _live() and not self._auth_revoked and not self.ui_input_fenced

        def _on_draft_change(text: str) -> None:
            if _live():
                self.on_draft_change(text)

        async def _on_send() -> str:
            if not _send_live():
                return SEND_IGNORED
            return await self.on_send()

        async def _on_confirm_blocked() -> bool:
            if not _send_live():
                return False
            return await self.on_confirm_blocked()

        def _on_cancel_blocked() -> bool:
            if not _live():
                return False
            return self.on_cancel_blocked()

        def _on_clear_error() -> None:
            if _live():
                self.on_clear_error()

        return {
            "on_draft_change": _on_draft_change,
            "on_send": _on_send,
            "on_confirm_blocked": _on_confirm_blocked,
            "on_cancel_blocked": _on_cancel_blocked,
            "on_clear_error": _on_clear_error,
        }

    def _bump_composer_epoch(self) -> None:
        """Invalidate existing composer callbacks (logout /
        switch / dispose path). The draft and uncertain outcomes are
        preserved; nothing is replayed."""
        self._composer_epoch += 1

    def on_draft_change(self, text: str) -> None:
        if self._disposed:
            return
        self.note_input()
        self._store.replace_draft(text)
        self._notify()

    async def on_send(self) -> str:
        """Only route to store.send(): IME composition blocks without mutation.
        Disposed, revoked and fenced sessions preserve the draft.
        Successful delivery synchronizes the tools-keys latch."""
        if self._disposed or self._auth_revoked or self.ui_input_fenced:
            return SEND_IGNORED
        self.note_input()
        result = await gated_composer_send(self._store, self._sessions.ime)
        self._sessions.modes.sync_from_store()
        self._notify()
        return result

    async def on_confirm_blocked(self) -> bool:
        if self._disposed or self._auth_revoked or self.ui_input_fenced:
            return False
        self.note_input()
        confirmed = await self._store.confirm_blocked_insert()
        self._sessions.modes.sync_from_store()
        self._notify()
        return confirmed

    def on_cancel_blocked(self) -> bool:
        if self._disposed:
            return False
        cancelled = self._store.cancel_blocked_insert()
        self._notify()
        return cancelled

    def on_clear_error(self) -> None:
        if self._disposed:
            return
        self._store.clear_error()
        self._notify()

    def submit_allowed(self) -> bool:
        """IME completion gate (mirrors the `on_send` choke point)."""
        return self._sessions.submit_allowed()

    # -- disclosure-gated attach -------------------------------------------
    def request_takeover(self) -> str:
        """Arm the ownership disclosure. Idempotent; performs no
        attach — the disclosure must render before `confirm_takeover`."""
        self._cancel_watch_recovery()
        self._disclosure_armed = True
        open_timing.mark_current("disclosure")
        self._notify()
        return TAKEOVER_DISCLOSURE_AGENT

    def _live_edge_owns_recovery(self) -> bool:
        # A parked viewer's pending live-edge resume must not swallow the takeover replacing it.
        controller, lease = self._controller, self._lease
        pending = controller is not None and controller.live_edge_pending
        return pending and lease is not None and lease.mode == "control"

    async def auto_take_control(self) -> str:
        """Take unheld control automatically; conflicts and unknown ownership stay explicit."""
        if self._live_edge_owns_recovery():
            return await self.drive_recovery()
        return await terminal_transition(self).automatic(self)

    async def _auto_take_control(self, token: object) -> str:
        self.log_reopen("auto_take")
        if self._disposed or self._paused or not self._foreground or self._phase == AGENT_EXITED:
            return self._phase
        if self._live_edge_owns_recovery():
            return await self.drive_recovery()
        if outcome := self.defer_gap_recovery(self.auto_take_control):
            return outcome
        lease = self._lease
        if (
            lease is not None
            and lease.mode == "control"
            and self._controller is not None
            and not self.gap_recovery.detail
        ):
            return self._phase
        target = self._pending_target or self._target
        if not target:
            return self._phase
        operation = self._interaction_generation
        try:
            state = await self._ops.control_holder(self._host_id, target)
        except ShellOpError:
            return self._phase
        if (
            not isinstance(state.get("held"), bool)
            or not isinstance(state.get("mine"), bool)
            or state["mine"]
            and not state["held"]
        ):
            return self._phase
        if (
            not self._operation_current(operation)
            or terminal_transition(self).control is not token
            or not self._foreground
        ):
            return self._phase
        if state.get("held") and not state.get("mine"):
            self._owner_label = owner_display_from_details(state)
            self._phase = AGENT_CONFLICT
            self._disclosure_armed = True
            self._notify()
            return self._phase
        revision = state.get("control_revision")
        if not is_control_revision(revision):
            return self._phase
        self.request_takeover()
        return await terminal_transition(self).confirm(self, expected_control_revision=revision)

    def cancel_takeover(self) -> None:
        self._disclosure_armed = False
        self._control_expected = False
        terminal_transition(self).invalidate()
        self._notify()

    def begin_control_transition(self) -> object:
        return terminal_transition(self).begin(self)

    def end_control_transition(self, token: object) -> None:
        terminal_transition(self).end(self, token)

    def open(self, *, cols: int = 80, rows: int = 24) -> str:
        """Agent open arms the takeover disclosure and performs NO attach:
        agent attach always takes upstream ownership, so even the first
        attach needs the same explicit confirm as any takeover. The target was bound at
        construction; a re-open keeps it (confirm consumed the pending one), like `reconnect`."""
        self.log_reopen("open")
        self._pending_geometry = (cols, rows)
        self._pending_target = self._pending_target or self._target
        self._target = self._pending_target
        self._phase = AGENT_CONNECTING
        self._notice = ""
        self.request_takeover()
        return self._phase

    def expect_control(self, expected: bool = True) -> None:
        if self._control_expected != expected:
            self._control_expected = expected
            self._notify()

    @property
    def expected_geometry(self) -> tuple[int, int] | None:
        """The size this terminal was (or will be) attached at, when it was measured."""
        size = reported_geometry(self.current_term) or self._pending_geometry
        return None if size == DEFAULT_GEOMETRY else size

    @property
    def control_pending(self) -> bool:
        """Control is expected or its authorized flight is still settling.

        Conflict, exit or a completed failure restores the usual ownership panels.
        """
        if self._phase in (AGENT_CONFLICT, AGENT_EXITED):
            return False
        # Closing the replaced reader can briefly project disconnected during this flight.
        if terminal_transition(self).control is not None:
            return True
        if not self._control_expected or self._phase == AGENT_DISCONNECTED:
            return False
        lease = self._lease
        return not (self._phase == AGENT_LIVE and lease is not None and lease.mode == "control")

    @property
    def takeover_busy(self) -> bool:
        """A confirmed takeover is still attaching; further confirms join it."""
        flight = self._takeover_flight
        return flight is not None and not flight.done()

    async def confirm_takeover(self) -> str:
        """Confirm disclosure once; concurrent callers join the same flight."""
        self.reset_gap_recovery()
        self.log_reopen("take_over")
        return await terminal_transition(self).confirm(self)

    async def _confirm_takeover(
        self,
        token: object,
        *,
        expected_control_revision: str | None = None,
    ) -> str:
        """Attach with takeover (generation bump + epoch rotation server-side), take a fresh
        control lease, replace the controller, rebind sessions and the blocked writer, and
        stream. Raises without attaching unless the disclosure was armed first."""
        self._cancel_watch_recovery()
        if not self._disclosure_armed:
            raise RuntimeError(TAKEOVER_UNDISCLOSED_MESSAGE)
        operation = self._interaction_generation

        def current() -> bool:
            return (
                self._operation_current(operation)
                and self._foreground
                and terminal_transition(self).control is token
            )

        if not current():
            return self._phase
        target = self._pending_target or self._target
        if not target:
            raise RuntimeError("confirm_takeover with no target")
        await self.stop_lease_renewal()
        if not current():
            return self._phase
        await self._close_controller()
        # The bound viewer's own measured size, when it has one, beats the size at open.
        cols, rows = reported_geometry(self.current_term) or self._pending_geometry
        if not current():
            return self._phase
        try:
            attachment = await self._ops.attach(
                self._host_id,
                target,
                "agent",
                True,
                cols,
                rows,
                **(
                    {"expected_control_revision": expected_control_revision}
                    if expected_control_revision is not None
                    else {}
                ),
            )
        except (ShellOpError, TransportError) as exc:
            if not current():
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(getattr(exc, "details", {}))
                self._phase = AGENT_CONFLICT
            else:
                self._notice = f"Take over failed ({exc.code})."
                self._phase = AGENT_DISCONNECTED
            self._notify()
            return self._phase
        if not current():
            return self._phase
        try:
            lease = await self._ops.acquire_lease(
                attachment.attachment_id,
                "control",
                **(
                    {"expected_generation": attachment.generation}
                    if expected_control_revision is not None
                    else {}
                ),
            )
        except ShellOpError as exc:
            if not current():
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = AGENT_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Could not take control ({exc.code})."
            self._phase = AGENT_DISCONNECTED
            self._notify()
            return self._phase
        if not current():
            await self._release_stale_lease(lease)
            return self._phase
        self._attachment = attachment
        self._lease = lease
        self._target = target
        self._pending_target = ""
        # Controller replacement settles in-flight sends as unknown (no
        # replay) and drops any pending blocked review bound to the dead
        # writer; rebinding the live writer afterwards is done below.
        self._store.reconnect("takeover")
        controller = self._spawn_controller(lease)
        controller.fence_ui_input(True)
        self._controller = controller
        self._term_state = TerminalViewState(lease_mode="control")
        # A replacement PTY must re-establish bracketed-paste mode from its own report.
        self._bracketed = False
        # Keep incremental redraws on the existing screen only when its native viewer
        # still owns that screen; replaced or unverified viewers need a fresh baseline.
        replaced = self._emulator_replaced or any(
            getattr(term, "resume_unverified", False) for term in self._renderer.terms()
        )
        controller.emulator = EMULATOR_FRESH if replaced else EMULATOR_PRESERVED
        self._emulator_replaced = False
        bind_blocked_writer(self._store, controller)
        if self._sessions.bound:
            stale = self._sessions.retarget(controller)
            self._notice = stale or ""
        elif self._foreground:
            self._sessions.bind(controller)
            self._notice = ""
        if replaced:
            # The reset retires the fence and re-syncs the screen from the new stream.
            self._reset_bound_renderers()
        try:
            ticket = await self._ops.terminal_ticket(lease.lease_id)
        except (ShellOpError, TransportError) as exc:
            if not current():
                await self._release_stale_lease(lease)
                return self._phase
            # Name the failure; the lease stays held so UI resume can re-ticket it.
            diagnostics_logger().info("ticket_failed code=%s", exc.code)
            self._notice = f"Could not stream ({exc.code})."
            self._phase = AGENT_DISCONNECTED
            self._notify()
            return self._phase
        if not current():
            await self._release_stale_lease(lease)
            return self._phase
        try:
            await controller.start(ticket, "", 0)
        except TransportError as exc:
            # A refusal for a lease this client already closed or superseded is expected
            # (the server rightly refuses a released lease): drop it quietly. A failure for
            # the current lease is the inline disconnected state with its retry action.
            diagnostics_logger().info("takeover_connect_failed code=%s", exc.code)
            if not current() or self._controller is not controller:
                await self._release_stale_lease(lease)
                return self._phase
            self._notice = f"Could not stream ({exc.code})."
            self._phase = AGENT_DISCONNECTED
            self._notify()
            return self._phase
        if not current():
            await self._release_stale_lease(lease)
            return self._phase
        self._disclosure_armed = False
        self._phase = AGENT_LIVE
        # The accepted socket and its lease establish the epoch even on a quiet pane.
        # Byte offsets and replay decisions still come only from the stream reader.
        controller._set(replace(controller.state, status="live", epoch=lease.epoch))
        # An explicit authorized takeover re-arms composer writes after
        # auth loss (saved pre-loss closures stay dead: the epoch moved).
        self._auth_revoked = False
        self._restart_renewal()
        self._notify()
        return self._phase

    def _cancel_watch_recovery(self) -> None:
        # Explicit intent also invalidates a reader that has not registered its
        # retry task yet. Task cancellation alone cannot fence that window.
        self._watch_recovery_revision += 1
        task = self._watch_retry_task
        if task is not None and not task.done():
            task.cancel()

    async def _recover_watch(self, context: _WatchRecovery, attempt: int) -> int | None:
        retry_task = asyncio.current_task()

        def current() -> bool:
            return (
                self._operation_current(context.operation)
                and self._watch_recovery_revision == context.revision
                and retry_task is not None
                and not retry_task.cancelling()
                and self._foreground
                and not self._auth_revoked
                and self._phase != AGENT_EXITED
                and self._attachment is context.attachment
                and self.current_term is context.term
                and getattr(context.term, "viewer_generation", None) == context.viewer_generation
                # Failed acquisitions can leave these empty. They must never
                # become a replacement control session captured by this retry.
                and (self._lease is None or self._lease.mode == "view")
                and (self._controller is None or self._controller.mode == "view")
            )

        def dropped_current() -> bool:
            return (
                current()
                and self._controller is context.controller
                and self._lease is context.lease
            )

        if not dropped_current():
            return None
        await self.stop_lease_renewal()
        if not dropped_current():
            return None
        for index in range(attempt, len(WATCH_RETRY_DELAYS_S)):
            if not current():
                return None
            self._notice = WATCH_RECONNECT_MESSAGE
            self._notify()
            self._lease_trace(context.lease).emit("watch_retry", reason="retry", attempt=index + 1)
            await asyncio.sleep(WATCH_RETRY_DELAYS_S[index])
            if not current():
                return None
            if await watch_only(self, current, context.attachment, preserved_state=context.state):
                return index + 1
        if current():
            self._lease_trace(context.lease).emit(
                "watch_retry", reason="exhausted", attempt=len(WATCH_RETRY_DELAYS_S)
            )
            self._phase = AGENT_DISCONNECTED
            self._notice = AGENT_DISCONNECT_MESSAGE
            self._notify()
        return None

    async def _persist_pause_draft(self) -> None:
        """Persist the settled pause draft per the storage policy.

        Secure storage only; a secure outage holds the draft in memory
        and appends the capability failure to the notice (never
        plaintext). No draft store wired: nothing to persist.
        """
        if self._draft_store is None:
            self._last_pause_persist = None
            return
        report = await pause_persist(
            self._draft_store,
            draft_key(self._host_id + ":" + self._agent_id),
            self._store.export_draft(),
        )
        self._last_pause_persist = report
        if report.capability_error is not None:
            suffix = report.capability_error
            self._notice = f"{self._notice} {suffix}".strip() if self._notice else suffix

    async def prepare_draft(self) -> None:
        """Show the terminal immediately while secure draft input remains gated."""
        self.draft_loading = True
        self._notify()
        try:
            await self._restore_resume_draft()
        finally:
            self.draft_loading = False
            self._notify()

    async def _restore_resume_draft(self) -> None:
        """Restore the pause-persisted draft into the live store.

        Runs after `reconnect()` settles (which clears the notice), so a
        restore failure stays visible. Missing key or outage: keep the
        live in-memory draft; corrupt payload: keep it and disclose.
        """
        if self._draft_store is None:
            self._last_resume_restore = None
            return
        report = await resume_restore(
            self._draft_store, draft_key(self._host_id + ":" + self._agent_id)
        )
        self._last_resume_restore = report
        if report.snapshot is not None:
            try:
                self._store.import_draft(report.snapshot)
            except ValueError as exc:
                if not self._notice:
                    self._notice = f"Stored draft unreadable ({exc}); kept the current draft."
        elif report.capability_error is not None and not self._notice:
            self._notice = report.capability_error

    def _mark_viewer_reopened(self, notice: str) -> None:
        self._emulator_replaced = True
        super()._mark_viewer_reopened(notice)

    async def recover_after_reload(self) -> str:
        return await self._recover_after_reload()

    async def _reconnect_flow(self) -> str:
        """Re-arm ownership disclosure without attaching; confirm_takeover completes it.
        The base already refused when exited and cancelled any watch recovery."""
        terminal_transition(self).invalidate()
        if not (self._target or self._pending_target):
            raise RuntimeError("reconnect with no target")
        await self.stop_lease_renewal()
        self._bump_composer_epoch()
        self._store.reconnect("reconnect_flow")
        self._store.unbind_writer("reconnect_flow")
        await self._close_controller()
        self._notice = ""
        self._pending_target = self._target or self._pending_target
        self._disclosure_armed = True
        self._phase = AGENT_DISCONNECTED
        self._notify()
        return self._phase

    # -- workflow / keyboard modes -----------------------------------------------
    def set_workflow(self, workflow: str) -> None:
        """Switch workflows, preserving the hidden draft and any active IME
        composition by construction (nothing autosent or cleared)."""
        self._sessions.modes.set_workflow(workflow)
        self._notify()

    def set_keyboard(self, keyboard: str) -> bool:
        """Preserve the draft while switching Text/Keys; report a tools-latch reset."""
        reset = self._sessions.modes.set_keyboard(keyboard)
        self._notify()
        return reset
