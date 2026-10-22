"""The phone composer field and its single-column composer layout."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.components.design_system import tokens as console_tokens
from relaydeck.client.components.design_system.terminal_window import (
    PillButton,
)
from relaydeck.client.features.composer import components as composer_ui
from relaydeck.client.features.composer.domain import (
    INPUT_MODE_TOOLS_KEYS,
    SUBMISSION_BLOCKED_INSERTED,
    SUBMISSION_FAILED,
    SUBMISSION_PROGRESS,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
)
from relaydeck.client.features.composer.draft_field import keep_native_value
from relaydeck.client.features.composer.keyboard_toggle import keep_focus, keyboard_toggle
from relaydeck.client.features.composer.shortcut import use_send_shortcut
from relaydeck.client.features.terminal.events import (
    click,
    spawn,
    value_change,
)
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.hooks.typing_focus import use_composer_focus_log
from relaydeck.client.theme import palette

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker


def use_composer_keyboard(
    *, enabled: bool, binding: Any, on_focus_change: Callable[[bool], Any] | None
) -> tuple[bool, bool, Callable[[bool], Any], Callable[[], None], Callable[[], None]]:
    focused, set_focused = ft.use_state(False)
    closing, set_closing = ft.use_state(False)
    current = ft.use_ref((binding, enabled))
    focused_now = ft.use_ref(focused)
    pending: Any = ft.use_ref(None)
    log_focus, note_send = use_composer_focus_log(on_focus_change)
    expected = (binding, enabled)
    current.current = expected
    focused_now.current = focused

    def cancel_close() -> None:
        if pending.current is not None:
            pending.current.cancel()
            pending.current = None

    def reset() -> None:
        set_closing(False)
        focused_now.current = False
        set_focused(False)

    ft.use_effect(reset, dependencies=[expected], cleanup=cancel_close)

    def focus_changed(value: bool) -> Any:
        if current.current != expected or (value and (not enabled or pending.current is not None)):
            return None
        focused_now.current = value
        set_focused(value)
        return log_focus(value)

    def dismiss() -> None:
        if (
            current.current != expected
            or not enabled
            or not focused_now.current
            or pending.current is not None
        ):
            return
        set_closing(True)
        focus_changed(False)

        async def finish() -> None:
            await asyncio.sleep(0.05)
            if current.current == expected:
                pending.current = None
                set_closing(False)

        pending.current = spawn(finish(), label="Close composer focus")

    return focused, closing, focus_changed, dismiss, note_send


def with_working_label(
    footer: Any, working: bool, tokens: dict[str, str], key: str = "composer-working"
) -> Any:
    # Floats just above the input over the terminal: no layout change and never over Send.
    # The Stack is always there so the input keeps its identity (and focus).
    shimmer = composer_ui.working_shimmer(
        WORKING_LABEL, tokens, size=12, weight=ft.FontWeight.W_600
    )
    # The terminal's canvas colour, so the label blends into its last line.
    label = ft.Container(
        key=key,
        top=-WORKING_LABEL_RISE,
        right=8,
        padding=ft.Padding.symmetric(horizontal=6, vertical=1),
        border_radius=4,
        bgcolor=console_tokens.TERMINAL_CANVAS,
        content=shimmer,
    )
    return ft.Stack(
        clip_behavior=ft.ClipBehavior.NONE,
        controls=[footer, *([label] if working else [])],
    )


def _suffix(send: Any, toggle: Any | None) -> Any:
    return send if toggle is None else ft.Row(tight=True, spacing=4, controls=[send, toggle])


@ft.component
def _PhoneComposerField(
    *,
    draft: str,
    dark: bool,
    blocked: bool,
    compact: bool,
    focusable: bool,
    send_enabled: bool,
    on_draft_change: Callable[[str], Any],
    on_send: Callable[[], Any],
    key: str = "phone-composer-editor",
    lines: int | None = None,
    on_focus_change: Callable[[bool], Any] | None = None,
    keyboard: str = "",
    on_hide_keyboard: Callable[[], Any] | None = None,
    hint: str = "Message the agent…",
) -> Any:
    """Keep native editing intact until the authoritative draft changes.

    Flet applies native text props before dispatching the change event. A
    parent render between those messages must not restore the older draft.
    Selection stays entirely native; there is no selection/focus round trip.
    """
    field_ref: Any = ft.use_ref(None)
    model_ref = ft.use_ref(draft)
    callbacks: Any = ft.use_ref((draft, focusable, on_draft_change, on_send, on_focus_change))
    callbacks.current = (draft, focusable, on_draft_change, on_send, on_focus_change)
    hide_ref: Any = ft.use_ref(None)
    hide_ref.current = on_hide_keyboard
    watch_focus = on_focus_change is not None

    def focus_changed(focused: bool) -> Any:
        shortcut.focus(focused)
        notify = callbacks.current[4]
        return notify(focused) if notify is not None else None

    def changed(text: str) -> Any:
        current, active, change, _send, _focus = callbacks.current
        if shortcut.swallows(text):
            return None
        return change(text) if active and text != current else None

    def send() -> Any:
        return callbacks.current[3]()

    shortcut, _hint = use_send_shortcut(draft=draft, can_send=send_enabled, send=send)

    def hide() -> Any:
        return hide_ref.current() if hide_ref.current is not None else None

    def show() -> None:
        # Screen-reader activation only. A touch lands on the field itself (see _keyboard_toggle).
        field = field_ref.current
        if field is not None:
            spawn(field.focus(), label="Show composer keyboard")

    def editor() -> Any:
        # Hidden/parked fields reject late changes. Reopening restores the
        # authoritative draft rather than an ignored native property update.
        value = keep_native_value(field_ref.current, model_ref.current, draft, focusable=focusable)
        tokens = palette(dark)
        return ft.TextField(
            key=composer_ui.DRAFT_KEY,
            expand=True,
            value=value,
            multiline=True,
            # A fixed height: typing never resizes the terminal; longer text scrolls inside.
            min_lines=lines or PHONE_DRAFT_LINES,
            max_lines=lines or PHONE_DRAFT_LINES,
            # Flet has no blur(); canRequestFocus=false drops existing focus.
            can_request_focus=focusable,
            disabled=not focusable,
            hint_text=hint,
            tooltip=None if compact else "Agent draft",
            text_size=15,
            color=tokens["text"],
            cursor_color=tokens["accent_text"],
            filled=True,
            fill_color=tokens["raised"],
            content_padding=8,
            size_constraints=ft.BoxConstraints(min_height=36),
            suffix_icon_size_constraints=ft.BoxConstraints(min_width=36, min_height=36),
            suffix_icon=_suffix(
                ft.IconButton(
                    ft.Icons.ARROW_UPWARD,
                    key=composer_ui.SEND_KEY,
                    width=28,
                    height=28,
                    icon_size=16,
                    padding=0,
                    bgcolor=tokens["accent"],
                    icon_color=tokens["on_accent"],
                    tooltip="Insert into terminal" if blocked else "Send message",
                    disabled=not send_enabled,
                    on_click=click(send),
                ),
                keyboard_toggle(keyboard, compact, hide, show),
            ),
            on_change=value_change(changed),
            on_focus=lambda _e: focus_changed(True),
            on_blur=lambda _e: focus_changed(False),
            on_tap_outside=keep_focus,
        )

    field = ft.use_memo(
        editor,
        dependencies=[
            draft,
            dark,
            blocked,
            compact,
            focusable,
            send_enabled,
            lines,
            watch_focus,
            keyboard,
        ],
    )
    field_ref.current = field
    model_ref.current = draft
    return field


STATUS_ROW_KEY = "phone-composer-status"
WORKING_HINT = "Agent is working… type your next message"
WORKING_NOTICE = "Agent is working on your message."
WORKING_LABEL = "Agent is working"
WORKING_LABEL_RISE = 18  # sits in the terminal's last line, just above the box
PHONE_DRAFT_LINES = 2
STATUS_ROW_HEIGHT = 16.0
STATUS_ROW_EMPTY = 2.0  # keeps the text box clear of iOS Safari's input bar


def _status_text(text: str, key: str, shown: bool = True) -> ft.Semantics:
    return ft.Semantics(
        live_region=True,
        expand=True,
        content=ft.Text(
            text,
            key=key,
            size=12,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
            opacity=1 if shown else 0,
        ),
    )


def _phone_composer(
    *,
    draft: str,
    markers: tuple[StagedMarker, ...],
    can_send: bool,
    blockers: tuple[str, ...],
    submission: str,
    error: str | None,
    review: PasteReview | None,
    input_mode: str,
    on_draft_change: Callable[[str], Any],
    on_send: Callable[[], Any],
    on_confirm_blocked: Callable[[], Any],
    on_cancel_blocked: Callable[[], Any],
    on_clear_error: Callable[[], Any],
    toggle: Any | None,
    dark: bool,
    blocked: bool,
    visible: bool,
    focusable: bool,
    working: bool = False,
    compact: bool = False,
    attachment: Any = None,
    chips: Any = None,
    lines: int | None = None,
    on_focus_change: Callable[[bool], Any] | None = None,
    keyboard: str = "",
    on_hide_keyboard: Callable[[], None] | None = None,
) -> Any:
    """Grow from one to four visible lines, preserving delivery safeguards.

    A retained multiline draft is neither normalized nor truncated.
    Enter is not a send action.
    Only the explicit Send callback (including its IME gate) submits.
    """
    row: list[Any] = [] if toggle is None else [toggle]
    row.append(
        _PhoneComposerField(
            key="phone-composer-editor",
            draft=draft,
            dark=dark,
            blocked=blocked,
            compact=compact,
            focusable=focusable,
            send_enabled=can_send and submission != SUBMISSION_SENDING and review is None,
            on_draft_change=on_draft_change,
            on_send=on_send,
            lines=lines,
            on_focus_change=on_focus_change,
            keyboard=keyboard if visible else "",
            on_hide_keyboard=on_hide_keyboard,
            hint=WORKING_HINT if submission == SUBMISSION_PROGRESS else "Message the agent…",
        )
    )
    if attachment is not None:
        row.insert(1 if toggle is not None else 0, attachment)
    footer = ft.Row(key=composer_ui.FOOTER_KEY, spacing=6, controls=row)
    controls: list[Any] = [with_working_label(footer, working, palette(dark))]
    if chips is not None:
        controls.append(chips)
    status: list[Any] = []
    staged = composer_ui.markers_line(markers)
    if staged:
        status.append(_status_text(staged, composer_ui.MARKERS_KEY))
    # An empty draft needs no extra line; meaningful unresolved content does.
    if blockers and blockers != ("empty",) and not can_send and error is None and review is None:
        status.append(_status_text(composer_ui.UNRESOLVED_HINT, composer_ui.VALIDATION_KEY))
    if error is not None and submission in (SUBMISSION_FAILED, SUBMISSION_UNKNOWN):
        controls.append(
            ft.Row(
                controls=[
                    ft.Text(error, key=composer_ui.ERROR_KEY, expand=True, size=12),
                    PillButton("Dismiss", on_clear_error, key="composer-error-dismiss"),
                ]
            )
        )
    message = {
        SUBMISSION_UNKNOWN: composer_ui.UNKNOWN_NOTICE,
        SUBMISSION_BLOCKED_INSERTED: composer_ui.BLOCKED_NOTICE,
    }.get(submission)
    if message:
        status.append(_status_text(message, composer_ui.NOTICE_KEY))
    shown = bool(status)
    if submission == SUBMISSION_PROGRESS:
        # Read aloud by screen readers but not drawn: the header badge shows it on screen.
        status.append(_status_text(WORKING_NOTICE, composer_ui.NOTICE_KEY, shown=False))
    if review is not None:
        controls.append(
            ft.Column(
                key=composer_ui.REVIEW_KEY,
                controls=[
                    ft.Text(composer_ui.REVIEW_TITLE, key="composer-blocked-title"),
                    ft.Text(
                        f"{review.line_count} lines · {review.character_count} characters",
                        key=composer_ui.REVIEW_COUNTS_KEY,
                    ),
                    ft.Container(
                        height=96,
                        content=ft.Column(
                            scroll=ft.ScrollMode.AUTO,
                            controls=[
                                ft.Text(
                                    review.preview,
                                    key=composer_ui.REVIEW_PREVIEW_KEY,
                                    selectable=True,
                                )
                            ],
                        ),
                    ),
                    ft.Row(
                        scroll=ft.ScrollMode.AUTO,
                        controls=[
                            PillButton(
                                composer_ui.CANCEL_LABEL,
                                on_cancel_blocked,
                                key=composer_ui.REVIEW_CANCEL_KEY,
                            ),
                            PillButton(
                                composer_ui.CONFIRM_LABEL,
                                on_confirm_blocked,
                                key=composer_ui.REVIEW_CONFIRM_KEY,
                                tone="primary",
                            ),
                        ],
                    ),
                ],
            )
        )
    if input_mode == INPUT_MODE_TOOLS_KEYS:
        status.append(_status_text("Tools keys", composer_ui.MODE_KEY))
    # Always present: a small gap when empty, full height only for the rare notices.
    controls.append(
        ft.Container(
            key=STATUS_ROW_KEY,
            height=STATUS_ROW_HEIGHT if shown else STATUS_ROW_EMPTY,
            content=ft.Row(spacing=8, controls=status),
        )
    )
    return ft.Column(
        key=composer_ui.COMPOSER_KEY,
        visible=visible,
        tight=True,
        spacing=4,
        controls=controls,
    )
