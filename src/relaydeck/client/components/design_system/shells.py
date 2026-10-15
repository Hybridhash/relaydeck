"""Terminals page list pieces.

Controlled presentation: display props in, callbacks out. Same shape as
the console list: header with counts, lime New terminal, a collapse
button; a filter field; host chips; shell rows with a terminal icon
tile, name, mono "host · N panes" and Live (lime dot) or Detached
(hollow muted dot). The selected row carries the 3px light outline.

"Live" means this device currently holds the shell attached; anything
else is "Detached" (the shell keeps running on its host either way).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.cards import _selected_outline
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import (
    RoundIconButton,
    _action_button,
    text_style,
)

TYPE_LIVE = {"size": 12.5, "weight": 700, "height": 1.2}
TYPE_DETACHED = {"size": 12.5, "weight": 600, "height": 1.2}
LIVE_INK = T.LIVE_INK


@dataclass(frozen=True)
class ShellListRow:
    key: str
    title: str
    meta: str
    live: bool


def LiveMark(live: bool) -> Any:
    """Lime dot + "Live", or hollow muted dot + "Detached"."""
    dot = ft.Container(
        width=8,
        height=8,
        border_radius=ft.BorderRadius.all(4),
        bgcolor=T.DONE if live else None,
        border=None if live else ft.Border.all(2, T.TEXT_TERMINAL_MUTED),
    )
    return ft.Row(
        tight=True,
        spacing=6,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            dot,
            ft.Text(
                "Live" if live else "Detached",
                style=text_style(
                    FAMILY_INTERFACE,
                    TYPE_LIVE if live else TYPE_DETACHED,
                    LIVE_INK if live else T.TEXT_MUTED,
                ),
            ),
        ],
    )


def ShellCard(
    row: ShellListRow,
    selected: bool,
    on_select: Callable[[str], Any],
    *,
    compact: bool = False,
) -> Any:
    """One shell row: icon tile, name + mono meta, Live/Detached."""
    return _action_button(
        key=f"shell-{row.key}",
        radius=T.RADIUS_CARD,
        tooltip=None if compact else row.title,
        on_press=lambda: on_select(row.key),
        content=ft.Container(
            padding=ft.Padding.symmetric(horizontal=T.CARD_PAD_H, vertical=T.CARD_PAD_V),
            border_radius=ft.BorderRadius.all(T.RADIUS_CARD),
            bgcolor=T.CARD,
            border=ft.Border.all(1, T.BORDER),
            shadow=_selected_outline(selected),
            content=ft.Row(
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        width=44,
                        height=44,
                        border_radius=ft.BorderRadius.all(14),
                        bgcolor=T.RAISED,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            ft.Icons.TERMINAL,
                            size=22,
                            color=T.PRIMARY_TEXT_LIGHT if row.live else T.TEXT_MUTED,
                        ),
                    ),
                    ft.Column(
                        expand=True,
                        spacing=1,
                        tight=True,
                        controls=[
                            ft.Text(
                                row.title,
                                style=text_style(FAMILY_INTERFACE, T.TYPE_CARD_TITLE, T.TEXT),
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                row.meta,
                                style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                    LiveMark(row.live),
                ],
            ),
        ),
    )


def CollapsedShellTile(row: ShellListRow, selected: bool, on_select: Callable[[str], Any]) -> Any:
    """48px strip tile: two-letter initials, full name as tooltip."""
    return _action_button(
        key=f"shell-tile-{row.key}",
        width=T.MIN_TOUCH,
        height=T.MIN_TOUCH,
        radius=14,
        tooltip=f"{row.title} · {'Live' if row.live else 'Detached'}",
        on_press=lambda: on_select(row.key),
        content=ft.Container(
            border_radius=ft.BorderRadius.all(14),
            bgcolor=T.RAISED,
            border=ft.Border.all(1, T.DONE if row.live else T.BORDER),
            shadow=_selected_outline(selected),
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                T.initials(row.title),
                style=text_style(FAMILY_INTERFACE, T.TYPE_TILE_INITIALS, T.TEXT),
            ),
        ),
    )


def ListHeader(
    eyebrow: str,
    title: str,
    actions: list[Any],
    key: str,
) -> Any:
    """Mono eyebrow + 34px title with round actions on the right."""
    return ft.Row(
        key=key,
        spacing=T.GAP_CARD,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Column(
                spacing=0,
                expand=True,
                controls=[
                    ft.Text(
                        eyebrow.upper(),
                        style=text_style(FAMILY_MONO, T.TYPE_EYEBROW, T.TEXT_MUTED, 0.06),
                        no_wrap=True,
                    ),
                    ft.Text(title, style=text_style(FAMILY_INTERFACE, T.TYPE_TITLE, T.TEXT, -0.02)),
                ],
            ),
            *actions,
        ],
    )


def FilterField(
    value: str,
    on_change: Callable[[str], Any],
    *,
    hint: str,
    key: str,
) -> Any:
    """48px rounded filter field with a leading search glyph."""
    return ft.TextField(
        key=key,
        value=value,
        hint_text=hint,
        height=T.MIN_TOUCH,
        text_size=15,
        color=T.TEXT,
        cursor_color=T.PRIMARY_TEXT_LIGHT,
        hint_style=ft.TextStyle(color=T.TEXT_MUTED),
        prefix_icon=ft.Icons.SEARCH,
        filled=True,
        fill_color=T.CARD,
        border_radius=24,
        border_color=T.BORDER,
        focused_border_color=T.PRIMARY_TEXT,
        content_padding=ft.Padding.symmetric(horizontal=16, vertical=12),
        on_change=lambda e: on_change(str(e.control.value or "")),
    )


def TerminalsHeader(
    shell_count: int,
    host_count: int,
    on_new: Callable[[], Any],
    on_collapse: Callable[[], Any] | None,
) -> Any:
    eyebrow = (
        f"{shell_count} shell{'s' if shell_count != 1 else ''} on "
        f"{host_count} host{'s' if host_count != 1 else ''}"
    )
    actions: list[Any] = [
        RoundIconButton(ft.Icons.ADD, "New terminal", on_new, tone="lime", key="new-terminal-open")
    ]
    if on_collapse is not None:
        actions.append(
            RoundIconButton(
                ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT,
                "Collapse shell list",
                on_collapse,
                key="shells-collapse",
            )
        )
    return ListHeader(eyebrow, "Terminals", actions, key="terminals-header")


__all__ = [
    "CollapsedShellTile",
    "FilterField",
    "ListHeader",
    "LiveMark",
    "ShellCard",
    "ShellListRow",
    "TerminalsHeader",
]
