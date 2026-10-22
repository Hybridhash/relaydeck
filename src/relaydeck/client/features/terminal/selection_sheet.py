"""Native selection over a frozen terminal snapshot, with explicit copy and dismissal."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.components.design_system.fonts import FAMILY_MONO
from relaydeck.client.features.terminal.events import click
from relaydeck.client.features.terminal.selection_area import selected_offsets
from relaydeck.client.features.terminal.selection_model import normalize_range
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH

SELECT_SHEET_KEY = "select-text-sheet"
SELECT_TITLE_KEY = "select-text-title"
SELECT_RANGE_KEY = "select-text-range"
SELECT_FIELD_KEY = "select-text-field"
SELECT_AREA_KEY = "select-text-area"
SELECT_COPY_KEY = "select-text-copy"
SELECT_CLOSE_KEY = "select-text-close"
SELECT_ERROR_KEY = "select-text-error"

MONO_STYLE = ft.TextStyle(
    font_family=FAMILY_MONO,
    size=14,
)


def range_label(text: str, start: int, end: int) -> str:
    """Human-readable summary of the anchored range for the sheet header."""
    if not text:
        return "No output to select"
    if start <= 0 and end >= len(text):
        return f"All text ({len(text)} chars)"
    lines = text[start:end].count("\n") + 1
    return f"Chars {start}-{end} of {len(text)} ({lines} lines)"


@ft.component
def SelectTextSheet(
    text: str,
    anchor: int | None,
    focus: int | None,
    on_copy: Callable[[int, int], Any],
    on_close: Callable[[], Any],
    error: str | None = None,
    on_selection_change: Callable[[int, int], Any] | None = None,
    dark: bool = True,
) -> Any:
    resolved = normalize_range(anchor, focus, text)

    async def picked(event: Any) -> None:
        offsets = selected_offsets(text, event.data)
        if on_selection_change is None or offsets is None:
            return
        result = on_selection_change(*offsets)
        if inspect.isawaitable(result):
            await result

    controls: list[Any] = [
        ft.Text(
            "Select Text",
            key=SELECT_TITLE_KEY,
            size=FONT_SIZE["body"],
            weight=ft.FontWeight.BOLD,
        ),
        ft.Text(
            range_label(text, resolved.start, resolved.end),
            key=SELECT_RANGE_KEY,
            size=FONT_SIZE["small"],
        ),
    ]
    if error is not None:
        controls.append(ft.Text(error, key=SELECT_ERROR_KEY))
    controls.extend(
        [
            ft.Column(
                expand=True,
                scroll=ft.ScrollMode.AUTO,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.SelectionArea(
                        key=SELECT_AREA_KEY,
                        on_change=picked,
                        content=ft.Text(text, key=SELECT_FIELD_KEY, style=MONO_STYLE),
                    )
                ],
            ),
            ft.Row(
                wrap=True,
                alignment=ft.MainAxisAlignment.END,
                controls=[
                    ft.Button(
                        "Copy",
                        key=SELECT_COPY_KEY,
                        height=MIN_TOUCH,
                        style=chrome_style(dark),
                        on_click=click(on_copy, resolved.anchor, resolved.focus),
                    ),
                    ft.Button(
                        "Done",
                        key=SELECT_CLOSE_KEY,
                        height=MIN_TOUCH,
                        style=chrome_style(dark),
                        on_click=click(on_close),
                    ),
                ],
            ),
        ]
    )
    return ft.Column(
        key=SELECT_SHEET_KEY,
        expand=True,
        spacing=8,
        controls=controls,
    )
