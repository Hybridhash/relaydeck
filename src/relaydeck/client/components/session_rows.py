"""Session/host row builders.

Plain builders from the existing selector row shapes
(AgentRow/ShellRow/HostRow): agent/shell tiles are styled
``ft.ListTile`` controls, while dark-mode host tiles render the mock
``HostCard`` (same key/callbacks). Keys,
titles, subtitles, trailing words and callbacks are unchanged from
the lists; the builders add only the reference presentation:
active-row surface, rounded shape, comfortable density. Used by
inventory/components.py; the coordinator can reuse them wherever the
same rows render.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system.hosts import HostCard
from relaydeck.client.components.design_system.primitives import AgentPill
from relaydeck.client.components.status import StatusDot
from relaydeck.client.features.inventory.selectors import AgentRow, HostRow, ShellRow
from relaydeck.client.theme import (
    STATUS_LABELS,
    agent_badge_ink,
    agent_identity,
    agent_subtitle_rest,
    palette,
    status_color,
)

__all__ = ["agent_tile", "empty_hint", "host_tile", "shell_tile"]


def _shape(selected: bool, highlighted: bool = False, ring: str = "") -> Any:
    side = ft.BorderSide(2, ring) if highlighted else None
    return ft.RoundedRectangleBorder(radius=8 if selected or highlighted else 0, side=side)


def _text(value: str, color: str, *, title: bool = False, phone: bool = False) -> ft.Text:
    return ft.Text(
        value,
        color=color,
        size=(17 if phone else 15) if title else (13 if phone else 12),
        weight=ft.FontWeight.W_600 if title else None,
        max_lines=1 if title else 2,
        overflow=ft.TextOverflow.ELLIPSIS,
        tooltip=None if phone else value,
    )


def agent_tile(
    row: AgentRow,
    selected: bool,
    on_select: Callable[[str], Any],
    dark: bool = True,
    phone: bool = False,
    title: str | None = None,
    highlighted: bool = False,
    on_name_double_tap: Callable[[], Any] | None = None,
) -> ft.ListTile:
    tokens = palette(dark)
    name: ft.Control = _text(title or row.title, tokens["text"], title=True, phone=phone)
    if on_name_double_tap is not None:
        name = ft.GestureDetector(
            key=f"agent-name-{row.key}", on_double_tap=lambda _e: on_name_double_tap(), content=name
        )
    identity = agent_identity(row.agent, dark)
    rest = agent_subtitle_rest(row.subtitle, row.agent)
    meta_value = f" · {rest}" if rest and rest != row.subtitle else rest
    return ft.ListTile(
        key=f"agent-{row.key}",
        leading=StatusDot(row.status.value, dark=dark),
        title=name,
        subtitle=ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                AgentPill(
                    identity.label,
                    identity.color,
                    key=f"agent-badge-{row.key}",
                    ink=agent_badge_ink(row.agent, dark),
                ),
                *(
                    [
                        ft.Text(
                            meta_value,
                            color=tokens["muted"],
                            size=13 if phone else 12,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            tooltip=None if phone else row.subtitle,
                            expand=True,
                        )
                    ]
                    if meta_value
                    else []
                ),
            ],
        ),
        trailing=ft.Column(
            tight=True,
            spacing=2,
            controls=[
                ft.Text(
                    STATUS_LABELS[row.status.value],
                    key=f"agent-status-{row.key}",
                    color=status_color(row.status.value, dark),
                    size=12,
                    max_lines=1,
                ),
                ft.Text(
                    "Unread",
                    key=f"unread-{row.key}",
                    color=tokens["accent_text"],
                    size=12,
                    visible=row.unread,
                    tooltip=None if phone else "New session activity since last read",
                ),
            ],
        ),
        selected=selected,
        selected_tile_color=tokens["active_row_bg"],
        shape=_shape(selected, highlighted, tokens["accent_text"]),
        content_padding=ft.Padding.symmetric(horizontal=10, vertical=12),
        min_height=64,
        text_color=tokens["text"],
        selected_color=tokens["text"],
        hover_color=tokens["raised"],
        on_click=lambda e, key=row.key: on_select(key),
    )


def shell_tile(
    row: ShellRow,
    selected: bool,
    on_select: Callable[[str], Any],
    dark: bool = True,
    phone: bool = False,
) -> ft.ListTile:
    tokens = palette(dark)
    return ft.ListTile(
        key=f"shell-{row.key}",
        leading=ft.Icon(ft.Icons.TERMINAL, color=tokens["muted"]),
        title=_text(row.title, tokens["text"], title=True, phone=phone),
        subtitle=_text(row.subtitle, tokens["muted"], phone=phone),
        selected=selected,
        selected_tile_color=tokens["active_row_bg"],
        shape=_shape(selected),
        content_padding=ft.Padding.symmetric(horizontal=10, vertical=12),
        min_height=64,
        text_color=tokens["text"],
        selected_color=tokens["text"],
        hover_color=tokens["raised"],
        on_click=lambda e, key=row.key: on_select(key),
    )


def host_tile(
    row: HostRow,
    selected: bool,
    on_select: Callable[[str], Any],
    dark: bool = True,
    phone: bool = False,
) -> Any:
    if dark:
        # Mock host card: counts sub-line is real selector
        # text; chips stay empty (no fabricated badges); the key,
        # selection callback and reachable split are unchanged.
        return HostCard(
            row.key,
            row.title,
            row.subtitle,
            row.reachable,
            (),
            selected,
            on_select,
            compact=phone,
        )
    tokens = palette(dark)
    return ft.ListTile(
        key=f"host-{row.key}",
        leading=ft.Icon(
            ft.Icons.DNS,
            color=status_color("done" if row.reachable else "unknown", dark),
        ),
        title=_text(row.title, tokens["text"], title=True, phone=phone),
        subtitle=_text(row.subtitle, tokens["muted"], phone=phone),
        selected=selected,
        selected_tile_color=tokens["active_row_bg"],
        shape=_shape(selected),
        content_padding=ft.Padding.symmetric(horizontal=10, vertical=12),
        min_height=64,
        text_color=tokens["text"],
        selected_color=tokens["text"],
        hover_color=tokens["raised"],
        on_click=lambda e, key=row.key: on_select(key),
    )


def empty_hint(key: str, text: str) -> ft.Text:
    """Non-interactive empty-list explanation."""
    return ft.Text(text, key=key)
