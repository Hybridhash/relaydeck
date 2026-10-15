"""Direct typing on touch devices: the keyboard hook, paste fallback and the hidden typing field."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_TEXT

from relaydeck.client.adapters.clipboard import ClipboardReadStatus
from relaydeck.client.features.terminal.events import (
    event_text,
    selection_change,
    spawn,
)
from relaydeck.client.features.terminal.input_modes import (
    ROUTE_DROPPED,
    ROUTE_PTY,
)
from relaydeck.client.features.terminal.paste_policy import contains_only_safe_scalars
from relaydeck.client.features.terminal.paste_review import (
    REQUEST_REJECTED,
    REQUEST_REVIEW,
)
from relaydeck.client.theme import palette

if TYPE_CHECKING:
    pass

from relaydeck.client.features.terminal.surfaces.actions import (
    DirectInput,
)

DIRECT_SENTINEL = "  "


def direct_typing_allowed(
    session: Any, token: Any, interaction: int, *, direct_only: bool = True
) -> bool:
    return (
        token is not None
        and token == session.input_token
        and interaction == session.interaction_generation
        and session.foreground
        and not session.paused
        and not session.disposed
        and session.keys_enabled
        and not session.term_state.selection.open
        and session.pending_review is None
        and (not direct_only or not session.sessions.modes.text_edits_draft)
    )


def _typing_now() -> float:
    return time.monotonic()


def use_direct_keyboard(
    *,
    opened: bool,
    enabled: bool,
    binding: Any,
    on_open: Callable[[], None],
    on_close: Callable[[], None],
    can_open: Callable[[], bool] | None = None,
) -> tuple[Any, int, Any, Callable[[], None], Callable[[], None], Any, bool]:
    """One source of open/close intent; duplicate click events are suppressed."""
    field: Any = ft.use_ref(lambda: ft.Ref[ft.TextField]())
    requests, set_requests = ft.use_state(0)
    handled: Any = ft.use_ref(0)
    opened_ref = ft.use_ref(opened)
    live_ref: Any = ft.use_ref((binding, enabled))
    last: Any = ft.use_ref(float("-inf"))
    closing, set_closing = ft.use_state(False)
    close_serial: Any = ft.use_ref(0)
    previous_binding: Any = ft.use_ref(binding)
    opened_ref.current = opened
    live_ref.current = (binding, enabled)

    def close_unavailable() -> None:
        if not enabled or previous_binding.current != binding:
            close_serial.current += 1
            opened_ref.current = False
            if opened and not enabled:
                on_close()
            set_closing(False)
        previous_binding.current = binding

    ft.use_effect(close_unavailable, dependencies=[enabled, binding])

    def cancel_close() -> None:
        close_serial.current += 1

    ft.use_effect(lambda: None, dependencies=[], cleanup=cancel_close)

    def open_keyboard() -> None:
        if (
            not enabled
            or live_ref.current != (binding, True)
            or (can_open is not None and not can_open())
        ):
            return
        opened_ref.current = True
        close_serial.current += 1
        set_closing(False)
        on_open()
        set_requests(lambda current: current + 1)

    def close_keyboard(after: Callable[[], None] | None = None) -> None:
        if not opened_ref.current:
            if after is not None:
                after()
            return
        opened_ref.current = False
        close_serial.current += 1
        serial = close_serial.current
        set_closing(True)

        async def finish_close() -> None:
            # Separate carrier updates: the still-mounted TextField first
            # receives can_request_focus=False/disabled=True. Flet 1.0.3 has
            # no blur or client-frame acknowledgement; allow a frame before
            # hiding it. Do not replay this close across a changed binding.
            await asyncio.sleep(0.05)
            if serial != close_serial.current or live_ref.current[0] != binding:
                return
            on_close()
            if after is not None:
                after()
            set_closing(False)

        spawn(finish_close(), label="Close Direct typing focus")

    def toggle() -> None:
        if (
            not enabled
            or live_ref.current != (binding, True)
            or (can_open is not None and not can_open())
        ):
            return
        now = _typing_now()
        if now - last.current < 0.3:
            return
        last.current = now
        if opened_ref.current:
            close_keyboard()
        else:
            open_keyboard()

    return field.current, requests, handled, open_keyboard, toggle, close_keyboard, closing


PASTE_FALLBACK_HINT = "Long-press here and choose Paste"


def use_paste_fallback(
    on_paste: Callable[[], Any] | None,
    open_keyboard: Callable[[], None],
    binding: Any,
    can_open: Callable[[], bool] | None,
) -> tuple[Any, bool, Callable[[], None]]:
    hint, set_hint = ft.use_state(False)
    current = ft.use_ref(binding)
    current.current = binding

    def clear() -> None:
        set_hint(False)

    ft.use_effect(clear, dependencies=[binding])

    async def paste() -> Any:
        if on_paste is None:
            return None
        result = on_paste()
        if inspect.isawaitable(result):
            result = await result
        if current.current == binding and (can_open is None or can_open()):
            set_hint(result == ClipboardReadStatus.UNAVAILABLE)
            if result == ClipboardReadStatus.UNAVAILABLE:
                open_keyboard()
        return result

    return paste if on_paste else None, hint, clear


def _typing_digests(value: str) -> tuple[bytes, ...]:
    """Transient prefix metadata for coalesced edits; retain no entered text."""
    digest = hashlib.sha256()
    prefixes = [digest.digest()]
    for char in value:
        digest.update(char.encode("utf-8", errors="surrogatepass"))
        prefixes.append(digest.digest())
    return tuple(prefixes)


def _ignore_tap(_event: Any) -> None:
    """Flet needs one GestureDetector handler; watching taps do nothing."""


@ft.component
def DirectTypingField(
    *,
    on_input: Callable[[int, bytes], Awaitable[str]],
    visible: bool,
    enabled: bool,
    binding: Any,
    field_ref: Any,
    focus_requests: int,
    dark: bool,
    focus_handled: Any,
    field_key: str,
    can_input: Callable[[], bool] | None = None,
    on_transaction: DirectInput | None = None,
    paste_fallback: bool = False,
    on_input_handled: Callable[[], None] | None = None,
) -> Any:
    """Controlled sentinel field; only explicit gestures request keyboard focus."""
    revision, set_revision = ft.use_state(0)
    live_ref: Any = ft.use_ref((binding, visible, enabled, paste_fallback))
    capture_value = "" if paste_fallback else DIRECT_SENTINEL
    pending: Any = ft.use_ref(_typing_digests(capture_value))
    unsent_editor: Any = ft.use_ref(DIRECT_SENTINEL)
    waiting = ft.use_ref(False)
    review_waiting = ft.use_ref(False)
    alive = ft.use_ref(True)
    fallback_lock: Any = ft.use_ref(lambda: asyncio.Lock())
    expected = (binding, visible, enabled, paste_fallback)
    field = field_ref.current
    if (
        field is not None
        and not live_ref.current[3]
        and not waiting.current
        and type(field.value) is str
        and _typing_digests(field.value) != pending.current
    ):
        # A native prop patch without a committed change event is editor state.
        # Values already processed by change() are never retained or replayed.
        unsent_editor.current = field.value
    if live_ref.current != expected:
        pending.current = _typing_digests(capture_value)
        waiting.current = False
        review_waiting.current = False
    live_ref.current = expected

    def live() -> bool:
        return (
            bool(alive.current)
            and visible
            and enabled
            and live_ref.current == expected
            and (can_input is None or can_input())
        )

    def cleanup() -> None:
        alive.current = False
        unsent_editor.current = DIRECT_SENTINEL

    ft.use_effect(lambda: None, dependencies=[], cleanup=cleanup)

    async def focus() -> None:
        if live() and field_ref.current is not None:
            try:
                await asyncio.wait_for(field_ref.current.focus(), 1)
            except (TimeoutError, RuntimeError):
                pass  # Best effort; tapping the actual field is the native gesture.

    def request_focus() -> None:
        if focus_requests > focus_handled.current:
            # Consume intent even if its generation has died; never replay
            # an old focus request on resume or a breakpoint remount.
            focus_handled.current = focus_requests
            if live():
                spawn(focus(), label="Direct typing focus")

    ft.use_effect(request_focus, dependencies=[focus_requests])

    async def send(payloads: tuple[bytes, ...], *, paste_text: str | None = None) -> str:
        if on_transaction is not None:
            return await on_transaction(
                ORIGIN_TEXT, payloads, can_input=live, paste_text=paste_text
            )
        if paste_text is not None:
            return ROUTE_DROPPED  # No review port: never fall back to writing.
        # Presentation-only callers retain the scalar callback contract. Actual
        # agent/shell mounts supply the transaction on their shared writer lock.
        async with fallback_lock.current:
            result = ROUTE_DROPPED
            for data in payloads:
                if not live():
                    return ROUTE_DROPPED
                result = await on_input(ORIGIN_TEXT, data)
                if result == ROUTE_DROPPED:
                    return result
            return result

    async def change(event: Any) -> None:
        if not live() or review_waiting.current:
            return
        value = event_text(event)
        if not paste_fallback:
            unsent_editor.current = DIRECT_SENTINEL
        old = pending.current
        size = len(old) - 1
        prefixes = _typing_digests(value)
        # Replacement is deletion of the old suffix followed by insertion of
        # the new one, including any replaced sentinel characters. The same
        # diff handles cumulative edits before native reset is acknowledged.
        common = 0
        while common < min(size, len(value)) and prefixes[common + 1] == old[common + 1]:
            common += 1
        inserted = value[common:]
        if paste_fallback:
            # A native select-all replacement can start with spaces identical
            # to the sentinel. Explicit capture uses an empty field and takes
            # the whole value verbatim, never a guessed common-prefix suffix.
            size = common = 0
            inserted = value
            if not inserted:
                return
        review = bool(inserted) and (
            paste_fallback
            or not contains_only_safe_scalars(inserted)
            or "\t" in inserted
            or (inserted not in ("\n", "\r") and ("\n" in inserted or "\r" in inserted))
        )
        payloads = [b"\x7f"] * (size - common)
        if inserted and not review:
            # This field's Return can coalesce with typed characters. Keep
            # terminal CR here; reviewed paste and the shared encoder retain
            # their own exact-text policy.
            payloads.append(inserted.replace("\n", "\r").encode())
        pending.current = prefixes
        waiting.current = True
        set_revision(lambda current: current + 1)
        if on_input_handled and not paste_fallback:
            on_input_handled()
        if review:
            review_waiting.current = True
            # Replacement DELs are also withheld; review/rejection authorizes
            # no terminal input before the existing explicit paste decision.
            result = await send((), paste_text=inserted)
            if result == REQUEST_REJECTED and live():
                pending.current = _typing_digests(capture_value)
                waiting.current = False
                review_waiting.current = False
            if (
                paste_fallback
                and result in (REQUEST_REVIEW, REQUEST_REJECTED)
                and alive.current
                and live_ref.current == expected
                and on_input_handled
            ):
                # Accepted local review/rejection completes without another
                # await. Its result consumes this intent even though an open
                # review deliberately makes can_input() false. Stale/blocked
                # results cannot clear a replacement capture's presentation.
                on_input_handled()
        else:
            await send(tuple(payloads))

    def acknowledge_reset(base: int, extent: int) -> None:
        if (
            live()
            and waiting.current
            and not review_waiting.current
            and base == extent == len(capture_value)
        ):
            pending.current = _typing_digests(capture_value)
            waiting.current = False
            review_waiting.current = False

    async def submit(event: Any) -> None:
        del event
        if not live() or review_waiting.current or paste_fallback:
            return
        unsent_editor.current = capture_value
        pending.current = _typing_digests(DIRECT_SENTINEL)
        waiting.current = False
        set_revision(lambda current: current + 1)
        if await send((b"\r",)) == ROUTE_PTY:
            await focus()

    tokens = palette(dark)
    # Explicit paste capture always starts empty and never replaces the parked
    # Direct editor. Its reviewed text is held by the existing review model.
    editor_value = "" if paste_fallback else unsent_editor.current
    editor_end = len(editor_value.encode("utf-16-le", errors="surrogatepass")) // 2
    _ = revision
    return ft.TextField(
        key=field_key,
        ref=field_ref,
        visible=visible,
        height=36,
        value=editor_value,
        selection=ft.TextSelection(base_offset=editor_end, extent_offset=editor_end),
        hint_text="Type to terminal",
        prefix="Type to terminal  ",
        tooltip="Type directly to the terminal",
        text_size=15,
        # Multiline editing avoids Flutter's single-line
        # submit/unfocus path. Finite height still shows one terminal line.
        multiline=True,
        fit_parent_size=True,
        # Pinned Flet.focus() targets its normal node; shift_enter would
        # attach a different node. Multiline Return arrives as a text change.
        shift_enter=False,
        min_lines=None,
        max_lines=None,
        autofocus=False,
        can_request_focus=visible and enabled,
        disabled=not (visible and enabled),
        autocorrect=False,
        enable_suggestions=False,
        capitalization=ft.TextCapitalization.NONE,
        smart_dashes_type=False,
        smart_quotes_type=False,
        # Clipboard.getData may be unavailable on iOS web. Explicit fallback
        # needs the native long-press/Paste menu; its change still enters review.
        enable_interactive_selection=paste_fallback,
        color=tokens["text"],
        cursor_color=tokens["accent_text"],
        filled=True,
        fill_color=tokens["raised"],
        content_padding=8,
        size_constraints=ft.BoxConstraints(min_height=36),
        on_change=change,
        on_submit=submit,
        on_selection_change=selection_change(acknowledge_reset),
    )
