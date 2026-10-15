"""Mock tokens + contrast + responsive helpers.

Dark chrome follows the design; light stays explicit.
Contrast pairs use the independent WCAG math in features/a11y/contrast.py.
Legacy graphic-only status colors remain separately audited.
"""

from __future__ import annotations

from relaydeck.client.components.layout import (
    COMPACT,
    DESKTOP,
    PHONE,
    layout_mode,
    list_width,
    rail_width,
)
from relaydeck.client.features.a11y.contrast import contrast_ratio
from relaydeck.client.theme import (
    ACCENT,
    BANNER_TONES,
    COMPACT_MAX_WIDTH,
    CONTRAST_PAIRS,
    DARK,
    FOCUS_RING_OFFSET,
    FOCUS_RING_WIDTH,
    FONT_SIZE,
    LIGHT,
    LIST_WIDTH,
    LIST_WIDTH_COMPACT,
    MIN_TOUCH,
    MONO_STACK,
    MOTION_HOVER_MS,
    MOTION_MODAL_MS,
    PHONE_MAX_WIDTH,
    RADIUS,
    RAIL_WIDTH,
    RAIL_WIDTH_COMPACT,
    SPACING,
    STATUS_COLORS,
    STATUS_LABELS,
    SURFACE_RADIUS,
    THEMES,
    WIDE_BREAKPOINT,
    palette,
)

# Transcribed from the design; independent of production tokens.
REFERENCE_ROOT = {
    "bg": "#130F26",
    "surface": "#191335",
    "raised": "#2B2356",
    "line": "#3B3270",
    "text": "#F5F2FF",
    "muted": "#B7AFDA",
    "accent": "#7443F5",
    "amber": "#FF8A3D",
}


def test_dark_palette_matches_reference_root() -> None:
    for key, value in REFERENCE_ROOT.items():
        assert DARK[key] == value, f"DARK[{key}] drifted from reference :root"
    # Reference hardcodes these outside :root; traces live in theme.py.
    assert DARK["on_accent"] == "#FFFFFF"
    assert DARK["rail_bg"] == "#1A1436"
    assert DARK["terminal_bg"] == "#0B0818"
    assert LIGHT["terminal_bg"] == "#101113"
    assert DARK["active_row_bg"] == "#1F1840"
    assert LIGHT["active_row_bg"] == "#E0EDFF"


def test_palettes_share_keys_and_resolve() -> None:
    assert set(LIGHT) == set(DARK)
    assert THEMES == {"dark": DARK, "light": LIGHT}
    assert palette(True) == DARK
    assert palette(False) == LIGHT
    assert palette() == DARK
    # Copies: callers cannot mutate the shared tables.
    palette()["bg"] = "#000000"
    assert DARK["bg"] == "#130F26"


def test_contrast_pairs_meet_minimum_in_both_themes() -> None:
    assert CONTRAST_PAIRS, "pair table must not be empty"
    for name, fg_key, bg_key, minimum in CONTRAST_PAIRS:
        for theme_name, theme in (("dark", DARK), ("light", LIGHT)):
            ratio = contrast_ratio(theme[fg_key], theme[bg_key])
            assert ratio >= minimum, f"{theme_name} {name}: {ratio:.2f} < {minimum}"
    # Brand/rail text pairs used by CompactRail (not in the table).
    assert contrast_ratio(DARK["on_accent"], DARK["accent"]) >= 4.5
    assert contrast_ratio(DARK["muted"], DARK["rail_bg"]) >= 4.5
    assert contrast_ratio(LIGHT["muted"], LIGHT["rail_bg"]) >= 4.5


def test_accessibility_geometry_and_legacy_graphic_tokens() -> None:
    assert SPACING == (4, 8, 12, 16, 24, 32)
    assert RADIUS == (4, 8, 12)
    assert ACCENT == "#0063CC"
    assert MIN_TOUCH == 48
    assert WIDE_BREAKPOINT == 1100
    assert STATUS_COLORS == {
        "idle": "#70757A",
        "working": "#1A73E8",
        "blocked": "#D93025",
        "done": "#188038",
        "unknown": "#70757A",
    }
    assert STATUS_LABELS == {
        "idle": "Idle",
        "working": "Working",
        "blocked": "Blocked",
        "done": "Done",
        "unknown": "Unknown",
    }
    assert BANNER_TONES == {
        "error": "#D93025",
        "warning": "#E8710A",
        "info": "#1A73E8",
    }


def test_responsive_and_type_tokens() -> None:
    assert (PHONE_MAX_WIDTH, COMPACT_MAX_WIDTH) == (599, 1099)
    assert (RAIL_WIDTH, RAIL_WIDTH_COMPACT) == (96, 96)
    assert (LIST_WIDTH, LIST_WIDTH_COMPACT) == (380, 380)
    assert FONT_SIZE == {
        "eyebrow": 12,
        "meta": 12,
        "small": 13,
        "body": 15,
        "base": 14,
        "title": 20,
        "headline": 22,
    }
    assert MONO_STACK == ("ui-monospace", "SFMono-Regular", "Consolas", "monospace")
    assert (MOTION_HOVER_MS, MOTION_MODAL_MS) == (140, 160)
    assert (FOCUS_RING_WIDTH, FOCUS_RING_OFFSET) == (2, 2)
    assert SURFACE_RADIUS == 8


def test_layout_modes_follow_reference_breakpoints() -> None:
    assert layout_mode(360) == PHONE
    assert layout_mode(599) == PHONE
    assert layout_mode(600) == COMPACT
    assert layout_mode(601) == COMPACT
    assert layout_mode(768) == COMPACT
    assert layout_mode(800) == COMPACT
    assert layout_mode(801) == COMPACT
    assert layout_mode(899.9) == COMPACT
    assert layout_mode(900) == COMPACT
    assert layout_mode(1099) == COMPACT
    assert layout_mode(1100) == DESKTOP
    assert layout_mode(1280) == DESKTOP


def test_layout_widths_per_mode() -> None:
    assert rail_width(PHONE) == 0  # rail hidden; bottom nav takes over
    assert rail_width(COMPACT) == RAIL_WIDTH_COMPACT
    assert rail_width(DESKTOP) == RAIL_WIDTH
    assert list_width(PHONE) is None  # list pane takes full width
    assert list_width(COMPACT) is None
    assert list_width(DESKTOP) == LIST_WIDTH
    # Compact/desktop rails keep >= 48dp touch geometry.
    assert RAIL_WIDTH_COMPACT >= MIN_TOUCH
    assert RAIL_WIDTH >= MIN_TOUCH
