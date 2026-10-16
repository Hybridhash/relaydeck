"""The phone composer field and its single-column composer layout."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import flet as ft

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
from relaydeck.client.features.terminal.events import (
    click,
    value_change,
)
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.theme import palette

if TYPE_CHECKING:
    from relaydeck.client.features.composer.domain import StagedMarker


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
    watch_focus = on_focus_change is not None

    def focus_changed(focused: bool) -> Any:
        notify = callbacks.current[4]
        return notify(focused) if notify is not None else None

    def changed(text: str) -> Any:
        current, active, change, _send, _focus = callbacks.current
        return change(text) if active and text != current else None

    def send() -> Any:
        return callbacks.current[3]()

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
            min_lines=lines or 1,
            max_lines=lines or 4,
            # Flet has no blur(); canRequestFocus=false drops existing focus.
            can_request_focus=focusable,
            disabled=not focusable,
            hint_text="Message the agent…",
            tooltip=None if compact else "Agent draft",
            text_size=15,
            color=tokens["text"],
            cursor_color=tokens["accent_text"],
            filled=True,
            fill_color=tokens["raised"],
            content_padding=8,
            size_constraints=ft.BoxConstraints(min_height=36),
            suffix_icon_size_constraints=ft.BoxConstraints(min_width=36, min_height=36),
            suffix_icon=ft.IconButton(
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
            on_change=value_change(changed),
            on_focus=(lambda _e: focus_changed(True)) if watch_focus else None,
            on_blur=(lambda _e: focus_changed(False)) if watch_focus else None,
        )

    field = ft.use_memo(
        editor,
        dependencies=[draft, dark, blocked, compact, focusable, send_enabled, lines, watch_focus],
    )
    field_ref.current = field
    model_ref.current = draft
    return field


STATUS_ROW_KEY = "phone-composer-status"
STATUS_ROW_HEIGHT = 16.0


def _status_text(text: str, key: str) -> ft.Text:
    return ft.Text(
        text, key=key, size=12, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True
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
    compact: bool = False,
    attachment: Any = None,
    chips: Any = None,
    lines: int | None = None,
    on_focus_change: Callable[[bool], Any] | None = None,
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
        )
    )
    if attachment is not None:
        row.insert(1 if toggle is not None else 0, attachment)
    controls: list[Any] = [ft.Row(key=composer_ui.FOOTER_KEY, spacing=6, controls=row)]
    if chips is not None:
        controls.append(chips)
    # Status lines share one fixed-height row: a notice arriving after send
    # must not change the dock's height (that resizes the terminal again).
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
        SUBMISSION_PROGRESS: "Agent is working on your message.",
    }.get(submission)
    if message:
        status.append(_status_text(message, composer_ui.NOTICE_KEY))
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
    controls.append(
        ft.Container(
            key=STATUS_ROW_KEY,
            height=STATUS_ROW_HEIGHT,
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
