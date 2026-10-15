"""Selection, keyboard-request and mobile key-row actions for the detail screens."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakKeyDictionary

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_KEY, ORIGIN_TEXT

from relaydeck.client.features.terminal.input_modes import (
    ROUTE_DROPPED,
    ROUTE_PTY,
    WORKFLOW_AGENT_DIRECT,
)
from relaydeck.client.features.terminal.key_bar import KeyBarItem, ModifierLatch
from relaydeck.client.features.terminal.keys import (
    MOD_ALT,
    MOD_CTRL,
    KeyInputSession,
    encode_key,
)
from relaydeck.client.features.terminal.scrolling import (
    last_lines,
    record_scroll_input,
    reset_alt_scroll,
    view_state,
)
from relaydeck.client.features.terminal.selection_model import (
    copy_text,
    resolved_range,
    set_selection_error,
)

if TYPE_CHECKING:
    from relaydeck.client.features.shell.session import ShellSession
    from relaydeck.client.features.terminal.agent_session import AgentSession


_selection_requests: WeakKeyDictionary[AgentSession | ShellSession, int] = WeakKeyDictionary()


def selection_actions(
    session: AgentSession | ShellSession,
    *,
    phone: bool = False,
) -> tuple[Callable[[], Awaitable[None]], Callable[[int, int], None], Callable[[], None]]:
    """Fence selection fetch/commit and callbacks to this viewer and frozen sheet."""
    controller = session.controller
    controller_generation = controller.generation if controller is not None else None
    term = session.current_term
    viewer_generation = getattr(term, "viewer_generation", None)
    # The bridge increments this monotonic counter on every native reset,
    # even when the channel generation, epoch and selection stay unchanged.
    # Bind to the existing counter without changing the bridge's API.
    reset_revision = getattr(term, "_reset_count", None)
    view_token = session._view_token
    interaction = session.interaction_generation
    epoch = session.term_state.epoch
    expected_selection = session.term_state.selection
    alt = view_state(term).alt

    def live() -> bool:
        return (
            session.foreground
            and not session.paused
            and not session.disposed
            and interaction == session.interaction_generation
            and controller is session.controller
            and (controller is None or controller.generation == controller_generation)
            and term is session.current_term
            and view_token == session._view_token
            and viewer_generation == getattr(term, "viewer_generation", None)
            and reset_revision == getattr(term, "_reset_count", None)
            and epoch == session.term_state.epoch
            and expected_selection is session.term_state.selection
            and (not phone or view_state(term).alt == alt)
        )

    def invalidate_request() -> int:
        serial = _selection_requests.get(session, 0) + 1
        _selection_requests[session] = serial
        return serial

    def mirror() -> None:
        if live() and controller is not None:
            # Only UI selection changes here. Preserve current stream status,
            # epoch/offset, lease and notices; resets retain their normal policy.
            controller.state = replace(controller.state, selection=session.term_state.selection)

    async def open_snapshot() -> None:
        nonlocal expected_selection
        if not live():
            return
        request = invalidate_request()
        # The session's legacy async opener commits after its await. Fetch
        # independently so no stale result/error can mutate a replacement.
        snapshot: str | None = None
        failure: Exception | None = None
        fetch = getattr(term, "get_snapshot_text", None)
        if callable(fetch):
            try:
                snapshot = await fetch()
            except Exception as exc:  # noqa: BLE001 - preserve visible fallback
                failure = exc
        if not live() or request != _selection_requests.get(session):
            return
        # No await from this check through the selection-only commit/mirror.
        if phone:
            snapshot = (
                snapshot or ""
                if alt
                else last_lines(
                    snapshot if snapshot is not None else session.selection_sheet_text, 5000
                )
            )
        session._selection_snapshot = snapshot
        session.open_selection()
        if phone:
            # A sheet starts on the newest text, with no accidental full selection.
            end = len(session.selection_sheet_text)
            session.move_selection(end, end)
        expected_selection = session.term_state.selection
        if failure is not None:
            detail = str(failure) or type(failure).__name__
            reason = "current screen could not be read" if phone and alt else "showing raw output"
            session._term_state = set_selection_error(
                session.term_state, f"Live snapshot unavailable; {reason}: {detail}"
            )
            expected_selection = session.term_state.selection
            session._notify()
        mirror()

    def move(anchor: int, focus: int) -> None:
        nonlocal expected_selection
        if not live() or not expected_selection.open:
            return
        session.move_selection(anchor, focus)
        expected_selection = session.term_state.selection
        mirror()

    def close() -> None:
        nonlocal expected_selection
        if not live():
            return
        invalidate_request()
        session.close_selection()
        expected_selection = session.term_state.selection
        mirror()

    return open_snapshot, move, close


def phone_selection_actions(
    session: AgentSession | ShellSession, text: str
) -> tuple[Any, Callable[[str], Awaitable[str | None]], Callable[[], bool]]:
    """Copy a frozen phone document without writing session state after awaits."""

    def boundary() -> tuple[Any, ...]:
        controller, term = session.controller, session.current_term
        return (
            controller,
            controller.generation if controller is not None else None,
            term,
            getattr(term, "viewer_generation", None),
            getattr(term, "_reset_count", None),
            session._view_token,
            session.interaction_generation,
            session.term_state.epoch,
            _selection_requests.get(session, 0),
            session._selection_snapshot,
        )

    expected = boundary()

    def live() -> bool:
        return (
            session.foreground
            and not session.paused
            and not session.disposed
            and session.term_state.selection.open
            and boundary() == expected
        )

    async def copy(mode: str) -> str | None:
        if not live():
            return None
        if mode == "all":
            value = text
        elif mode == "last-100":
            # Native snapshots join logical lines with LF. Preserve CRLF,
            # trailing newlines and Unicode separators inside a logical line.
            value = last_lines(text, 100)
        elif mode == "selection":
            value = resolved_range(session.term_state, text).extract(text)
        else:
            # In particular, logical-line snapshots cannot reconstruct the
            # viewport. Never silently substitute a guessed tail for it.
            return None
        if session._clipboard is None:
            raise RuntimeError("No clipboard service is available")
        written = await copy_text(value, session._clipboard)
        return written if live() else None

    return expected, copy, live


def request_terminal_keyboard(
    session: Any, token: tuple[int, int] | None, *, agent_direct: bool = False
) -> None:
    """Explicit focus request fenced by the same live writer as terminal input."""
    if (
        token is None
        or token != session.input_token
        or not session.foreground
        or session.paused
        or session.disposed
        or not session.keys_enabled
        or session.term_state.selection.open
        or (agent_direct and session.view().workflow != WORKFLOW_AGENT_DIRECT)
    ):
        return
    term = session.current_term
    if term is not None:
        term.set_input_enabled(True)
        term.request_keyboard()


class DirectInput(Protocol):
    async def __call__(
        self,
        origin: int,
        data: bytes | tuple[bytes, ...],
        *,
        can_input: Callable[[], bool] | None = None,
        paste_text: str | None = None,
    ) -> str: ...


class MobileInput(Protocol):
    async def __call__(
        self,
        origin: int,
        data: bytes | tuple[bytes, ...],
        *,
        direct_only: bool = False,
        can_input: Callable[[], bool] | None = None,
        paste_text: str | None = None,
        terminal_scroll: bool = False,
    ) -> str: ...


def use_mobile_key_actions(
    session: AgentSession | ShellSession, *, active: bool = True
) -> tuple[Callable[[KeyBarItem, bool], Any], dict[str, str], MobileInput]:
    """One mobile modifier owner for chips and text; keys only projects its armed set."""
    latch_ref: Any = ft.use_ref(None)
    token_ref: Any = ft.use_ref(None)
    lock_ref: Any = ft.use_ref(lambda: asyncio.Lock())
    _tick, set_tick = ft.use_state(0)
    token = session.input_token
    binding = (token, active)
    if latch_ref.current is None or token_ref.current != binding:
        latch_ref.current = ModifierLatch()
        token_ref.current = binding
        # When entering mobile, adopt existing one-shots from the desktop dock.
        if active and token is not None and session.sessions.bound:
            for mod in session.sessions.keys.pending_modifiers & {MOD_CTRL, MOD_ALT}:
                latch_ref.current.tap(mod, int(time.monotonic() * 1000))
    latch = latch_ref.current

    def live(*, writer: bool = True) -> bool:
        return (
            token is not None
            and token == session.input_token
            and binding == token_ref.current
            and latch is latch_ref.current
            and session.foreground
            and not session.paused
            and not session.disposed
            and not session.term_state.selection.open
            and (not writer or session.keys_enabled)
        )

    def arm() -> None:
        if live():
            for mod in (MOD_CTRL, MOD_ALT):
                session.sessions.keys.set_modifier_armed(mod, mod in latch.armed())

    def settled(keys: KeyInputSession, before: frozenset[str]) -> None:
        if not live() or keys is not session.sessions.keys:
            return
        # Observe the existing encoder's consumption, never re-encode packets.
        # Multi-character/unencodable IME fall-through leaves modifiers armed.
        if before - keys.pending_modifiers:
            latch.consume()
        arm()  # only locked modifiers survive successful consumption
        set_tick(lambda current: current + 1)

    async def press(item: KeyBarItem, long_press: bool = False) -> None:
        async with lock_ref.current:
            if not active or not live():
                return
            if item.kind == "modifier":
                latch.tap(next(iter(item.mods)), int(time.monotonic() * 1000))
                arm()
                set_tick(lambda current: current + 1)
            elif item.kind == "key":
                key, mods = (
                    item.long_press if long_press and item.long_press else (item.key, item.mods)
                )
                if key is None:
                    return
                arm()
                keys = session.sessions.keys
                before = keys.pending_modifiers
                try:
                    sent = await session.press_key(key, physical_modifiers=mods)
                    if sent:
                        if key in ("page-up", "page-down", "enter", "return"):
                            record_scroll_input(session, ORIGIN_KEY, encode_key(key) or b"")
                        elif len(key) == 1:
                            reset_alt_scroll(session)
                finally:
                    settled(keys, before)

    def direct_allowed() -> bool:
        return session.keys_enabled and not session.sessions.modes.text_edits_draft

    async def input(
        origin: int,
        data: bytes | tuple[bytes, ...],
        *,
        direct_only: bool = False,
        can_input: Callable[[], bool] | None = None,
        paste_text: str | None = None,
        terminal_scroll: bool = False,
    ) -> str:
        payloads = (data,) if isinstance(data, bytes) else data

        def allowed() -> bool:
            return (
                (can_input is None or can_input())
                and binding == token_ref.current
                and (not active or live(writer=False))
                and (not direct_only or direct_allowed())
                and (not terminal_scroll or live())
            )

        # One FIFO writer transaction per native change. Keep packet boundaries
        # so one-shot modifiers consume exactly as they did for individual keys.
        async with lock_ref.current:
            if paste_text is not None:
                if not allowed():
                    return ROUTE_DROPPED
                return await session.request_paste(paste_text, require_review=True)
            result = ROUTE_DROPPED
            for payload in payloads:
                if not allowed():
                    return ROUTE_DROPPED  # Drop the remaining delta; never replay.
                if terminal_scroll:
                    # Only the scroll router uses this escape hatch, with its
                    # strict packet whitelist and native/lease boundary guard.
                    assert token is not None
                    keys = session.sessions.keys
                    emission = keys.commit_bytes(payload, token=token[1])
                    result = (
                        ROUTE_PTY if emission is not None and await keys.flush() else ROUTE_DROPPED
                    )
                elif not active or origin != ORIGIN_TEXT:
                    result = await session.handle_view_input(origin, payload, token=token)
                else:
                    arm()
                    keys = session.sessions.keys
                    before = keys.pending_modifiers
                    try:
                        result = await session.handle_view_input(origin, payload, token=token)
                    finally:
                        settled(keys, before)
                if result == ROUTE_DROPPED:
                    return result
                if result == ROUTE_PTY:
                    record_scroll_input(session, origin, payload, scroll=terminal_scroll)
            return result

    return press, {mod: latch.state(mod) for mod in (MOD_CTRL, MOD_ALT)}, input
