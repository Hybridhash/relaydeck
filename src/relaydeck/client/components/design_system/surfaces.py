"""Shared surface components for the feature-palette world.

Feature screens (composer, shell, agent detail) draw on the themed
palette dict (`palette(dark)`: surface/line/muted roles), not the console
token table. These components take that dict so the three duplicated
`_framed` helpers collapse into one without changing any resolved value.
"""

from __future__ import annotations

from typing import Any

import flet as ft

from relaydeck.client.components.design_system import tokens as T


def frame_border(tokens: dict[str, str]) -> ft.Border:
    """Reference 1px line border (theme-selected)."""
    side = ft.BorderSide(width=1, color=tokens["line"])
    return ft.Border(left=side, top=side, right=side, bottom=side)


def SurfaceFrame(
    key: str,
    content: Any,
    *,
    tokens: dict[str, str],
    disabled: bool = False,
) -> Any:
    """Themed surface frame: surface bg, line border, radius, padding."""
    return ft.Container(
        key=key,
        disabled=disabled,
        bgcolor=tokens["surface"],
        border=frame_border(tokens),
        border_radius=T.SURFACE_RADIUS,
        padding=12,
        content=content,
    )


def MutedText(
    value: str,
    *,
    key: str | None = None,
    tokens: dict[str, str],
    size: float = 13,
) -> Any:
    """Muted palette text (the shell "Connecting…" line)."""
    return ft.Text(value, key=key, color=tokens["muted"], size=size)
