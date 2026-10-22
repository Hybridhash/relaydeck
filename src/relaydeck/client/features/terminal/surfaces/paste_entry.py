"""Paste panel for browsers that block clipboard reads: the platform's own Paste goes into a
box, and the text then goes through the same paste review as every other paste."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, NamedTuple

import flet as ft

from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.surfaces.common import _framed
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH

LAYER_KEY = "paste-entry-layer"
FIELD_KEY = "paste-entry-field"
REVIEW_KEY = "paste-entry-review"
CANCEL_KEY = "paste-entry-cancel"
HINT = "Long-press in the box and choose Paste."


class PasteEntry(NamedTuple):
    visible: bool
    on_submit: Callable[[str], Any]
    on_close: Callable[[], None]


@ft.component
def PasteEntrySheet(
    *,
    visible: bool,
    on_submit: Callable[[str], Any],
    on_close: Callable[[], None],
    tokens: dict[str, str],
    top: float,
    wide: bool,
    width: float,
) -> Any:
    text, set_text = ft.use_state("")

    def clear_when_closed() -> None:
        if not visible:
            set_text("")

    ft.use_effect(clear_when_closed, dependencies=[visible])

    async def review() -> None:
        value = text
        on_close()
        if value:
            pending = on_submit(value)
            if inspect.isawaitable(pending):
                await pending

    body = ft.Column(
        spacing=8,
        tight=True,
        controls=[
            ft.Text("Paste", size=FONT_SIZE["body"], weight=ft.FontWeight.BOLD),
            ft.Text(HINT, size=FONT_SIZE["small"], color=tokens["muted"]),
            ft.TextField(
                key=FIELD_KEY,
                value=text,
                multiline=True,
                min_lines=3,
                max_lines=6,
                hint_text="Paste here",
                on_change=lambda e: set_text(e.control.value or ""),
            ),
            ft.Row(
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.Button("Cancel", key=CANCEL_KEY, height=MIN_TOUCH, on_click=click(on_close)),
                    ft.Button(
                        "Review",
                        key=REVIEW_KEY,
                        height=MIN_TOUCH,
                        disabled=not text,
                        on_click=click(review),
                    ),
                ],
            ),
        ],
    )
    sheet = _framed(tokens, "paste-entry-frame", body)
    # Mounted while available and only hidden, like the other floating layers.
    if wide:
        return ft.Container(
            key=LAYER_KEY, visible=visible, top=top, right=12, width=width, content=sheet
        )
    return ft.Container(key=LAYER_KEY, visible=visible, top=top, left=4, right=4, content=sheet)
