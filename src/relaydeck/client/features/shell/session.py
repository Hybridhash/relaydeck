"""Standalone shell lifecycle and terminal interaction.

Owns attach, explicit takeover, lease streaming and renewal, direct input,
selection, reviewed paste and control-only resize. Disconnect releases the
lease while the remote process survives; explicit close ends the remote pane.
An exited session cannot reconnect. Fresh attachments clear obsolete output
and disclose incomplete history. Shells have no composer or agent metadata."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from relaydeck.client.features.create.commands import session_close_command
from relaydeck.client.features.shell.ops import (
    ShellGatewayOps,
    ShellOpError,
    owner_display_from_details,
)
from relaydeck.client.features.sync.transport import TerminalSocket
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.input_modes import (
    WORKFLOW_SHELL,
    InputModeState,
)
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.session.base import TerminalSessionBase
from relaydeck.client.features.terminal.surfaces import (
    DetailSessions,
)
from relaydeck.contracts.terminal_ownership import is_control_revision

SHELL_CONNECTING = "connecting"
SHELL_LIVE = "live"
SHELL_DISCONNECTED = "disconnected"
SHELL_CONFLICT = "conflict"
SHELL_EXITED = "exited"

CLOSE_TITLE = "Close Terminal?"
CLOSE_MESSAGE = (
    "This closes the pane on the Host, ending anything running in it. "
    "Going Back instead leaves it for desktop handoff."
)
DISCONNECT_MESSAGE = "Disconnected. The shell keeps running on the Host."
PAUSE_MESSAGE = "Paused. The shell keeps running on the Host."
EXIT_MESSAGE = "Shell exited."
SELF_TAKEOVER_NOTICE = "Reconnected to your shell (ownership renewed)."
FRESH_ATTACH_NOTICE = "Shell ended elsewhere; opened a fresh terminal."


@dataclass(frozen=True)
class ShellViewState:
    """Immutable shell-detail view props for `ShellScreen`."""

    phase: str = SHELL_CONNECTING
    terminal_id: str = ""
    owner_label: str = ""
    notice: str = ""
    confirm_close: bool = False
    keyboard: str = "text"
    reconnecting: bool = False


class ShellSession(TerminalSessionBase):
    """One standalone shell detail (Flet-free). The caller supervises the
    `run()` task (cancel it after `disconnect()`/`close_terminal()`; a
    server-side close ends it and the session classifies the exit)."""

    _PHASE_LIVE = SHELL_LIVE
    _PHASE_EXITED = SHELL_EXITED
    _PHASE_DISCONNECTED = SHELL_DISCONNECTED
    _PHASE_CONFLICT = SHELL_CONFLICT
    _DISCONNECT_MESSAGE = DISCONNECT_MESSAGE
    _PAUSE_MESSAGE = PAUSE_MESSAGE
    _NOUN = "shell"
    _EXIT_MESSAGE = EXIT_MESSAGE
    _LEASE_ENDED_MESSAGE = "Stream lease ended elsewhere; reconnect or take over explicitly."

    def __init__(
        self,
        *,
        ops: ShellGatewayOps,
        host_id: str,
        device_id: str,
        socket_factory: Callable[[str], TerminalSocket],
        clipboard: Any | None = None,
        on_bytes: Callable[[bytes], None] | None = None,
        renew_lease: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        host_epoch: str = "",
    ) -> None:
        self._terminal_id = ""
        self._host_epoch = host_epoch
        self._confirm_close = False
        super().__init__(
            ops=ops,
            host_id=host_id,
            device_id=device_id,
            socket_factory=socket_factory,
            clipboard=clipboard,
            on_bytes=on_bytes,
            sessions=DetailSessions(modes=InputModeState(workflow=WORKFLOW_SHELL)),
            phase=SHELL_CONNECTING,
            renew_lease=renew_lease,
        )

    # -- observable state -------------------------------------------------
    @property
    def terminal_id(self) -> str:
        return self._terminal_id

    # -- session-skeleton hooks (see TerminalSessionBase) ---------------------
    @property
    def _workflow_kind(self) -> str:
        return WORKFLOW_SHELL

    def _terminal_label(self) -> str:
        return self._terminal_id

    def _on_attach_lost(self) -> None:
        # A shell has no composer: nothing to fence besides the writer itself.
        return None

    def view(self) -> ShellViewState:
        keyboard = self._sessions.modes.keyboard
        return ShellViewState(
            phase=self._phase,
            terminal_id=self._terminal_id,
            owner_label=self._owner_label,
            notice=self._notice,
            confirm_close=self._confirm_close,
            keyboard=keyboard,
            reconnecting=self._recovering,
        )

    # -- lifecycle ----------------------------------------------------------
    def _fresh_controller_state(self) -> None:
        """Reset per-stream state for a replacement controller: a new PTY
        starts unnegotiated and the old screen cannot continue. The
        engine's post-reset mode report re-establishes the 2004 truth."""
        self._term_state = TerminalViewState(lease_mode="control")
        self._bracketed = False

    async def open(self, terminal_id: str, cols: int = 80, rows: int = 24) -> str:
        """Attach without takeover, acquire control, and stream.

        Conflicts disclose the owner without taking over or resizing. Geometry
        accepts positional or keyword values with defaults for single-arg calls.
        """
        operation = self._interaction_generation
        if self._disposed or self._paused:
            return self._phase
        await self.stop_lease_renewal()
        if not self._operation_current(operation):
            return self._phase
        self._terminal_id = terminal_id
        self._phase = SHELL_CONNECTING
        self._notice = ""
        try:
            attachment = await self._ops.attach(
                self._host_id, terminal_id, "shell", False, cols, rows
            )
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Could not open the shell ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            return self._phase
        bound: dict[str, Any] = {}
        try:
            lease = await self._ops.acquire_lease(attachment.attachment_id, "control", **bound)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Could not take control ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._attachment = attachment
        self._lease = lease
        controller = self._spawn_controller(lease)
        if self._automatic:
            controller.fence_ui_input(True)  # input waits for native verification
        self._controller = controller
        self._fresh_controller_state()
        if self._sessions.bound:
            stale = self._sessions.retarget(controller)
            if stale is not None:
                self._notice = stale
        elif self._foreground:
            self._sessions.bind(controller)
        try:
            ticket = await self._ops.terminal_ticket(lease.lease_id)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                await self._release_stale_lease(lease)
                return self._phase
            # Name the failure; the lease stays held so UI resume can re-ticket it.
            diagnostics_logger().info("ticket_failed code=%s", exc.code)
            self._notice = f"Could not stream ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        await controller.start(ticket, "", 0)
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._phase = SHELL_LIVE
        self._reset_bound_renderers()
        self._restart_renewal()
        self._notify()
        return self._phase

    async def take_over(self, *, expected_control_revision: str | None = None) -> str:
        """Explicit Take Over: attach with takeover (generation bump +
        epoch rotation server-side), take a fresh control lease, replace
        the controller, and retarget sessions with IME/deduper resets. A
        dropped open review surfaces `STALE_MESSAGE`; nothing is sent."""
        if self._phase == SHELL_EXITED:
            return self._phase
        operation = self._interaction_generation
        if self._disposed or self._paused:
            return self._phase
        if not self._terminal_id:
            raise RuntimeError("take_over with no terminal")
        await self.stop_lease_renewal()
        if not self._operation_current(operation):
            return self._phase
        await self._close_controller()
        if not self._operation_current(operation):
            return self._phase
        try:
            attachment = await self._ops.attach(
                self._host_id,
                self._terminal_id,
                "shell",
                True,
                expected_control_revision=expected_control_revision,
            )
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if expected_control_revision is not None and exc.code == "lease_conflict":
                # The holder changed since it was read: stay a watcher, never force.
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Take over failed ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            return self._phase
        bound: dict[str, Any] = (
            {"expected_generation": attachment.generation}
            if expected_control_revision is not None
            else {}
        )
        try:
            lease = await self._ops.acquire_lease(attachment.attachment_id, "control", **bound)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Could not take control ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._attachment = attachment
        self._lease = lease
        controller = self._spawn_controller(lease)
        if self._automatic:
            controller.fence_ui_input(True)  # input waits for native verification
        self._controller = controller
        self._fresh_controller_state()
        stale = self._sessions.retarget(controller)
        self._notice = stale or ""
        try:
            ticket = await self._ops.terminal_ticket(lease.lease_id)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                await self._release_stale_lease(lease)
                return self._phase
            # Name the failure; the lease stays held so UI resume can re-ticket it.
            diagnostics_logger().info("ticket_failed code=%s", exc.code)
            self._notice = f"Could not stream ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        await controller.start(ticket, "", 0)
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._phase = SHELL_LIVE
        self._reset_bound_renderers()
        self._restart_renewal()
        self._notify()
        return self._phase

    async def _reconnect_flow(self) -> str:
        """Reconnect a disconnected shell (the base already refused when
        exited: use New Terminal). Re-attaches without takeover; a conflict
        owned by this same device self-takeovers (re-owning a released lease
        is not a steal); any other owner lands in `conflict` for an explicit
        decision. Resumes live attachments only: a dead lease fails the
        ticket mint and classifies instead of resurrecting."""
        operation = self._interaction_generation
        if self._disposed or self._paused:
            return self._phase
        if not self._terminal_id:
            raise RuntimeError("reconnect with no terminal")
        await self.stop_lease_renewal()
        if not self._operation_current(operation):
            return self._phase
        await self._close_controller()
        self._notice = ""
        if not self._operation_current(operation):
            return self._phase
        try:
            attachment = await self._ops.attach(self._host_id, self._terminal_id, "shell", False)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if exc.code == "lease_conflict":
                owner = exc.details.get("owner")
                device = owner.get("device_id", "") if isinstance(owner, dict) else ""
                if device and device == self._device_id:
                    if self._automatic:
                        return await self._retake_if_unchanged(operation)
                    phase = await self.take_over()
                    if phase == SHELL_LIVE:
                        self._notice = SELF_TAKEOVER_NOTICE
                        self._notify()
                    return phase
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Reconnect failed ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            return self._phase
        # Fresh attach (no live attachment: closed elsewhere or expired
        # away): stream it as a new live attachment.
        previous = self._attachment
        bound = {"expected_generation": attachment.generation} if self._automatic else {}
        try:
            lease = await self._ops.acquire_lease(attachment.attachment_id, "control", **bound)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                return self._phase
            if exc.code == "lease_conflict":
                self._owner_label = owner_display_from_details(exc.details)
                self._phase = SHELL_CONFLICT
                self._notify()
                return self._phase
            self._notice = f"Could not take control ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._attachment = attachment
        self._lease = lease
        controller = self._spawn_controller(lease)
        if self._automatic:
            controller.fence_ui_input(True)  # input waits for native verification
        self._controller = controller
        self._fresh_controller_state()
        stale = self._sessions.retarget(controller)
        if stale is not None:
            self._notice = stale
        if previous is not None and (
            attachment.attachment_id != previous.attachment_id or attachment.generation == 1
        ):
            # A different terminal, not a continuation: disclose it (wins over a dropped-review
            # notice) and clear the log so old bytes never read as the new terminal's prefix.
            self.output_log.clear()
            self._notice = FRESH_ATTACH_NOTICE
        try:
            ticket = await self._ops.terminal_ticket(lease.lease_id)
        except ShellOpError as exc:
            if not self._operation_current(operation):
                await self._release_stale_lease(lease)
                return self._phase
            # Name the failure; the lease stays held so UI resume can re-ticket it.
            diagnostics_logger().info("ticket_failed code=%s", exc.code)
            self._notice = f"Could not stream ({exc.code})."
            self._phase = SHELL_DISCONNECTED
            self._notify()
            return self._phase
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        await controller.start(ticket, "", 0)
        if not self._operation_current(operation):
            await self._release_stale_lease(lease)
            return self._phase
        self._phase = SHELL_LIVE
        self._reset_bound_renderers()
        self._restart_renewal()
        self._notify()
        return self._phase

    async def _retake_if_unchanged(self, operation: int) -> str:
        """Automatic recovery of this device's own released terminal.

        Only the observed holder revision may be taken; a changed or unknown holder stays a
        conflict, because forced takeover belongs to the explicit user action.
        """
        try:
            state = await self._ops.control_holder(self._host_id, self._terminal_id)
        except ShellOpError:
            state = {}
        revision = state.get("control_revision")
        held, mine = state.get("held"), state.get("mine")
        if not self._operation_current(operation):
            return self._phase
        if not isinstance(held, bool) or not isinstance(mine, bool) or (held and not mine):
            self._phase = SHELL_CONFLICT
            self._notify()
            return self._phase
        if not is_control_revision(revision):
            self._phase = SHELL_CONFLICT
            self._notify()
            return self._phase
        phase = await self.take_over(expected_control_revision=revision)
        if phase == SHELL_LIVE:
            self._notice = SELF_TAKEOVER_NOTICE
            self._notify()
        return phase

    # -- explicit close (terminal exit) ---------------------------------------
    def request_close(self) -> None:
        """Arm the "Close Terminal?" confirm."""
        self._confirm_close = True
        self._notify()

    def cancel_close(self) -> None:
        self._confirm_close = False
        self._notify()

    async def close_terminal(self) -> str:
        """Explicit Close Terminal: close the remote pane,
        drop the dead lease (never reused), stop the stream, enter
        `exited`. One-way: only New Terminal moves on."""
        await self.stop_lease_renewal()
        self._confirm_close = False
        # The session.close command closes the Herdr pane (pane.closed then
        # removes it from the list). It needs no live attachment. One attempt, never retried.
        command = session_close_command(
            command_id=f"cmd-{uuid.uuid4().hex[:16]}",
            host_id=self._host_id,
            host_epoch=self._host_epoch,
            terminal_id=self._terminal_id,
        )
        try:
            await self._ops.close_terminal(command)
        except ShellOpError as exc:
            if exc.code != "not_found":
                self._notice = f"Close failed ({exc.code})."
                self._notify()
                return self._phase
        await self._close_controller()
        self._lease = None
        self._enter_exited(EXIT_MESSAGE)
        return self._phase

    def set_keyboard(self, keyboard: str) -> None:
        """Text/Keys is an independent choice; shells bind no
        composer store, so no reset rule fires and no draft exists."""
        self._sessions.modes.set_keyboard(keyboard)
        self._notify()
