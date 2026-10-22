"""The keyboard button inside a draft field, shared by the phone and the iPad typing column."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as console_tokens
from relaydeck.client.features.terminal.diagnostics import diagnostics_logger
from relaydeck.client.features.terminal.events import click


def keep_focus(_event: Any) -> None:
    diagnostics_logger().info("composer_tap_outside action=keep_focus")


def keyboard_toggle(
    state: str, compact: bool, on_hide: Callable[[], Any], on_show: Callable[[], Any]
) -> Any | None:
    """Hide keyboard while the field is focused, Show keyboard otherwise.

    The toggle sits inside the field's decoration. Showing the keyboard must happen inside the
    user's tap on iOS, so the Show icon ignores pointers and the tap reaches the text field
    natively; a server-side focus request would arrive after the gesture has ended.
    """
    if state not in ("hide", "show"):
        return None
    disc = ft.Colors.with_opacity(0.35, console_tokens.SCROLL_THUMB_BG)
    if state == "hide":
        return ft.Semantics(
            key="composer-hide-keyboard-semantics",
            label="Hide keyboard",
            button=True,
            content=ft.IconButton(
                ft.Icons.KEYBOARD_HIDE,
                key="composer-hide-keyboard",
                width=44,
                height=44,
                icon_size=20,
                padding=0,
                style=ft.ButtonStyle(
                    bgcolor=disc, color=console_tokens.ON_PRIMARY, shape=ft.CircleBorder()
                ),
                tooltip=None if compact else "Hide keyboard",
                on_click=click(on_hide),
            ),
        )
    return ft.Semantics(
        key="composer-show-keyboard-semantics",
        label="Show keyboard",
        button=True,
        on_tap=lambda _e: on_show(),
        content=ft.Container(
            key="composer-show-keyboard",
            width=44,
            height=44,
            border_radius=22,
            bgcolor=disc,
            alignment=ft.Alignment.CENTER,
            ignore_interactions=True,
            content=ft.Column(
                tight=True,
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.KEYBOARD_ARROW_UP, size=14, color=console_tokens.ON_PRIMARY),
                    ft.Icon(ft.Icons.KEYBOARD, size=20, color=console_tokens.ON_PRIMARY),
                ],
            ),
        ),
    )
