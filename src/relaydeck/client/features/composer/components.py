"""Declarative composer box.

`ComposerBox` is fully controlled: immutable props plus callbacks, no
sockets, no timers, no direct store access. It renders the draft field,
staged markers, Send enablement, error/unknown/blocked notices, and the
multiline blocked-review surface.

Mount contract (screen owner):
- Own one `ComposerStore` per agent detail and pass props shaped as
  `draft=store.draft`, `markers=store.markers`,
  `can_send=store.can_send`, `blockers=store.send_blockers()`,
  `submission=store.submission`, `error=store.error`,
  `review=store.pending_blocked.review if store.pending_blocked else None`,
  `input_mode=store.input_mode`.
- Route `on_draft_change` to `store.replace_draft`, `on_send` to an async
  task calling `store.send()`, `on_confirm_blocked` to
  `store.confirm_blocked_insert()`, `on_cancel_blocked` to
  `store.cancel_blocked_insert()`, and `on_clear_error` to
  `store.clear_error()`.
- When `input_mode` becomes `"tools-keys"` after blocked delivery, mount
  the keys UI; this component only announces the handoff.
- Nothing mounts `ComposerBox` yet; like the paste review sheet, first
  mounting lands with the screen work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.surfaces import SurfaceFrame
from relaydeck.client.features.composer.domain import (
    BLOCKER_EMPTY,
    INPUT_MODE_TOOLS_KEYS,
    SUBMISSION_BLOCKED_INSERTED,
    SUBMISSION_FAILED,
    SUBMISSION_PROGRESS,
    SUBMISSION_SENDING,
    SUBMISSION_UNKNOWN,
    StagedMarker,
)
from relaydeck.client.features.composer.draft_field import use_draft_value
from relaydeck.client.features.composer.shortcut import use_send_shortcut
from relaydeck.client.features.terminal.events import click, value_change
from relaydeck.client.features.terminal.paste_policy import PasteReview
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH, palette

COMPOSER_KEY = "composer-box"
DRAFT_KEY = "composer-draft"
MARKERS_KEY = "composer-markers"
SEND_KEY = "composer-send"
NOTICE_KEY = "composer-notice"
ERROR_KEY = "composer-error"
VALIDATION_KEY = "composer-validation"
REVIEW_KEY = "composer-blocked-review"
REVIEW_COUNTS_KEY = "composer-blocked-counts"
REVIEW_PREVIEW_KEY = "composer-blocked-preview"
REVIEW_CONFIRM_KEY = "composer-blocked-confirm"
REVIEW_CANCEL_KEY = "composer-blocked-cancel"
MODE_KEY = "composer-mode"
FRAME_KEY = "composer-frame"
REVIEW_FRAME_KEY = "composer-review-frame"
FOOTER_KEY = "composer-footer"


SEND_LABEL = "Send"
CONFIRM_LABEL = "Insert into terminal"
CANCEL_LABEL = "Cancel"
REVIEW_TITLE = "Review blocked insertion"
EMPTY_HINT = "Write a message to send."
SPLIT_HINT_HEIGHT = 18.0  # one line of FONT_SIZE["small"] text
UNRESOLVED_HINT = "Waiting on staged content\u2026"
BLOCKED_NOTICE = (
    "Inserted into the live terminal without submitting. "
    "Use the tools keyboard to press Enter or Esc yourself."
)
UNKNOWN_NOTICE = "Delivery is uncertain. The draft was kept and nothing was retried."


def _validation_hint(blockers: tuple[str, ...]) -> str:
    if any(b == BLOCKER_EMPTY for b in blockers):
        return EMPTY_HINT
    if blockers:
        return UNRESOLVED_HINT
    return ""


def markers_line(markers: tuple[StagedMarker, ...]) -> str:
    """One-line staged-marker summary (markers only; own
    backends). Empty when nothing is staged."""
    if not markers:
        return ""
    parts = [
        f"{m.token} (ready)" if m.resolved else f"{m.token} (resolving\u2026)" for m in markers
    ]
    return "Staged: " + ", ".join(parts)


@ft.component
def ComposerBox(
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
    dark: bool = True,
    blocked: bool = False,
    attachment: Any = None,
    chips: Any = None,
    on_focus_change: Callable[[bool], Any] | None = None,
    fill_lines: int | None = None,
) -> Any:
    """Presentation: reference `.composer` frame (draft field plus
    staged/validation/error notices) with a `.composer-footer` row
    carrying Send, over the shared tokens (`dark` selects the
    table). Every key, callback, value handler, and traversal position
    is unchanged. Optional props (both default off, defaults render exactly as before):
    `on_focus_change(bool)` reports the draft field gaining/losing focus;
    `fill_lines` fixes the field to that many visible lines (text scrolls
    inside, the layout never grows). Only plain TextField props change, never
    the widget structure or wrappers, so the field keeps its identity and its
    focus while the box moves between layouts."""
    tokens = palette(dark)
    hint = _validation_hint(blockers)
    staged = markers_line(markers)
    busy = submission == SUBMISSION_SENDING
    send_disabled = not can_send or busy or review is not None
    # a parent render between a native paste and its on_change must not
    # send the older draft back into the field (same guard as the phone composer).
    draft_value, remember_draft = use_draft_value(draft, focusable=True)
    shortcut, shortcut_hint = use_send_shortcut(
        draft=draft, can_send=not send_disabled, send=lambda: on_send()
    )

    def focus_changed(focused: bool) -> None:
        shortcut.focus(focused)
        if on_focus_change is not None:
            on_focus_change(focused)

    def draft_changed(text: str) -> Any:
        return None if shortcut.swallows(text) else on_draft_change(text)

    draft_control = ft.TextField(
        key=DRAFT_KEY,
        expand=True,
        value=draft_value,
        multiline=True,
        min_lines=fill_lines or 1,
        max_lines=fill_lines or 5,
        label="Agent draft",
        hint_text="Message the agent…",
        color=tokens["text"],
        label_style=ft.TextStyle(color=tokens["muted"]),
        text_size=16,
        cursor_color=tokens["accent_text"],
        filled=True,
        fill_color=tokens["raised"],
        content_padding=8,
        border={
            ft.ControlState.DEFAULT: ft.OutlineInputBorder(
                border_radius=T.RADIUS_KEY, side=ft.BorderSide(1, tokens["outline"])
            ),
            ft.ControlState.FOCUSED: ft.OutlineInputBorder(
                border_radius=T.RADIUS_KEY, side=ft.BorderSide(2, tokens["accent_text"])
            ),
        },
        on_change=value_change(draft_changed),
        on_focus=lambda _e: focus_changed(True),
        on_blur=lambda _e: focus_changed(False),
    )
    remember_draft(draft_control)
    frame_controls: list[Any] = [draft_control]
    if staged:
        frame_controls.append(ft.Text(staged, key=MARKERS_KEY, size=FONT_SIZE["small"]))
    if fill_lines is not None:
        # in the typing split the hint row keeps its height whether or
        # not it has text, so the first keystroke (which clears the hint) does not
        # reflow the column.
        shown = hint if hint and can_send is False and error is None and review is None else ""
        frame_controls.append(
            ft.Container(
                key=VALIDATION_KEY,
                height=SPLIT_HINT_HEIGHT,
                content=ft.Text(shown, size=FONT_SIZE["small"], max_lines=1),
            )
        )
    elif hint and can_send is False and error is None and review is None:
        frame_controls.append(ft.Text(hint, key=VALIDATION_KEY, size=FONT_SIZE["small"]))
    if error is not None and submission in (SUBMISSION_FAILED, SUBMISSION_UNKNOWN):
        frame_controls.append(
            ft.Row(
                wrap=True,
                controls=[
                    ft.Text(error, key=ERROR_KEY),
                    ft.Button(
                        "Dismiss",
                        key="composer-error-dismiss",
                        height=MIN_TOUCH,
                        on_click=click(on_clear_error),
                    ),
                ],
            )
        )
    if submission == SUBMISSION_UNKNOWN:
        frame_controls.append(ft.Text(UNKNOWN_NOTICE, key=NOTICE_KEY, size=FONT_SIZE["small"]))
    if submission == SUBMISSION_BLOCKED_INSERTED:
        frame_controls.append(ft.Text(BLOCKED_NOTICE, key=NOTICE_KEY, size=FONT_SIZE["small"]))
    if submission == SUBMISSION_PROGRESS:
        frame_controls.append(
            ft.Text(
                "Agent is working on your message.",
                key="composer-progress",
                size=FONT_SIZE["small"],
            )
        )
    # Keep the send action alongside the draft as in the supplied composer,
    # while retaining all delivery/review guards and stable event keys.
    send_style = chrome_style(dark, primary=True)
    send_style.shape = ft.RoundedRectangleBorder(radius=24)
    send_style.padding = 0
    send = ft.IconButton(
        ft.Icons.ARROW_UPWARD,
        key=SEND_KEY,
        tooltip=f"{'Insert into terminal' if blocked else 'Send'} ({shortcut_hint})",
        disabled=send_disabled,
        width=MIN_TOUCH,
        height=MIN_TOUCH,
        style=send_style,
        on_click=click(on_send),
    )
    if attachment is not None and isinstance(attachment, ft.IconButton):
        # Same footprint and axis as Send, so the two buttons line up in the footer row.
        attach_style = chrome_style(dark)
        attach_style.shape = ft.RoundedRectangleBorder(radius=24)
        attach_style.padding = 0
        attachment.width = attachment.height = MIN_TOUCH
        attachment.icon_size = 22
        attachment.style = attach_style
    draft_field = frame_controls.pop(0)
    frame_controls.insert(
        0,
        ft.Row(
            key=FOOTER_KEY,
            vertical_alignment=ft.CrossAxisAlignment.END,
            spacing=10,
            controls=[draft_field, *([attachment] if attachment is not None else []), send],
        ),
    )
    if chips is not None:
        frame_controls.append(chips)
    controls: list[Any] = [
        SurfaceFrame(
            FRAME_KEY,
            ft.Column(spacing=8, controls=frame_controls),
            tokens=tokens,
        )
    ]
    if review is not None:
        controls.append(
            SurfaceFrame(
                REVIEW_FRAME_KEY,
                ft.Column(
                    key=REVIEW_KEY,
                    controls=[
                        ft.Text(
                            REVIEW_TITLE,
                            key="composer-blocked-title",
                            size=FONT_SIZE["body"],
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            f"{review.line_count} lines \u00b7 {review.character_count} characters",
                            key=REVIEW_COUNTS_KEY,
                            size=FONT_SIZE["small"],
                        ),
                        ft.Container(
                            height=144,
                            content=ft.Column(
                                scroll=ft.ScrollMode.AUTO,
                                controls=[
                                    ft.Text(
                                        review.preview,
                                        key=REVIEW_PREVIEW_KEY,
                                        selectable=True,
                                        font_family="monospace",
                                    )
                                ],
                            ),
                        ),
                        ft.Row(
                            wrap=True,
                            controls=[
                                ft.Button(
                                    CANCEL_LABEL,
                                    key=REVIEW_CANCEL_KEY,
                                    height=MIN_TOUCH,
                                    on_click=click(on_cancel_blocked),
                                ),
                                ft.Button(
                                    CONFIRM_LABEL,
                                    key=REVIEW_CONFIRM_KEY,
                                    height=MIN_TOUCH,
                                    on_click=click(on_confirm_blocked),
                                ),
                            ],
                        ),
                    ],
                ),
                tokens=tokens,
            )
        )
    if input_mode == INPUT_MODE_TOOLS_KEYS:
        controls.append(ft.Text("Tools keys", key=MODE_KEY, size=FONT_SIZE["small"]))
    return ft.Column(key=COMPOSER_KEY, spacing=8, controls=controls)


__all__ = [
    "BLOCKED_NOTICE",
    "CANCEL_LABEL",
    "COMPOSER_KEY",
    "CONFIRM_LABEL",
    "DRAFT_KEY",
    "EMPTY_HINT",
    "ERROR_KEY",
    "FOOTER_KEY",
    "FRAME_KEY",
    "MARKERS_KEY",
    "MODE_KEY",
    "NOTICE_KEY",
    "REVIEW_CANCEL_KEY",
    "REVIEW_CONFIRM_KEY",
    "REVIEW_COUNTS_KEY",
    "REVIEW_FRAME_KEY",
    "REVIEW_KEY",
    "REVIEW_PREVIEW_KEY",
    "REVIEW_TITLE",
    "SEND_KEY",
    "SEND_LABEL",
    "UNKNOWN_NOTICE",
    "UNRESOLVED_HINT",
    "VALIDATION_KEY",
    "ComposerBox",
    "markers_line",
]
