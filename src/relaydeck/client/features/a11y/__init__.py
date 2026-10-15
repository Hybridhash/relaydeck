"""Accessibility primitives.

Flet-free helpers (announcer batching, focus-order audit, contrast math)
plus tiny declarative wrappers (`LiveRegion`). Domain logic stays
testable without Flet; only the wrappers import it.
"""

from relaydeck.client.features.a11y.announcer import (
    ANNOUNCE_WINDOW_MS,
    LiveRegion,
    StatusAnnouncer,
)
from relaydeck.client.features.a11y.contrast import (
    MIN_GRAPHIC_RATIO,
    MIN_TEXT_RATIO,
    SURFACE_DARK_REF,
    SURFACE_LIGHT_REF,
    audit_theme_tokens,
    contrast_ratio,
    relative_luminance,
)
from relaydeck.client.features.a11y.focus import (
    DOCK_KEY_ORDER,
    FOCUS_ORDER,
    check_focus_order,
    focus_order,
    interactive_controls,
    iter_controls,
)

__all__ = [
    "ANNOUNCE_WINDOW_MS",
    "DOCK_KEY_ORDER",
    "FOCUS_ORDER",
    "MIN_GRAPHIC_RATIO",
    "MIN_TEXT_RATIO",
    "SURFACE_DARK_REF",
    "SURFACE_LIGHT_REF",
    "LiveRegion",
    "StatusAnnouncer",
    "audit_theme_tokens",
    "check_focus_order",
    "contrast_ratio",
    "focus_order",
    "interactive_controls",
    "iter_controls",
    "relative_luminance",
]
