"""Generation-bound paste review and confirmation.

The review captures framing and writer generation at request time. Confirmation
revalidates the writer and sends only the reviewed bytes; each frame is checked
again by the gateway. Rejection clears the review, while single-line insertion
leaves an existing review untouched. The sheet renders untrusted text verbatim."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import flet as ft

from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.paste_policy import (
    OUTCOME_INSERT,
    OUTCOME_REJECTED,
    OUTCOME_REVIEW,
    PasteError,
    PasteReview,
    character_count,
    confirm_chunks,
    insert_chunks,
    line_count,
    preview,
    validate,
)
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH

if TYPE_CHECKING:
    from relaydeck.client.features.terminal.controller import TerminalController

REQUEST_INSERTED = "inserted"
REQUEST_REVIEW = "review"
REQUEST_REJECTED = "rejected"
REQUEST_BLOCKED = "blocked"

REVIEW_TITLE = "Review paste"
CONFIRM_LABEL = "Paste"
CANCEL_LABEL = "Cancel"

SHEET_KEY = "paste-review-sheet"
TITLE_KEY = "paste-review-title"
COUNTS_KEY = "paste-review-counts"
SOURCE_KEY = "paste-review-source"
APP_COPY_LABEL = "Last copied in RelayDeck; clipboard unavailable"
PREVIEW_KEY = "paste-review-preview"
PREVIEW_FRAME_KEY = "paste-review-preview-frame"
CONFIRM_KEY = "paste-review-confirm"
CANCEL_KEY = "paste-review-cancel"

BLOCKED_MESSAGE = "Terminal is not accepting input: no live control writer."
STALE_MESSAGE = "Terminal session changed; review the paste again. Nothing was sent."
NO_REVIEW_MESSAGE = "No paste awaiting review."


def counts_line(review: PasteReview) -> str:
    return f"{review.line_count} lines \u00b7 {review.character_count} characters"


@dataclass(frozen=True)
class PendingPaste:
    text: str
    generation: int
    bracketed: bool
    review: PasteReview


class PasteReviewSession:
    """One generation-bound review at a time.

    `generation_probe` returns the current writer generation, or None
    when no writer is live. `sender` transmits one <=16 KiB text chunk.
    Confirm sends the exact reviewed bytes (framed iff bracketed mode
    was captured at open) only when the probe still reports the bound
    generation; anything else fails closed with no input sent.
    """

    def __init__(
        self,
        generation_probe: Callable[[], int | None],
        sender: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._probe = generation_probe
        self._sender = sender
        self._pending: PendingPaste | None = None
        self._error: str | None = None

    @property
    def pending(self) -> PendingPaste | None:
        return self._pending

    @property
    def error(self) -> str | None:
        return self._error

    async def request_paste(
        self,
        text: str,
        bracketed: bool,
        *,
        require_review: bool = False,
        from_app: bool = False,
    ) -> str:
        """Classify and act: single-line inserts now, multiline opens a
        generation-bound review, unsafe controls reject, and a missing
        writer reports blocked. Only the insert path sends."""
        self._error = None
        generation = self._probe()
        if generation is None:
            self._error = BLOCKED_MESSAGE
            return REQUEST_BLOCKED
        validation = validate(text, bracketed)
        if validation.outcome == OUTCOME_INSERT and (require_review or from_app) and text:
            self._pending = PendingPaste(
                text=text,
                generation=generation,
                bracketed=bracketed,
                review=PasteReview(
                    line_count(text), character_count(text), preview(text), from_app=from_app
                ),
            )
            return REQUEST_REVIEW
        if validation.outcome == OUTCOME_INSERT:
            for chunk in insert_chunks(text):
                await self._sender(chunk)
            return REQUEST_INSERTED
        if validation.outcome == OUTCOME_REVIEW:
            assert validation.review is not None  # noqa: S101
            self._pending = PendingPaste(
                text=text,
                generation=generation,
                bracketed=validation.bracketed,
                review=replace(validation.review, from_app=from_app),
            )
            return REQUEST_REVIEW
        assert validation.outcome == OUTCOME_REJECTED  # noqa: S101
        self._pending = None
        self._error = validation.reason or "rejected"
        return REQUEST_REJECTED

    async def confirm_paste(self) -> bool:
        """Send the exact reviewed bytes iff the bound generation is
        still current. Stale generation, dead writer, or no pending
        review fails closed: pending is cleared and nothing is sent.
        Pending is cleared before the first byte is sent, so a mid-send
        failure cannot be retried implicitly: the user re-opens review
        and bytes are never duplicated."""
        pending = self._pending
        if pending is None:
            self._error = NO_REVIEW_MESSAGE
            return False
        if self._probe() != pending.generation:
            self._pending = None
            self._error = STALE_MESSAGE
            return False
        chunks = confirm_chunks(pending.text, pending.bracketed)
        self._pending = None
        for chunk in chunks:
            await self._sender(chunk)
        return True

    def cancel_paste(self) -> None:
        """Drop the pending review. Provably sends nothing."""
        self._pending = None

    def clear_error(self) -> None:
        self._error = None


def bind_review_session(controller: TerminalController) -> PasteReviewSession:
    """Wire a session to the sole current writer: the controller's live
    control-lease generation probe plus its text-frame sender."""
    return PasteReviewSession(
        generation_probe=controller.paste_generation,
        sender=controller.send_paste_chunk,
    )


@ft.component
def PasteReviewSheet(
    review: PasteReview,
    on_confirm: Callable[[], Any],
    on_cancel: Callable[[], Any],
) -> Any:
    """Render untrusted preview text with counts and explicit actions.

    The mounting screen supplies the themed surface frame.
    """
    return ft.Column(
        key=SHEET_KEY,
        spacing=8,
        controls=[
            ft.Text(
                REVIEW_TITLE,
                key=TITLE_KEY,
                size=FONT_SIZE["body"],
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(counts_line(review), key=COUNTS_KEY, size=FONT_SIZE["small"]),
            ft.Text(
                APP_COPY_LABEL,
                key=SOURCE_KEY,
                visible=review.from_app,
                size=FONT_SIZE["small"],
            ),
            ft.Container(
                key=PREVIEW_FRAME_KEY,
                height=176,
                padding=12,
                content=ft.Column(
                    scroll=ft.ScrollMode.AUTO,
                    controls=[
                        ft.Text(
                            review.preview,
                            key=PREVIEW_KEY,
                            selectable=True,
                            font_family="monospace",
                        )
                    ],
                ),
            ),
            ft.Row(
                wrap=True,
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.Button(
                        CANCEL_LABEL,
                        key=CANCEL_KEY,
                        height=MIN_TOUCH,
                        on_click=click(on_cancel),
                    ),
                    ft.Button(
                        CONFIRM_LABEL,
                        key=CONFIRM_KEY,
                        height=MIN_TOUCH,
                        on_click=click(on_confirm),
                    ),
                ],
            ),
        ],
    )


__all__ = [
    "APP_COPY_LABEL",
    "CANCEL_KEY",
    "CANCEL_LABEL",
    "CONFIRM_KEY",
    "CONFIRM_LABEL",
    "COUNTS_KEY",
    "PREVIEW_FRAME_KEY",
    "PREVIEW_KEY",
    "REQUEST_BLOCKED",
    "REQUEST_INSERTED",
    "REQUEST_REJECTED",
    "REQUEST_REVIEW",
    "SHEET_KEY",
    "SOURCE_KEY",
    "TITLE_KEY",
    "PasteError",
    "PasteReviewSession",
    "PendingPaste",
    "PasteReviewSheet",
    "bind_review_session",
    "counts_line",
]
