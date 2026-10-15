"""Shared semantic input controls; presentation only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.theme import MIN_TOUCH, palette


def mode_button(
    label: str, key: str, selected: bool, callback: Callable[..., Any], dark: bool
) -> ft.Button:
    t = palette(dark)
    style = chrome_style(dark)
    if selected:
        style.color = t["accent_text"]
        style.bgcolor = t["active_row_bg"]
    return ft.Button(
        label,
        key=key,
        disabled=selected,
        height=MIN_TOUCH,
        style=style,
        tooltip=f"{label} selected" if selected else label,
        on_click=callback,
    )
