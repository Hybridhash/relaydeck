"""Floating status toast with one action (Undo, Retry)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE
from relaydeck.client.components.design_system.primitives import text_style

TOAST_SECONDS = 5
TOAST_HEIGHT = 56
_MESSAGE = {"size": 14, "weight": 600, "height": 1.2}
_STRONG = {"size": 14, "weight": 800, "height": 1.2}


def ActionToast(
    key: str,
    text: str,
    action: str,
    on_action: Callable[[], Any],
    on_dismiss: Callable[[], Any],
    *,
    emphasis: str = "",
    error: bool = False,
    open: bool = True,
) -> Any:
    """Light toast above the bottom edge: icon, message with an optional bold part, one action.

    Auto-dismisses after `TOAST_SECONDS`; the message is announced as a status, never trapped
    in focus.
    """
    ink = T.GROUND
    spans = [ft.TextSpan(text, style=text_style(FAMILY_INTERFACE, _MESSAGE, ink))]
    if emphasis:
        spans.append(ft.TextSpan(emphasis, style=text_style(FAMILY_INTERFACE, _STRONG, ink)))
    return ft.SnackBar(
        key=key,
        open=open,
        behavior=ft.SnackBarBehavior.FLOATING,
        bgcolor=T.TEXT,
        margin=ft.Margin.only(left=16, right=16, bottom=16),
        shape=ft.RoundedRectangleBorder(radius=16),
        duration=TOAST_SECONDS * 1000,
        persist=False,
        content=ft.Semantics(
            live_region=True,
            content=ft.Row(
                spacing=10,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(
                        ft.Icons.ERROR_OUTLINE if error else ft.Icons.CHECK_CIRCLE,
                        size=20,
                        color=T.TOAST_ACTION if not error else T.BLOCKED_SMALL_TEXT,
                    ),
                    ft.Text(spans=spans, expand=True, max_lines=2),
                ],
            ),
        ),
        action=ft.SnackBarAction(
            label=action,
            text_color=T.TOAST_ACTION,
            on_click=lambda _e: on_action(),
        ),
        on_dismiss=lambda _e: on_dismiss(),
    )
