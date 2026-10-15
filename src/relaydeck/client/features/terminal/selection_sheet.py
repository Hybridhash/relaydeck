"""Select Text sheet.

Declarative Flet sheet: read-only, selectable,
monospaced output with an anchored initial selection, an explicit Copy
button, and explicit Done dismissal. Presentational only: the parent owns
the text, the anchor/focus, and the clipboard write; this module never
touches the input path or any draft.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.features.terminal.events import click, selection_change
from relaydeck.client.features.terminal.selection_model import (
    normalize_range,
    str_to_utf16_offset,
    utf16_to_str_offset,
)
from relaydeck.client.theme import FONT_SIZE, MIN_TOUCH

SELECT_SHEET_KEY = "select-text-sheet"
SELECT_TITLE_KEY = "select-text-title"
SELECT_RANGE_KEY = "select-text-range"
SELECT_FIELD_KEY = "select-text-field"
SELECT_COPY_KEY = "select-text-copy"
SELECT_CLOSE_KEY = "select-text-close"
SELECT_ERROR_KEY = "select-text-error"

MONO_STYLE = ft.TextStyle(
    font_family="monospace",
    font_family_fallback=["Courier New", "Courier", "Roboto Mono"],
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
    selection = ft.TextSelection(
        base_offset=str_to_utf16_offset(text, resolved.anchor),
        extent_offset=str_to_utf16_offset(text, resolved.focus),
    )

    async def _forward_selection(base_utf16: int, extent_utf16: int) -> None:
        # Fully controlled sheet: the parent owns the CURRENT range, so
        # Copy always binds the live anchor/focus, never a stale open.
        if on_selection_change is None:
            return
        result = on_selection_change(
            utf16_to_str_offset(text, base_utf16),
            utf16_to_str_offset(text, extent_utf16),
        )
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
            ft.TextField(
                key=SELECT_FIELD_KEY,
                value=text,
                read_only=True,
                label="Read-only terminal output",
                multiline=True,
                expand=True,
                autofocus=False,
                text_style=MONO_STYLE,
                selection=selection,
                on_selection_change=selection_change(_forward_selection),
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
