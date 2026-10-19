"""Observe an existing attachment; never attach, take control, resize or replay input."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from relaydeck.client.features.shell.ops import AttachmentInfo, ShellOpError
from relaydeck.client.features.terminal.models import TerminalViewState
from relaydeck.client.features.terminal.stream import EMULATOR_PRESERVED


def fence_hidden_writer(session: Any) -> None:
    if session.controller is not None:
        session.controller.pause_writer()
    session.sessions.invalidate()
    store = getattr(session, "store", None)
    if store is not None:
        store.reconnect("fence_hidden")
        store.unbind_writer("fence_hidden")


async def watch_only(
    session: Any,
    guard: Callable[[], bool],
    attachment: AttachmentInfo | None = None,
    *,
    preserved_state: TerminalViewState | None = None,
) -> bool:
    """Release control before minting a view lease for a known attachment.

    Preserve the native emulator and applied cursor across the lease change.
    Any stale/failed response releases its lease; uncertain input is never sent.
    """
    attachment = attachment or session.attachment
    if attachment is None or session.disposed or session.paused or session.phase == "exited":
        return False
    operation = session.interaction_generation
    revision = getattr(session, "_watch_recovery_revision", None)
    original_attachment = session.attachment

    def current() -> bool:
        return (
            guard()
            and session._operation_current(operation)
            and getattr(session, "_watch_recovery_revision", None) == revision
            and (session.attachment is original_attachment or session.attachment is attachment)
        )

    if not current():
        return False
    if session.lease is not None and session.lease.mode == "view" and session.phase == "live":
        return True
    fence_hidden_writer(session)
    previous = session.controller
    # A failed acquisition can leave no controller. Keep the original applied
    # cursor for the next bounded watch retry, without replaying output/input.
    previous_state = preserved_state or session.term_state
    epoch, offset = previous_state.epoch, previous_state.applied_offset
    # A cancelled/failed earlier conversion leaves no controller but keeps the
    # mounted emulator and its applied cursor: resuming from offset 0 would
    # render the retained output a second time.
    had_viewer = session.current_term is not None and (
        previous is not None or preserved_state is not None or bool(previous_state.epoch)
    )
    old_lease = session.lease
    session._controller = None
    session._lease = None
    await session.stop_lease_renewal()
    lease = None
    success = False
    try:
        if previous is not None:
            try:
                await previous.close()
            except Exception:
                pass  # Release still closes the old server-side socket/lease.
        if old_lease is not None:
            await session._ops.release_lease(old_lease.lease_id)
        if not current():
            return False
        lease = await session._ops.acquire_lease(attachment.attachment_id, "view")
        if (
            lease.mode != "view"
            or lease.host_id != attachment.host_id
            or lease.terminal_id != attachment.terminal_id
            or lease.attachment_id != attachment.attachment_id
            or lease.generation != attachment.generation
            or lease.epoch != attachment.epoch
        ):
            raise ShellOpError("stale_revision", "Attachment changed")
        if not current():
            return False
        ticket = await session._ops.terminal_ticket(lease.lease_id)
        if not current():
            return False
        session._attachment, session._lease = attachment, lease
        controller = session._spawn_controller(lease)
        if had_viewer:
            controller.emulator = EMULATOR_PRESERVED
            controller.state = replace(previous_state, lease_mode="view")
        session._controller = controller
        session._term_state = replace(previous_state, lease_mode="view")
        await controller.start(ticket, epoch if had_viewer else "", offset if had_viewer else 0)
        if not current():
            return False
        session._phase = "live"
        session._notice = ""
        if hasattr(session, "_disclosure_armed"):
            session._disclosure_armed = False
        if session.foreground:
            session.sessions.bind(controller)
        session._restart_renewal()
        session._notify()
        success = True
        return True
    except Exception:
        if current():
            session._phase = "disconnected"
            session._notice = "Watch unavailable; reconnect or take over explicitly."
            session._notify()
        return False
    finally:
        if lease is not None and not success:
            await session._release_stale_lease(lease)
