"""Hosts presentation: header, group labels, host cards.

Values come from the design. Controlled presentation: display
props in, selection callback out. New type specs live here because
tokens.py is out of scope here; grouped counts come from real rows.

Deliberate deviations from the HTML:
- No latency or address lines: that data is unavailable, and the
  assignment forbids fabricated metadata.
- No Retry button: no real host retry action exists (management
  refresh reconciles access); a fake Retry is forbidden.
- 48px icon target (mock draws 44px); offline border is solid in
  the dashed-token color (Flet cannot draw dashed borders).
- Chips render only when the application passes real entries; nothing is
  invented (no "Alerts on" / "Via jump host" claims).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T
from relaydeck.client.components.design_system.cards import _selected_outline
from relaydeck.client.components.design_system.fonts import FAMILY_INTERFACE, FAMILY_MONO
from relaydeck.client.components.design_system.primitives import _action_button, text_style

__all__ = ["HostCard", "HostGroupLabel", "HostsHeader"]

# Local mock specs (tokens.py is out of scope here).
_HOST_NAME = {"size": 18, "weight": 700, "height": 22 / 18}
_CHIP_TEXT = {"size": 12.5, "weight": 600, "height": 1.5}
_CHIP_INK = T.CHIP_INK  # mock chip label (theme.toml [console] chip_ink)
_CARD_PAD = 14
_ICON_SIZE = 48  # mock draws 44px; 48px is the hit floor


def HostsHeader(eyebrow: str, title: str) -> Any:
    """Mock title hierarchy: mono eyebrow over the 34px title, no actions."""
    return ft.Column(
        key="hosts-header",
        spacing=0,
        controls=[
            ft.Text(
                eyebrow.upper(),
                style=text_style(FAMILY_MONO, T.TYPE_EYEBROW, T.TEXT_MUTED, 0.06),
                no_wrap=True,
            ),
            ft.Text(title, style=text_style(FAMILY_INTERFACE, T.TYPE_TITLE, T.TEXT, -0.02)),
        ],
    )


def HostGroupLabel(label: str, key: str | None = None) -> Any:
    """Mono uppercase group header ("Connected · N" / "Offline · N")."""
    return ft.Text(
        label.upper(),
        key=key,
        style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED, 0.08),
        no_wrap=True,
    )


def _host_chip(entry: str) -> Any:
    """26px raised chip; height comes from text + padding (grows at scale)."""
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        border_radius=ft.BorderRadius.all(13),
        bgcolor=T.RAISED,
        content=ft.Text(
            entry,
            style=text_style(FAMILY_INTERFACE, _CHIP_TEXT, _CHIP_INK),
            no_wrap=True,
        ),
    )


def HostCard(
    host_id: str,
    name: str,
    line: str,
    reachable: bool,
    chips: tuple[str, ...],
    selected: bool,
    on_select: Callable[[str], Any],
    compact: bool = False,
) -> Any:
    """Mock host card: 48px icon square, 18px name, mono detail line.

    Reachable cards are dark with a lime icon square; offline cards
    use the panel surface, dimmed name and muted icon. The full name
    stays in the tooltip when the title ellipsizes. Key matches the
    long-standing `host-{key}` contract.
    """
    body: list[Any] = [
        ft.Row(
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=_ICON_SIZE,
                    height=_ICON_SIZE,
                    border_radius=ft.BorderRadius.all(14),
                    bgcolor=T.DONE if reachable else T.RAISED,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Icon(
                        ft.Icons.STORAGE,
                        size=22,
                        color=T.DONE_TEXT if reachable else T.TEXT_MUTED,
                    ),
                ),
                ft.Column(
                    spacing=1,
                    expand=True,
                    controls=[
                        ft.Text(
                            name,
                            style=text_style(
                                FAMILY_INTERFACE,
                                _HOST_NAME,
                                T.TEXT if reachable else T.TEXT_SECONDARY,
                            ),
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.Text(
                            line,
                            style=text_style(FAMILY_MONO, T.TYPE_META, T.TEXT_MUTED),
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ],
                ),
            ],
        )
    ]
    if chips:
        body.append(
            ft.Wrap(
                spacing=8,
                run_spacing=8,
                controls=[_host_chip(entry) for entry in chips],
            )
        )
    return _action_button(
        key=f"host-{host_id}",
        radius=T.RADIUS_TILE,
        tooltip=None if compact else name,
        on_press=lambda: on_select(host_id),
        content=ft.Container(
            padding=ft.Padding.all(_CARD_PAD),
            border_radius=ft.BorderRadius.all(T.RADIUS_TILE),
            bgcolor=T.CARD if reachable else T.PANEL,
            border=ft.Border.all(1, T.BORDER if reachable else T.BORDER_DASHED_OFFLINE),
            shadow=_selected_outline(selected),
            content=ft.Column(spacing=12, controls=body),
        ),
    )
