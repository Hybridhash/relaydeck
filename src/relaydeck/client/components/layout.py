"""Responsive presentation helpers.

Pure functions over the layout breakpoints. Routing and
presentation share this decision: phone is single pane, compact/desktop
keep rail + list + detail. No Flet dependency.
"""

from __future__ import annotations

from relaydeck.client.theme import (
    COMPACT,
    DESKTOP,
    LIST_WIDTH,
    PHONE,
    RAIL_WIDTH,
    RAIL_WIDTH_COMPACT,
    layout_mode,
)


def rail_width(mode: str) -> int:
    """Rail column width; 0 on phone (rail hidden, bottom nav used)."""
    if mode == PHONE:
        return 0
    if mode == COMPACT:
        return RAIL_WIDTH_COMPACT
    return RAIL_WIDTH


def list_width(mode: str) -> int | None:
    """List column width; None on phone (list pane takes full width)."""
    if mode != DESKTOP:
        return None
    return LIST_WIDTH


def sidebar_width(mode: str, requested: float, viewport: float) -> float | None:
    """Clamp a desktop resize while reserving at least 320px for detail."""
    if mode != DESKTOP:
        return None
    maximum = max(240, min(420, viewport - rail_width(mode) - 328))
    return max(240, min(requested, maximum))


__all__ = [
    "COMPACT",
    "DESKTOP",
    "PHONE",
    "layout_mode",
    "list_width",
    "rail_width",
    "sidebar_width",
]
