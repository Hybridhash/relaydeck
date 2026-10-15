"""Console navigation: bottom bar (<600) and side rail (>=600).

Four fixed destinations in order (console, terminals, usage,
settings). Hosts is a section inside settings. Controlled: `selected` id in, `on_select` id out. The
active destination carries a primary indicator pill; labels stay
12px with 700/500 active/idle weights.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE
from relaydeck.client.components.design_system.primitives import _action_button, text_style

DEST_CONSOLE = "console"
DEST_TERMINALS = "terminals"
DEST_USAGE = "usage"
DEST_SETTINGS = "settings"
# Legacy id of the Hosts destination (now a Settings section). Kept only so old references resolve.
DEST_HOSTS = "hosts"

DESTINATIONS: tuple[tuple[str, str, ft.IconData], ...] = (
    (DEST_CONSOLE, "Console", ft.Icons.VIEW_LIST),
    (DEST_TERMINALS, "Terminals", ft.Icons.TERMINAL),
    (DEST_USAGE, "Usage", ft.Icons.INSIGHTS),
    (DEST_SETTINGS, "Settings", ft.Icons.TUNE),
)


def _destination_button(
    dest_id: str,
    label: str,
    icon: ft.IconData,
    selected: bool,
    on_select: Callable[[str], Any],
    indicator_size: tuple[int, int],
    width: float | None,
    key_prefix: str,
) -> Any:
    active = selected
    ink = T.TEXT if active else T.TEXT_MUTED
    return _action_button(
        key=f"{key_prefix}-{dest_id}",
        width=width,
        expand=True if width is None else False,
        radius=T.RADIUS_KEY,
        tooltip=label,
        on_press=lambda: on_select(dest_id),
        content=ft.Container(
            padding=ft.Padding.only(top=10),
            content=ft.Column(
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        width=indicator_size[0],
                        height=indicator_size[1],
                        border_radius=ft.BorderRadius.all(indicator_size[1] / 2),
                        bgcolor=T.PRIMARY_FILL if active else None,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(icon, size=20, color=T.ON_PRIMARY if active else ink),
                    ),
                    ft.Text(
                        label,
                        style=text_style(
                            FAMILY_INTERFACE,
                            T.TYPE_NAV_LABEL_ACTIVE if active else T.TYPE_NAV_LABEL,
                            ink,
                        ),
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
            ),
        ),
    )


def ConsoleBottomBar(
    selected: str,
    on_select: Callable[[str], Any],
) -> Any:
    """76px bottom navigation bar for widths below 600."""
    return ft.Container(
        key="console-nav-bar",
        height=76,
        bgcolor=T.NAV,
        padding=ft.Padding.symmetric(horizontal=8),
        border=ft.Border.only(top=ft.BorderSide(1, T.BORDER)),
        content=ft.Row(
            spacing=0,
            controls=[
                _destination_button(
                    d, label, icon, d == selected, on_select, (60, 30), None, "console-nav"
                )
                for d, label, icon in DESTINATIONS
            ],
        ),
    )


def ConsoleRail(
    selected: str,
    on_select: Callable[[str], Any],
) -> Any:
    """96px side rail for widths at/above 600."""
    return ft.Container(
        key="console-nav-rail",
        width=T.RAIL_WIDTH,
        bgcolor=T.NAV,
        padding=ft.Padding.symmetric(vertical=24, horizontal=8),
        content=ft.Column(
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            scroll=ft.ScrollMode.AUTO,
            controls=[
                _destination_button(
                    d, label, icon, d == selected, on_select, (56, 32), 76, "console-rail"
                )
                for d, label, icon in DESTINATIONS
            ],
        ),
    )
