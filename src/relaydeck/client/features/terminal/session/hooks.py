"""Explicit callback contracts for terminal collaborators.

Late-bound callbacks honor session overrides without exposing the session itself."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from relaydeck.client.features.terminal.input_modes import WORKFLOW_SHELL
from relaydeck.client.features.terminal.renewal import lease_deadline_monotonic
from relaydeck.client.features.terminal.session.keys import KeyHooks
from relaydeck.client.features.terminal.session.lease import LeaseHooks
from relaydeck.client.features.terminal.session.paste import PasteHooks
from relaydeck.client.features.terminal.session.rendering import RendererHooks
from relaydeck.client.features.terminal.session.selection import SelectionHooks

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.controller import TerminalController
    from relaydeck.client.features.terminal.session.base import TerminalSessionBase


def renderer_hooks(session: TerminalSessionBase) -> RendererHooks:
    return RendererHooks(
        session_id=id(session),
        controller=lambda: session._controller,
        clear_controller=session._drop_controller,
        foreground=lambda: session._foreground,
        disposed=lambda: session._disposed,
        exited=lambda: session._phase == session._PHASE_EXITED,
        sessions_bound=lambda: session._sessions.bound,
        bind_sessions=lambda controller: session._sessions.bind(controller),
        invalidate_sessions=lambda: session._sessions.invalidate(),
        restart_renewal=lambda: session._restart_renewal(),
        cancel_renewal=session._cancel_renewal_driver,
        bump_interaction=session._bump_interaction,
        lease_trace=lambda: session._lease_trace(),
        notify=lambda: session._notify(),
        mark_disconnected=session._mark_viewer_reopened,
        note_stale_notice=session._note_stale_notice,
        cancel_watch=lambda: session._cancel_background_recovery(),
        bump_epoch=lambda: session._bump_epoch(),
        detach_store=lambda: session._detach_store(),
    )


def lease_hooks(session: TerminalSessionBase) -> LeaseHooks:
    return LeaseHooks(
        session_id=id(session),
        host_id=session._host_id,
        terminal=lambda: session._terminal_label(),
        lease=lambda: session._lease,
        clear_lease=session._drop_lease,
        controller=lambda: session._controller,
        paused=lambda: session._paused,
        disposed=lambda: session._disposed,
        exited=lambda: session._phase == session._PHASE_EXITED,
        close_controller=lambda: session._close_controller(),
        release_remote=lambda lease_id: session._ops.release_lease(lease_id),
        on_terminal=lambda code: session._on_renewal_terminal(code),
    )


def selection_hooks(session: TerminalSessionBase) -> SelectionHooks:
    return SelectionHooks(
        owner=type(session).__name__,
        state=lambda: session._term_state,
        set_state=session._set_term_state,
        term=lambda: session.current_term,
        output_text=lambda: bytes(session.output_log).decode("utf-8", errors="replace"),
        notify=lambda: session._notify(),
        clipboard=lambda: session._clipboard,
    )


def paste_hooks(session: TerminalSessionBase) -> PasteHooks:
    return PasteHooks(
        bound=lambda: session._sessions.bound,
        review=lambda: session._sessions.paste,
        bracketed=lambda: session._bracketed,
        input_token=lambda: session.input_token,
        interaction_generation=lambda: session.interaction_generation,
        clipboard=lambda: session._clipboard,
        notify=lambda: session._notify(),
    )


def key_hooks(session: TerminalSessionBase) -> KeyHooks:
    return KeyHooks(
        paused=lambda: session._paused,
        foreground=lambda: session._foreground,
        bound=lambda: session._sessions.bound,
        epoch=lambda: session._sessions.epoch,
        keys=lambda: session._sessions.keys,
        modes=lambda: session._sessions.modes,
        notify=lambda: session._notify(),
        note_input=lambda: session.note_input(),
        store=lambda: session._composer_store(),
        # Only a session with a composer workflow may route text to a draft.
        allow_draft=session._workflow_kind != WORKFLOW_SHELL,
        drops_engine_keys=lambda: session._drops_engine_keys(),
    )


__all__ = ["key_hooks", "lease_hooks", "paste_hooks", "renderer_hooks", "selection_hooks"]


async def reset_history(session: TerminalSessionBase, controller: TerminalController) -> bool:
    generation = session._interaction_generation
    lease = session._lease
    socket = controller._client._socket

    def current() -> bool:
        if lease is None:
            return False
        driver = session._renewal
        deadline = (
            driver.deadline
            if driver is not None and driver.lease_id == lease.lease_id
            else lease_deadline_monotonic(lease.expires_at)
        )
        return (
            session._controller is controller
            and controller._client._socket is socket
            and session._lease is lease
            and session._foreground
            and not session._paused
            and session._operation_current(generation)
            and not controller.writer_revoked
            and not (driver is not None and driver.invalidated)
            and lease.lease_id == controller.lease_id
            and lease.generation == controller.generation
            and lease.mode == controller.mode
            and deadline is not None
            and deadline > time.monotonic()
        )

    if not current():
        return False
    if not await session._renderer.reset_history(current) or not current():
        return False
    session.output_log.clear()
    return True
