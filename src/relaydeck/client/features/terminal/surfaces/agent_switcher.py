"""Phone header agent switcher: the title button and the agent list."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import StatusPill
from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE
from relaydeck.client.components.design_system.primitives import SpaceChip, text_style
from relaydeck.client.components.design_system.terminal_window import TYPE_ROW_TITLE
from relaydeck.client.features.terminal.switcher import ACTIVE, SwitcherRow
from relaydeck.client.theme import space_tint

HEADER_HEIGHT = 28
ROW_HEIGHT = 44
LABEL_HEIGHT = 28
TITLE_INSET = 10
LIST_MAX_HEIGHT = 440
_LABEL_STYLE = {"size": 11, "weight": 700, "height": 1.2}
_BADGE_STYLE = {"size": 11, "weight": 700, "height": 1.2}

TITLE_KEY = "agent-detail-title"
OPEN_KEY = "agent-switcher-open"
CARET_KEY = "agent-switcher-caret"
BADGE_KEY = "agent-switcher-badge"
SCRIM_KEY = "agent-switcher-scrim"
LIST_KEY = "agent-switcher-list"
LAYER_KEY = "agent-switcher-layer"


def row_key(key: str) -> str:
    return f"agent-switcher-row-{key}"


def _plain_style(inset: int = 0) -> ft.ButtonStyle:
    return ft.ButtonStyle(
        padding=ft.Padding.symmetric(horizontal=inset),
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color=ft.Colors.TRANSPARENT,
        shape=ft.RoundedRectangleBorder(radius=8),
    )


def _badge(working: int) -> Any:
    return ft.Container(
        key=BADGE_KEY,
        height=18,
        padding=ft.Padding.symmetric(horizontal=6),
        border_radius=ft.BorderRadius.all(9),
        bgcolor=T.WORKING,
        alignment=ft.Alignment.CENTER,
        content=ft.Text(
            f"● {working} working",
            style=text_style(FAMILY_INTERFACE, _BADGE_STYLE, T.WORKING_TEXT),
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        ),
    )


@ft.component
def SwitcherTitle(
    title: str,
    working: int,
    open: bool,
    on_open: Callable[[], Any],
    dark: bool = True,
) -> Any:
    del dark
    content = ft.Row(
        spacing=4,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(
                title,
                key=TITLE_KEY,
                style=text_style(FAMILY_INTERFACE, TYPE_ROW_TITLE, T.TEXT),
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
                expand=True,
            ),
            ft.Icon(ft.Icons.ARROW_DROP_DOWN, key=CARET_KEY, size=16, color=T.TEXT_MUTED),
            *([_badge(working)] if working > 0 else []),
        ],
    )
    return ft.Semantics(
        label=f"{title}, switch agent, {working} working",
        button=True,
        expanded=open,
        exclude_semantics=True,
        content=ft.TextButton(
            key=OPEN_KEY,
            height=HEADER_HEIGHT,
            expand=True,
            style=_plain_style(TITLE_INSET),
            on_click=lambda _e: on_open(),
            content=content,
        ),
    )


def _row(row: SwitcherRow, on_pick: Callable[[str], Any]) -> Any:
    parts = [row.name, row.space, row.status, "current" if row.current else ""]
    label = ", ".join(part for part in parts if part)
    bar = ft.Container(
        width=3,
        height=ROW_HEIGHT - 12,
        border_radius=ft.BorderRadius.all(2),
        bgcolor=T.PRIMARY_FILL if row.current else ft.Colors.TRANSPARENT,
    )
    controls: list[Any] = [
        bar,
        ft.Text(
            row.name,
            style=text_style(FAMILY_INTERFACE, TYPE_ROW_TITLE, T.TEXT),
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
            expand=True,
        ),
    ]
    if row.space:
        controls.append(SpaceChip(row.space, space_tint(row.space)))
    controls.append(StatusPill(row.status))
    return ft.Semantics(
        label=label,
        button=True,
        selected=row.current,
        exclude_semantics=True,
        content=ft.TextButton(
            key=row_key(row.key),
            height=ROW_HEIGHT,
            style=_plain_style(),
            on_click=lambda _e, key=row.key: on_pick(key),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=controls,
            ),
        ),
    )


def _section(text: str) -> Any:
    return ft.Container(
        height=LABEL_HEIGHT,
        padding=ft.Padding.only(left=12, top=8),
        content=ft.Text(text, style=text_style(FAMILY_INTERFACE, _LABEL_STYLE, T.TEXT_MUTED)),
    )


def _body(rows: tuple[SwitcherRow, ...], on_pick: Callable[[str], Any]) -> tuple[list[Any], float]:
    active = [row for row in rows if row.status in ACTIVE]
    rest = [row for row in rows if row.status not in ACTIVE]
    controls: list[Any] = []
    height = 8.0
    for text, group in ((f"Working · {len(active)}", active), ("Others", rest)):
        if group:
            controls.append(_section(text))
            controls.extend(_row(row, on_pick) for row in group)
            height += LABEL_HEIGHT + ROW_HEIGHT * len(group)
    if len(rows) == 1:
        controls.append(
            ft.Container(
                height=LABEL_HEIGHT,
                padding=ft.Padding.only(left=12),
                content=ft.Text(
                    "No other agents on this host",
                    style=text_style(FAMILY_INTERFACE, _LABEL_STYLE, T.TEXT_MUTED),
                ),
            )
        )
        height += LABEL_HEIGHT
    return controls, height


@ft.component
def SwitcherList(
    rows: tuple[SwitcherRow, ...],
    on_pick: Callable[[str], Any],
    on_close: Callable[[], Any],
    top: float,
    dark: bool = True,
    visible: bool = True,
) -> Any:
    del dark
    controls, height = _body(rows, on_pick)
    card = ft.Container(
        key=LIST_KEY,
        top=top,
        left=4,
        right=4,
        height=min(height, LIST_MAX_HEIGHT),
        bgcolor=ft.Colors.with_opacity(0.94, T.PANEL),
        border_radius=ft.BorderRadius.all(12),
        border=ft.Border(top=ft.BorderSide(1, T.BORDER)),
        content=ft.Semantics(
            label="Agents",
            container=True,
            content=ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, controls=controls),
        ),
    )
    scrim = ft.GestureDetector(
        key=SCRIM_KEY,
        on_tap=lambda _e: on_close(),
        content=ft.Container(bgcolor=ft.Colors.with_opacity(0.35, T.GROUND), expand=True),
    )
    return ft.Container(
        key=LAYER_KEY,
        visible=visible,
        left=0,
        top=0,
        right=0,
        bottom=0,
        content=ft.Stack(
            expand=True,
            controls=[ft.Container(left=0, top=0, right=0, bottom=0, content=scrim), card],
        ),
    )
