"""Direct typing on touch devices: the keyboard hook, paste fallback and the hidden typing field."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

import flet as ft
from relaydeck_terminal.protocol import ORIGIN_TEXT

from relaydeck.client.adapters.clipboard import ClipboardReadStatus
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.events import event_text, selection_change, spawn
from relaydeck.client.features.terminal.input_modes import ROUTE_DROPPED, ROUTE_PTY
from relaydeck.client.features.terminal.paste_policy import contains_only_safe_scalars
from relaydeck.client.features.terminal.paste_review import REQUEST_REJECTED, REQUEST_REVIEW
from relaydeck.client.features.terminal.surfaces.actions import DirectInput
from relaydeck.client.features.terminal.surfaces.direct_typing_state import (
    _Authority,
    _DirectCallbacks,
    _ParkedEditor,
)
from relaydeck.client.flet_invokes import focus_control
from relaydeck.client.theme import palette

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


@dataclass
class _FocusCounter:
    current: int = 0
    requested: int = 0
    authority: _Authority | None = None


def use_direct_keyboard(
    *,
    opened: bool,
    enabled: bool,
    binding: Any,
    on_open: Callable[[], None],
    on_close: Callable[[], None],
    can_open: Callable[[], bool] | None = None,
) -> tuple[Any, int, Any, Callable[[], None], Callable[[], None], Any, bool]:
    field: Any = ft.use_ref(lambda: ft.Ref[ft.TextField]())
    requests, set_requests = ft.use_state(0)
    handled: Any = cast(Any, ft.use_ref(lambda: _FocusCounter())).current
    opened_ref = ft.use_ref(opened)
    live_ref: Any = ft.use_ref((binding, enabled))
    last: Any = ft.use_ref(float("-inf"))
    closing, set_closing = ft.use_state(False)
    close_serial: Any = ft.use_ref(0)
    previous_binding: Any = ft.use_ref(binding)
    opened_ref.current, live_ref.current = opened, (binding, enabled)

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
        handled.authority, handled.requested = _Authority.read(binding), handled.requested + 1
        set_requests(handled.requested)

    def close_keyboard(after: Callable[[], None] | None = None) -> None:
        handled.current = handled.requested
        if not opened_ref.current:
            if after is not None:
                after()
            return
        opened_ref.current = False
        close_serial.current += 1
        serial = close_serial.current
        set_closing(True)

        async def finish_close() -> None:
            # Disable focus for a carrier frame before hiding; Flet 1.0.3 has no blur/ack.
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
    on_unavailable: Callable[[], None] | None = None,
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
        if result == ClipboardReadStatus.UNAVAILABLE and on_unavailable is not None:
            on_unavailable()  # a paste panel takes the platform paste instead of the field
            return result
        if current.current == binding and (can_open is None or can_open()):
            set_hint(result == ClipboardReadStatus.UNAVAILABLE)
            if result == ClipboardReadStatus.UNAVAILABLE:
                open_keyboard()
        return result

    return paste if on_paste else None, hint, clear


DIRECT_EDIT_CAP = 4096


@dataclass
class _ModifierGuard:
    current: Callable[[], bool]
    modifier_used: Callable[[], None]

    def __call__(self) -> bool:
        return self.current()


@dataclass
class _DirectEditor:
    """Transient code-point diff state, not grapheme erasure;
    cap clearing waits for an explicit boundary.
    """

    value: str = DIRECT_SENTINEL
    unsent: int = len(DIRECT_SENTINEL)
    protected: tuple[bool, ...] | None = None
    edit: int = 0
    boundary: int = 0
    reset_epoch: int = 0
    reset_revision: tuple[int, int] | None = None
    reported_reset: tuple[int, int] | None = None
    waiting: bool = False
    review_waiting: bool = False
    return_epoch: int | None = None
    refocus_epoch: int | None = None
    focus_epoch: int | None = None
    focus_request: tuple[int, Any] | None = None
    cap_pending: bool = False
    writable_fence: bool = False
    rebased: bool = False
    parked: _ParkedEditor | None = None
    presented: str | None = None
    presented_selection: ft.TextSelection | None = None
    inflight: int = 0
    native: Any = None
    context: Any = None
    authority: _Authority | None = None

    def reset(self, capture: str) -> None:
        self.value, self.unsent = capture, len(capture)
        self.protected = None
        self.edit += 1
        self.boundary += 1
        self.reset_epoch += 1
        self.reset_revision = (self.boundary, self.edit)
        self.waiting = self.review_waiting = self.cap_pending = False
        self.return_epoch = self.refocus_epoch = self.focus_epoch = None
        self.parked = None
        self.rebased = self.writable_fence = False

    def capture_native(self, field: Any) -> None:
        if (
            field is None
            or self.waiting
            or self.reset_revision is not None
            or self.review_waiting
            or self.inflight
            or type(field.value) is not str
        ):
            return
        baseline = field.value == self.value
        if baseline:
            self.parked = None
            if not self.rebased:
                return
        if (
            not (baseline and self.rebased)
            and field.value == self.presented
            and (self.parked is None or field.selection == self.presented_selection)
        ):
            return
        selection = field.selection
        if selection is None or selection == self.presented_selection:
            end = len(field.value.encode("utf-16-le", errors="surrogatepass")) // 2
            selection = ft.TextSelection(base_offset=end, extent_offset=end)
        self.parked = _ParkedEditor(field.value, replace(selection))

    def cancel_focus(self, *, latest: bool = False, pending: bool = True) -> None:
        self.focus_epoch = self.refocus_epoch = None
        if pending and self.focus_request is not None:
            requests, handled = self.focus_request
            if latest:
                requests = getattr(handled, "requested", requests)
            handled.current = max(requests, handled.current)

    def leave(self, field: Any, capture: str, *, latest: bool = False, cancel: bool = True) -> None:
        self.cancel_focus(latest=latest, pending=cancel)
        if capture == DIRECT_SENTINEL:
            self.capture_native(field)
        if self.parked is None or self.cap_pending:
            self.reset(capture)
        elif self.rebased:
            self.rebase(retire=False)
        else:
            self.boundary += 1
            self.edit += 1
            self.reset_revision = None
            self.waiting = False
            self.return_epoch = None

    def rebase(self, *, retire: bool) -> None:
        """Protected restored characters must never contribute PTY replay or DELs."""
        assert self.parked is not None  # noqa: S101
        self.value = self.parked.value
        self.unsent = len(self.value)
        self.protected = (True,) * len(self.value)
        self.rebased = True
        self.boundary += 1
        self.edit += 1
        self.reset_epoch += int(retire)
        self.writable_fence = not retire
        self.reset_revision = None
        self.waiting = self.review_waiting = False
        self.return_epoch = None
        self.cancel_focus()
        self.cap_pending = len(self.value) >= DIRECT_EDIT_CAP

    def reconcile(self, field: Any, current: Any, capture: str, *, available: bool) -> None:
        if field is not None:
            self.native = field
        previous = self.context or current
        authority = _Authority.read(current[0])
        changed = self.authority is not None and authority != self.authority
        navigation = authority is not None and authority.navigation_from(self.authority)
        same_owner = (
            authority is not None
            and self.authority is not None
            and authority.owner == self.authority.owner
        )
        explicit = previous[3:] != current[3:]
        request_only = previous[:4] == current[:4] and not changed
        handoff = (
            explicit and request_only and available and all(current[1:3])
            and self.reset_epoch in (self.focus_epoch, self.refocus_epoch)
        )  # fmt: skip
        blocked = not available and bool(authority and authority.foreground) and all(previous[1:3])
        hard = explicit or blocked or (not navigation and (changed or previous[0] != current[0]))
        if hard and (blocked or not request_only) and all(previous[1:3]):
            self.cancel_focus(latest=blocked or changed or previous[0] != current[0])
        if hard:
            if same_owner and self.parked is not None and not explicit and changed:
                self.capture_native(field)
                if self.parked is not None:
                    self.rebase(retire=available)
                else:
                    self.reset(capture)
            else:
                self.reset(capture)
                if handoff:
                    self.refocus_epoch = self.reset_epoch
        else:
            if not previous[3]:
                self.capture_native(field)
            if previous != current:
                leaving = all(previous[1:3])
                self.leave(field, capture, latest=leaving, cancel=leaving)
        if available and self.writable_fence:
            self.reset_epoch += 1
            self.boundary += 1
            self.writable_fence = False
        self.context, self.authority = current, authority

    def unmount(self, field: Any, capture: str) -> None:
        self.cancel_focus(latest=True)
        if self.context is not None:
            self.reconcile(
                field or self.native,
                (self.context[0], False, False, *self.context[3:]),
                capture,
                available=True,
            )

    def presentation(self, field: Any, capture: str) -> tuple[str, ft.TextSelection | None]:
        value = self.parked.value if self.parked else self.value
        selection = self.parked.selection if self.parked else getattr(field, "selection", None)
        if self.reset_revision is not None or (field is None and self.parked is None):
            end = len(capture)
            selection = ft.TextSelection(base_offset=end, extent_offset=end)
        if self.reset_revision is not None:
            sent = field is not None and (field.value != value or field.selection != selection)
            self.waiting = self.waiting or sent
            if self.reported_reset != self.reset_revision:
                self.reported_reset = self.reset_revision
                if os.environ.get("RELAYDECK_TERMINAL_DIAGNOSTICS") == "1":
                    diagnostics_logger().info("direct_typing_reset sent=%s", sent)
            if not self.waiting:
                self.reset_revision = None
        self.presented = value
        self.presented_selection = replace(selection) if selection is not None else None
        return value, selection

    def suffix(self, value: str, common: int) -> int:
        length = 0
        limit = min(len(self.value), len(value)) - common
        while length < limit and self.value[-length - 1] == value[-length - 1]:
            length += 1
        return length

    def delta(self, value: str) -> tuple[int, str, int]:
        common = 0
        for before, after in zip(self.value, value, strict=False):
            if before != after:
                break
            common += 1
        removed = len(self.value) - max(common, self.unsent)
        inserted = value[common:]
        if self.protected is not None:
            suffix = self.suffix(value, common)
            removed = sum(not protected for protected in self.protected[common:])
            inserted = value[common : len(value) - suffix]
            inserted += "".join(
                char
                for char, protected in zip(
                    self.value[len(self.value) - suffix :],
                    self.protected[len(self.value) - suffix :],
                    strict=True,
                )
                if not protected
            )
        # A deletion on an otherwise empty sentinel is a deliberate Backspace.
        if self.value == DIRECT_SENTINEL and value in ("", " "):
            removed = 1
        return removed, inserted, common

    def accept(self, value: str, common: int) -> None:
        self.parked = None
        self.rebased = False
        self.unsent = min(self.unsent, common)
        if self.protected is not None:
            suffix = self.suffix(value, common)
            self.protected = (
                self.protected[:common]
                + (False,) * (len(value) - common - suffix)
                + self.protected[len(self.value) - suffix :]
            )
        self.value = value
        self.edit += 1
        self.reset_revision = None
        self.waiting = False
        self.cap_pending = len(value) >= DIRECT_EDIT_CAP

    def acknowledge_reset(self, revision: Any, capture: str, base: int, extent: int) -> None:
        if (
            self.waiting
            and revision == self.reset_revision == (self.boundary, self.edit)
            and base == extent == len(capture)
        ):
            self.waiting = False
            self.reset_revision = None


def _editor_state(field_ref: Any, capture: str) -> _DirectEditor:
    editor = getattr(field_ref, "_direct_editor", None)
    if not isinstance(editor, _DirectEditor):
        editor = _DirectEditor(value=capture, unsent=len(capture))
        field_ref._direct_editor = editor
    return editor


def use_direct_reset_requests() -> tuple[int, Callable[..., Any]]:
    """Fence non-field actions; negative revisions mark pending actions."""
    requests, set_requests = ft.use_state(0)
    pending = ft.use_ref(0)

    def bump(delta: int) -> None:
        pending.current = (pending.current or 0) + delta
        busy = bool(pending.current)
        set_requests(lambda value: -(abs(value) + 1) if busy else abs(value) + 1)

    def wrap(handler: Any, *, keys_only: bool = False) -> Any:
        if handler is None:
            return None

        async def invoke(*args: Any, **kwargs: Any) -> Any:
            reset = not keys_only or args[0].kind == "key"
            if reset:
                bump(1)
            try:
                result = handler(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            finally:
                if reset:
                    bump(-1)

        return invoke

    return requests, wrap


def _ignore_tap(_event: Any) -> None:
    """Flet needs one GestureDetector handler; watching taps do nothing."""


def _use_direct_focus(
    editor: _DirectEditor, field_ref: Any, requests: int, handled: Any, live: Callable[[], bool]
) -> None:
    """Consume/cancel intent once;
    delivery requires its live authority, native epoch and boundary.
    """
    epoch, boundary = editor.reset_epoch, editor.boundary
    owner = getattr(handled, "authority", editor.authority)
    if editor.authority is not None and owner != editor.authority:
        handled.current = max(requests, handled.current)

    async def focus() -> None:
        if live() and boundary == editor.boundary and field_ref.current is not None:
            await focus_control(field_ref.current)

    def request_focus() -> None:
        ready = editor.context[4] >= 0 and live()
        if ready and (requests > handled.current or editor.refocus_epoch == epoch):
            editor.refocus_epoch = None
            handled.current = requests  # Consume stale intent; never replay on resume.
            spawn(focus(), label="Direct typing focus")

    ft.use_effect(request_focus, dependencies=[requests, epoch, boundary, editor.refocus_epoch])


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
    on_focus: Callable[[], Any] | None = None,
    suffix_icon: Any = None,
    reset_requests: int = 0,
) -> Any:
    """Retain edits until explicit resets; lone newline means Return, bulk controls need review."""
    _, set_revision = ft.use_state(0)
    callbacks: Any = ft.use_ref(_DirectCallbacks)
    callbacks.current = _DirectCallbacks(
        set_revision, on_input, on_transaction, can_input, on_input_handled, on_focus
    )
    capture_value = "" if paste_fallback else DIRECT_SENTINEL
    editor_ref: Any = ft.use_ref(lambda: _editor_state(field_ref, capture_value))
    editor: _DirectEditor = editor_ref.current
    expected = (binding, visible, enabled, paste_fallback, reset_requests)
    live_ref: Any = ft.use_ref(expected)
    alive = ft.use_ref(True)
    fallback_lock: Any = ft.use_ref(lambda: asyncio.Lock())
    editor.reconcile(
        field_ref.current, expected, capture_value, available=callbacks.current.allowed()
    )
    # Reconciliation cancels prior intent; a newer open remains usable.
    editor.focus_request = (focus_requests, focus_handled)
    live_ref.current = expected
    epoch = editor.reset_epoch

    def live(native_epoch: int = epoch) -> bool:
        return (
            bool(alive.current)
            and native_epoch == editor.reset_epoch
            and visible
            and enabled
            and live_ref.current == expected
            and callbacks.current.allowed()
        )

    def reset(*, continuing: bool = False) -> None:
        refocus = continuing and live() and editor.focus_epoch == epoch
        editor.reset(capture_value)
        if refocus:
            editor.refocus_epoch = editor.reset_epoch
        callbacks.current.revise()

    def leave(_event: Any) -> None:
        if not alive.current or epoch != editor.reset_epoch:
            return
        if not live_ref.current[1]:  # Hidden focus notifications are not new boundaries.
            editor.cancel_focus(latest=True)
            return
        editor.leave(field_ref.current, capture_value, latest=True)
        callbacks.current.revise()

    def focused(_event: Any) -> Any:
        if live():
            editor.focus_epoch = epoch
            return callbacks.current.on_focus() if callbacks.current.on_focus is not None else None

    def cleanup() -> None:
        alive.current = False
        editor.unmount(field_ref.current, capture_value)
        editor.native = None
        callbacks.current = _DirectCallbacks()

    ft.use_effect(lambda: None, dependencies=[], cleanup=cleanup)

    _use_direct_focus(editor, field_ref, focus_requests, focus_handled, live)

    async def send(
        payloads: tuple[bytes, ...], boundary: int, *, paste_text: str | None = None
    ) -> str:
        modified_epoch = None

        def current() -> bool:
            return live() and editor.boundary == boundary

        def modified() -> None:
            nonlocal modified_epoch
            if current():
                refocus = editor.focus_epoch == epoch
                reset()
                if refocus:
                    modified_epoch = (editor.reset_epoch, editor.boundary)

        if callbacks.current.on_transaction is not None:
            result: str = await callbacks.current.on_transaction(
                ORIGIN_TEXT,
                payloads,
                can_input=_ModifierGuard(current, modified),
                paste_text=paste_text,
            )
            # Under-lock focus authorization survives only a successful, unchanged boundary.
            if (
                result == ROUTE_PTY
                and modified_epoch == (editor.reset_epoch, editor.boundary)
                and live(editor.reset_epoch)
            ):
                editor.refocus_epoch = editor.reset_epoch
                callbacks.current.revise()
            return result
        if paste_text is not None:
            return ROUTE_DROPPED
        async with fallback_lock.current:
            result = ROUTE_PTY
            for data in payloads:
                if not current():
                    return ROUTE_DROPPED
                result = await callbacks.current.on_input(ORIGIN_TEXT, data)
                if result != ROUTE_PTY:
                    return result
            return result

    async def change(event: Any) -> None:
        if not live() or editor.review_waiting:
            return
        value = event_text(event)
        removed, inserted, common = editor.delta(value)
        returning = inserted in ("\n", "\r")
        if returning and editor.return_epoch == epoch:
            callbacks.current.revise()
            return
        editor.accept(value, common)
        editor.return_epoch = epoch if returning else None
        review = bool(inserted) and (
            paste_fallback
            or not contains_only_safe_scalars(inserted)
            or "\t" in inserted
            or (not returning and ("\n" in inserted or "\r" in inserted))
        )
        if paste_fallback:
            removed, inserted, review = 0, value, bool(value)
        boundary, edit = editor.boundary, editor.edit
        callbacks.current.revise()
        if not paste_fallback:
            callbacks.current.handled()
        editor.review_waiting = review
        payloads = (b"\x7f",) * removed
        if inserted and not review:
            payloads += (inserted.replace("\n", "\r").encode(),)
        try:
            editor.inflight += 1
            result = (
                await send(
                    payloads if not review else (),
                    boundary,
                    paste_text=inserted if review else None,
                )
                if payloads or review
                else ROUTE_PTY
            )
        except BaseException:
            if editor.boundary == boundary:
                reset()
            raise  # uncertain writes are never reconstructed or retried
        finally:
            editor.inflight -= 1
        if editor.boundary != boundary or not alive.current or live_ref.current != expected:
            return
        if review or result != ROUTE_PTY or returning:
            reset(continuing=not review and result == ROUTE_PTY)
        elif len(value) <= len(DIRECT_SENTINEL) and editor.edit == edit:
            reset(continuing=True)
        if paste_fallback and result in (REQUEST_REVIEW, REQUEST_REJECTED):
            callbacks.current.handled()

    rendered_reset = editor.reset_revision

    def acknowledge_reset(base: int, extent: int) -> None:
        if live():
            editor.acknowledge_reset(rendered_reset, capture_value, base, extent)

    async def submit(event: Any) -> None:
        del event
        if not live() or editor.review_waiting or paste_fallback or editor.return_epoch == epoch:
            return
        editor.return_epoch = epoch
        boundary = editor.boundary
        result = ROUTE_DROPPED
        try:
            result = await send((b"\r",), boundary)
        finally:
            if editor.boundary == boundary:
                reset(continuing=result == ROUTE_PTY)

    tokens = palette(dark)
    value, selection = editor.presentation(field_ref.current, capture_value)
    field = ft.TextField(
        key=field_key,
        ref=field_ref,
        visible=visible,
        height=36,
        value=value,
        selection=selection,
        data={"direct_reset": rendered_reset if editor.waiting else None},
        hint_text="Type to terminal",
        prefix=None if paste_fallback else "Type to terminal  ",  # one label, never both
        tooltip="Type directly to the terminal",
        text_size=15,
        multiline=True,
        fit_parent_size=True,
        # shift_enter attaches another focus node; Return arrives as change.
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
        # Disabled selection makes the web engine collapse and rewrite dictation ranges.
        enable_interactive_selection=True,
        color=ft.Colors.TRANSPARENT,
        cursor_color=tokens["accent_text"],
        filled=True,
        fill_color=tokens["raised"],
        content_padding=8,
        size_constraints=ft.BoxConstraints(min_height=36),
        on_change=change,
        on_submit=submit,
        on_blur=leave,
        on_selection_change=selection_change(acknowledge_reset),
        on_tap_outside=lambda _e: None,
        on_focus=focused,
        suffix_icon=suffix_icon,
    )
    editor.native = field
    return ft.Container(key=f"{field_key}:reset:{epoch}", visible=visible, content=field)
