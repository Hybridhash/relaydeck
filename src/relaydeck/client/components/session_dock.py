"""Compact session dock with the shared chrome and phone targets."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.features.terminal.events import event_text
from relaydeck.client.theme import MIN_TOUCH, palette


@ft.component
def SessionDock(
    value: str,
    options: tuple[tuple[str, str], ...],
    on_select: Callable[[str], Any],
    dark: bool = True,
) -> Any:
    tokens = palette(dark)
    return ft.Dropdown(
        key="terminal-session-switcher",
        label="Session",
        height=MIN_TOUCH,
        value=value,
        text_style=ft.TextStyle(size=13, color=tokens["text"]),
        label_style=ft.TextStyle(color=tokens["muted"]),
        border=ft.OutlineInputBorder(side=ft.BorderSide(1, tokens["outline"])),
        filled=True,
        fill_color=tokens["raised"],
        options=[ft.DropdownOption(key=key, text=title) for key, title in options],
        on_select=lambda e: on_select(event_text(e)),
    )
