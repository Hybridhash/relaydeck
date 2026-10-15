"""Compact navigation rail.

Reference `.rail`: narrow branded column with one labeled action per
destination. Driven by the existing destinations data shape
``(id, label, icon-name)`` plus the existing ``on_destination``
callback shape. Every destination is a >=48dp labeled button in
destination order (keyboard focus order); the selected destination
carries the active-row surface in addition to its label.

Standalone proposal component: AppShell keeps the framework
NavigationRail/NavigationBar until the coordinator adopts this rail
and updates the declared FOCUS_ORDER sequences.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.appearance import chrome_style
from relaydeck.client.theme import (
    FONT_SIZE,
    MIN_TOUCH,
    RAIL_WIDTH,
    RAIL_WIDTH_COMPACT,
    palette,
)

__all__ = ["CompactRail"]


@ft.component
def CompactRail(
    destinations: tuple[tuple[str, str, str], ...],
    selected: str,
    on_select: Callable[[str], Any],
    compact: bool = False,
    dark: bool = True,
) -> Any:
    tokens = palette(dark)
    width = RAIL_WIDTH_COMPACT if compact else RAIL_WIDTH
    buttons: list[Any] = []
    for dest_id, label, icon in destinations:
        active = dest_id == selected
        buttons.append(
            ft.Button(
                content=ft.Column(
                    spacing=2,
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            getattr(ft.Icons, icon),
                            size=20,
                            color=tokens["accent_text"] if active else tokens["muted"],
                        ),
                        ft.Text(
                            label,
                            key=f"rd2-rail-label-{dest_id}",
                            size=10,
                            color=tokens["accent_text"] if active else tokens["muted"],
                        ),
                    ],
                ),
                key=f"rd2-rail-{dest_id}",
                tooltip=label,
                width=width - 8,
                height=MIN_TOUCH + 8,
                style=chrome_style(dark),
                bgcolor=tokens["active_row_bg"] if active else None,
                on_click=lambda e, key=dest_id: on_select(key),
            )
        )
    return ft.Container(
        key="rd2-rail",
        width=width,
        bgcolor=tokens["rail_bg"],
        padding=4,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Container(
                    key="rd2-rail-brand",
                    width=40,
                    height=40,
                    border_radius=12,
                    bgcolor=tokens["accent_text"],
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text(
                        "R",
                        key="rd2-rail-brand-label",
                        size=21,
                        weight=ft.FontWeight.BOLD,
                        color=tokens["on_accent"],
                    ),
                ),
                ft.Text(
                    "RelayDeck",
                    key="rd2-rail-title",
                    size=FONT_SIZE["eyebrow"],
                    color=tokens["muted"],
                ),
                *buttons,
            ],
        ),
    )
